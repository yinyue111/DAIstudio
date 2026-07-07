"""Video result download, persistence, and settlement helpers."""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone

from billiard.exceptions import SoftTimeLimitExceeded

from ..config import settings
from ..db import SessionLocal
from ..models import GenAsset, GenTask
from . import credits, gateway, locks, storage, usage, video_frames
from .config_store import get_model_config
from .generation_common import (
    fail_and_refund,
    mark_needs_review,
    publish_task_update,
    settlement_cost,
)
from .generation_media import localize_video_poster, video_media_meta, video_poster_url
from .generation_model_runtime import model_from_snapshot
from .generation_state import (
    NEEDS_REVIEW,
    LocalVideoSettlementError,
    VideoResultValidationError,
    claim_terminal,
)
from .generation_state import TERMINAL_STATUSES as TERMINAL
from .generation_video_flow import (
    VIDEO_DOWNLOAD_LIVENESS_TTL,
    VIDEO_DOWNLOAD_MAX_ATTEMPTS,
    VIDEO_POLL_INTERVAL,
    clear_poll_alive,
    clear_video_download_alive,
    download_lock_key,
    download_result_from_task,
    enqueue_video_download,
    mark_video_download_alive,
    unlink_keys,
)
from .progress import set_progress

log = logging.getLogger("generation")

EnqueueVideoDownload = Callable[..., None]
GetModelConfig = Callable[..., object | None]


def _existing_local_key(keys: list[str], prefixes: tuple[str, ...]) -> str | None:
    for key in keys:
        if not key.startswith(prefixes):
            continue
        try:
            path = storage.local_path(key)
        except Exception:  # noqa: BLE001
            continue
        if path.exists() and path.is_file():
            return key
    return None


def _local_video_result_keys(task: GenTask) -> list[str]:
    params = task.params or {}
    return [str(key) for key in (params.get("_video_result_keys") or []) if key]


def video_review_has_local_results(task: GenTask) -> bool:
    """Whether a video review task can settle from already saved result keys."""
    if task.category != "video":
        return False
    keys = _local_video_result_keys(task)
    if task.stage == "final":
        return _existing_local_key(keys, ("video_hd/",)) is not None
    return _existing_local_key(keys, ("video_preview/", "video_hd/", "preview/")) is not None


def persist_local_video_result_asset(db, task: GenTask, keys: list[str]) -> list[str]:
    """Expose an already-saved video result before a held task is reconciled.

    Returns newly written keys that should be deleted if the surrounding
    settlement later rolls back.
    """
    if db.query(GenAsset).filter(GenAsset.task_id == task.id).count() > 0:
        return []
    fallback_duration = int((task.params or {}).get("duration", 0) or 0) or None
    written_keys: list[str] = []
    if task.stage == "final":
        media_key = _existing_local_key(keys, ("video_hd/",))
        if not media_key:
            raise ValueError("视频待对账任务缺少本地高清结果,请填写结果 URL")
        preview_key = _existing_local_key(keys, ("preview/",))
        if not preview_key:
            poster = video_frames.extract_poster(str(storage.local_path(media_key)))
            if poster:
                preview_key = storage.save_bytes(poster, "preview", "jpg")
                written_keys.append(preview_key)
        if not preview_key:
            raise ValueError("视频待对账任务缺少本地预览封面,请填写结果 URL")
        media_meta = video_media_meta(media_key, fallback_duration)
        db.add(
            GenAsset(
                task_id=task.id,
                user_id=task.user_id,
                type="video",
                preview_url=storage.public_url(preview_key),
                hd_url=storage.public_url(media_key),
                watermarked=False,
                unlocked=True,
                width=media_meta.get("width"),
                height=media_meta.get("height"),
                duration=media_meta.get("duration"),
            )
        )
        return written_keys

    media_key = _existing_local_key(keys, ("video_preview/", "video_hd/", "preview/"))
    if not media_key:
        raise ValueError("视频待对账任务缺少本地结果,请填写结果 URL")
    media_meta = (
        video_media_meta(media_key, fallback_duration)
        if media_key.startswith(("video_preview/", "video_hd/"))
        else {"width": None, "height": None, "duration": fallback_duration}
    )
    db.add(
        GenAsset(
            task_id=task.id,
            user_id=task.user_id,
            type="video",
            preview_url=storage.public_url(media_key),
            hd_url=None,
            watermarked=False,
            unlocked=True,
            width=media_meta.get("width"),
            height=media_meta.get("height"),
            duration=media_meta.get("duration"),
        )
    )
    return written_keys


def hold_video_download_for_reconciliation(db, task_id: int, error: str) -> None:
    """Hold provider-success videos when local persistence failed."""
    db.rollback()
    task = db.get(GenTask, task_id)
    if not task:
        return
    params = dict(task.params or {})
    mark_needs_review(
        db,
        task_id,
        (
            "视频已由上游生成,但结果下载落盘失败,需要系统恢复或管理员确认。"
            f"external_task_id={task.external_task_id or 'unknown'}; "
            f"result_url={'present' if params.get('_video_result_url') else 'missing'}; "
            f"error={error[:500]}"
        ),
    )


def fail_video_download_and_refund(db, task_id: int, error: str) -> None:
    """Fail a video whose provider result cannot be downloaded locally.

    The user has no usable asset in this state. Keeping it in needs_review makes
    the gallery look stuck and requires manual reconciliation, so refund the
    frozen credits and surface a terminal failure instead.
    """
    fail_and_refund(
        db,
        task_id,
        error,
        public_error="视频结果下载失败，已退回冻结积分，请稍后重试",
    )


def try_enqueue_video_download(
    db,
    task_id: int,
    *,
    countdown: int = 0,
    enqueue_video_download_fn: EnqueueVideoDownload | None = None,
) -> None:
    enqueue = enqueue_video_download_fn or enqueue_video_download
    try:
        enqueue(task_id, countdown=countdown)
    except Exception as e:  # noqa: BLE001
        hold_video_download_for_reconciliation(db, task_id, str(e))


def finalize_or_retry_video_download(
    db,
    task: GenTask,
    model,
    result: dict,
    *,
    enqueue_video_download_fn: EnqueueVideoDownload | None = None,
) -> bool:
    """Return True when finalised; False when a recoverable download retry was queued."""
    lock_key = download_lock_key(task.id)
    lock_token = locks.acquire(lock_key, ttl=VIDEO_DOWNLOAD_LIVENESS_TTL)
    if not lock_token:
        log.info("video task %s download/finalize already in progress", task.id)
        return False
    mark_video_download_alive(task.id)
    task._download_lock_token = lock_token
    try:
        finalize_video_success(db, task, model, result)
        return True
    except VideoResultValidationError as e:
        # The provider already reported a terminal success, so an invalid or
        # unverifiable local file is an accounting ambiguity, not a clean user
        # retry. Keep frozen credits for reconciliation instead of refunding.
        usage.record_call(
            db,
            kind="video_download",
            model_id=model.model_id,
            user_id=task.user_id,
            task_id=task.id,
            status="failed",
            detail={
                "stage": task.stage,
                "external_task_id": task.external_task_id,
                "permanent": True,
                "needs_review": True,
                "error": str(e)[:300],
            },
        )
        hold_video_download_for_reconciliation(db, task.id, str(e))
        return True
    except LocalVideoSettlementError as e:
        usage.record_call(
            db,
            kind="video_download",
            model_id=model.model_id,
            user_id=task.user_id,
            task_id=task.id,
            status="failed",
            detail={
                "stage": task.stage,
                "external_task_id": task.external_task_id,
                "local_settlement": True,
                "error": str(e)[:300],
            },
        )
        hold_video_download_for_reconciliation(db, task.id, str(e))
        return True
    except SoftTimeLimitExceeded:
        log.warning("video task %s download/finalize hit soft time limit; holding for review", task.id)
        hold_video_download_for_reconciliation(db, task.id, "视频结果下载执行超时")
        return True
    except Exception as e:  # noqa: BLE001
        mark_video_download_alive(task.id)
        params = dict(task.params or {})
        attempts = int(params.get("_video_download_attempts") or 0)
        usage_detail = {
            "stage": task.stage,
            "external_task_id": task.external_task_id,
            "attempt": attempts,
            "error": str(e)[:300],
        }
        if attempts < VIDEO_DOWNLOAD_MAX_ATTEMPTS:
            log.warning(
                "video task %s download/finalize attempt %s/%s failed: %s",
                task.id,
                attempts,
                VIDEO_DOWNLOAD_MAX_ATTEMPTS,
                e,
            )
            usage.record_call(
                db,
                kind="video_download",
                model_id=model.model_id,
                user_id=task.user_id,
                task_id=task.id,
                status="failed",
                detail=usage_detail,
            )
            try_enqueue_video_download(
                db,
                task.id,
                countdown=VIDEO_POLL_INTERVAL,
                enqueue_video_download_fn=enqueue_video_download_fn,
            )
            return False
        usage_detail["permanent"] = True
        usage_detail["needs_review"] = True
        usage.record_call(
            db,
            kind="video_download",
            model_id=model.model_id,
            user_id=task.user_id,
            task_id=task.id,
            status="failed",
            detail=usage_detail,
        )
        hold_video_download_for_reconciliation(db, task.id, str(e))
        return True
    finally:
        if hasattr(task, "_download_lock_token"):
            delattr(task, "_download_lock_token")
        locks.release(lock_key, lock_token)


def run_video_download_task(
    task_id: int,
    *,
    get_model_config_fn: GetModelConfig | None = None,
    enqueue_video_download_fn: EnqueueVideoDownload | None = None,
) -> None:
    """Persist a completed provider video on the dedicated download queue."""
    db = SessionLocal()
    try:
        task = db.get(GenTask, task_id)
        if not task or task.status in TERMINAL:
            return
        if task.category != "video" or task.phase != "downloading":
            return
        model_loader = get_model_config_fn or get_model_config
        model = model_loader(db, "video")
        if not model:
            hold_video_download_for_reconciliation(db, task_id, "视频模型配置缺失")
            return
        model = model_from_snapshot(task, model)
        finalize_or_retry_video_download(
            db,
            task,
            model,
            download_result_from_task(task),
            enqueue_video_download_fn=enqueue_video_download_fn,
        )
    except SoftTimeLimitExceeded:
        log.warning("run_video_download_task %s hit soft time limit; holding for review", task_id)
        hold_video_download_for_reconciliation(db, task_id, "视频结果下载执行超时")
    except Exception as e:  # noqa: BLE001
        log.exception("run_video_download_task %s failed", task_id)
        hold_video_download_for_reconciliation(db, task_id, str(e))
    finally:
        db.close()


def finalize_video_success(db, task: GenTask, model, result: dict) -> None:
    params = dict(task.params or {})
    video_url = result.get("url") or params.get("_video_result_url")
    # Only mock mode may legitimately lack a URL; a real "succeeded" with no URL
    # is a failure (don't fabricate a placeholder and charge for it).
    if not result.get("mock") and not video_url:
        raise VideoResultValidationError("视频网关返回成功但未提供视频地址")
    if video_url:
        params["_video_result_url"] = video_url
    params.setdefault("_video_download_started_at", datetime.now(timezone.utc).isoformat())
    params["_video_download_attempts"] = int(params.get("_video_download_attempts") or 0) + 1
    task.params = params
    task.phase = "downloading"
    db.commit()
    mark_video_download_alive(task.id)

    media_meta = {"width": None, "height": None, "duration": int(params.get("duration", 0)) or None}
    written_keys: list[str] = []
    if result.get("mock") or not video_url:
        # mock / no direct url -> placeholder still so the flow is demonstrable
        still = gateway.mock_video_preview_image()
        pv_key = storage.save_bytes(still, "preview", "png")
        written_keys.append(pv_key)
        preview_url = storage.public_url(pv_key)
        hd_url = preview_url if task.stage == "final" else None
    else:
        # Download + store the mp4 locally (Ark URLs expire within ~24h). If we
        # can't persist it, hold the task for reconciliation rather than charging
        # for a result behind an expiring URL that will soon 404.
        try:
            set_progress(task.id, 92, "running")
            def mark_download_progress() -> None:
                mark_video_download_alive(task.id)
                locks.refresh(download_lock_key(task.id), getattr(task, "_download_lock_token", None), VIDEO_DOWNLOAD_LIVENESS_TTL)

            mark_download_progress()
            media_subdir = "video_hd" if task.stage == "final" else "video_preview"
            media_key = gateway.download_to_storage(
                video_url,
                media_subdir,
                "mp4",
                max_bytes=int(settings.video_download_max_bytes),
                timeout_seconds=int(settings.video_download_timeout_seconds),
                allowed_content_types=("video/", "application/octet-stream", "binary/octet-stream"),
                progress_callback=mark_download_progress,
                low_speed_timeout_seconds=int(settings.video_download_low_speed_timeout_seconds),
                low_speed_min_bytes_per_second=int(settings.video_download_low_speed_bytes_per_second),
            )
            mark_download_progress()
            written_keys.append(media_key)
            local = storage.public_url(media_key)
            media_meta = video_media_meta(media_key, int(params.get("duration", 0)) or None)
        except SoftTimeLimitExceeded:
            raise
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"视频结果下载落盘失败: {e}") from e
        # Reject non-video content: a gateway returning an HTML error page or a
        # corrupt file would otherwise be saved + charged as a "video". Real
        # video mode requires ffprobe so this check cannot silently disappear on
        # a misbuilt worker image.
        if not settings.effective_video_mock and not video_frames.FFPROBE:
            unlink_keys(written_keys)
            raise VideoResultValidationError("服务器缺少 ffprobe,无法校验视频结果")
        if video_frames.FFPROBE and not media_meta.get("width"):
            unlink_keys(written_keys)
            raise VideoResultValidationError("视频网关返回的不是有效视频(无视频流)")
        if task.stage == "final":
            # gate full video behind unlock; poster = the source first frame
            hd_url = local
            preview_url = localize_video_poster(db, task, video_poster_url(task, params), written_keys)
            if not preview_url:
                # pure text-to-video has no reference poster -> grab the rendered
                # clip's first frame so the locked asset isn't blank in the UI.
                poster = video_frames.extract_poster(str(storage.local_path(media_key)))
                if poster:
                    poster_key = storage.save_bytes(poster, "preview", "jpg")
                    written_keys.append(poster_key)
                    preview_url = storage.public_url(poster_key)
            if not preview_url:
                # Last-resort public poster. Never expose the locked final video
                # URL as preview_url; /media/video_hd is intentionally not mounted.
                still = gateway.mock_video_preview_image()
                poster_key = storage.save_bytes(still, "preview", "png")
                written_keys.append(poster_key)
                preview_url = storage.public_url(poster_key)
        else:
            # preview stage is watchable for free
            preview_url = local
            hd_url = None

    db.add(
        GenAsset(
            task_id=task.id,
            user_id=task.user_id,
            type="video",
            preview_url=preview_url,
            hd_url=hd_url,
            watermarked=False,
            unlocked=True,
            width=media_meta.get("width"),
            height=media_meta.get("height"),
            duration=media_meta.get("duration"),
        )
    )
    real_cost = settlement_cost(task, model)
    real_cost = max(0, min(int(real_cost or 0), int(task.cost_frozen or 0)))
    if not claim_terminal(db, task.id, "succeeded", cost_settled=real_cost):
        db.rollback()  # another runner finalized first -> discard our row + files
        unlink_keys(written_keys)
        return
    try:
        credits.settle(
            db,
            task.user_id,
            reserved=task.cost_frozen,
            real_cost=real_cost,
            biz_ref=task.id,
            commit=False,
        )
        db.commit()
    except Exception as e:
        db.rollback()
        params = dict(task.params or {})
        params["_video_result_keys"] = list(written_keys)
        task = db.get(GenTask, task.id)
        if task:
            task.params = {**(task.params or {}), **params}
            db.commit()
        raise LocalVideoSettlementError(f"视频已落盘但本地结算失败:{e}") from e
    set_progress(task.id, 100, "succeeded")
    clear_poll_alive(task.id)
    clear_video_download_alive(task.id)
    publish_task_update(task, "succeeded")


def admin_settle_needs_review_video(
    db,
    task: GenTask,
    *,
    result_url: str | None = None,
    external_task_id: str | None = None,
    get_model_config_fn: GetModelConfig | None = None,
) -> None:
    claimed_for_reconciliation = task.status == "running" and task.phase == "reconciling"
    if task.status != NEEDS_REVIEW and not claimed_for_reconciliation:
        raise ValueError("仅待对账任务可执行成功结算")
    if task.category != "video":
        raise ValueError("仅视频任务支持补结果结算")
    model_loader = get_model_config_fn or get_model_config
    model = model_loader(db, "video")
    if not model:
        raise ValueError("未配置视频模型")
    model = model_from_snapshot(task, model)
    params = dict(task.params or {})
    if external_task_id:
        task.external_task_id = external_task_id
    task.phase = "downloading"
    task.error = None
    task.finished_at = None
    params.setdefault("_video_download_started_at", datetime.now(timezone.utc).isoformat())
    task.params = params
    db.flush()
    task_id = task.id
    locally_written_keys: list[str] = []
    try:
        if result_url:
            finalize_video_success(db, task, model, {"status": "succeeded", "url": result_url})
        else:
            locally_written_keys = persist_local_video_result_asset(
                db,
                task,
                _local_video_result_keys(task),
            )
            real_cost = settlement_cost(task, model)
            if not claim_terminal(db, task.id, "succeeded", cost_settled=real_cost):
                db.rollback()
                unlink_keys(locally_written_keys)
                return
            credits.settle(
                db,
                task.user_id,
                reserved=task.cost_frozen,
                real_cost=real_cost,
                biz_ref=task.id,
                commit=False,
            )
            db.commit()
            set_progress(task.id, 100, "succeeded")
            publish_task_update(task, "succeeded")
    except Exception as e:
        db.rollback()
        unlink_keys(locally_written_keys)
        task = db.get(GenTask, task_id)
        if task is not None and task.status not in TERMINAL:
            task.status = NEEDS_REVIEW
            task.phase = "reconciling"
            task.error = f"补结果结算失败:{str(e)[:900]}"
            task.finished_at = None
            db.commit()
            set_progress(task.id, 100, NEEDS_REVIEW)
            publish_task_update(task, NEEDS_REVIEW)
        raise
