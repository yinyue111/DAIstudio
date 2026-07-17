"""Video result download, persistence, and settlement helpers."""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from types import SimpleNamespace

from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy import update

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
from .generation_model_runtime import model_config_for_task, model_from_snapshot
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
from .media_sidecars import storage_bytes_for_asset_urls, storage_bytes_for_keys
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
                bytes=storage_bytes_for_asset_urls(
                    storage.public_url(preview_key),
                    storage.public_url(media_key),
                ),
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
            bytes=storage_bytes_for_asset_urls(storage.public_url(media_key)),
        )
    )
    return written_keys


def hold_video_download_for_reconciliation(
    db,
    task_id: int,
    error: str,
    *,
    expected_status: str | None = None,
    expected_phase: str | None = None,
    expected_external_task_id: str | None = None,
) -> bool:
    """Hold provider-success videos when local persistence failed."""
    db.rollback()
    task = db.get(GenTask, task_id, populate_existing=True)
    if not task:
        return False
    params = dict(task.params or {})
    return mark_needs_review(
        db,
        task_id,
        (
            "视频已由上游生成,但结果下载落盘失败,需要系统恢复或管理员确认。"
            f"external_task_id={expected_external_task_id or task.external_task_id or 'unknown'}; "
            f"result_url={'present' if params.get('_video_result_url') else 'missing'}; "
            f"error={error[:500]}"
        ),
        expected_status=expected_status,
        expected_phase=expected_phase,
        expected_external_task_id=expected_external_task_id,
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
    external_task_id: str | None = None,
    expected_status: str | None = None,
    expected_phase: str | None = None,
    enqueue_video_download_fn: EnqueueVideoDownload | None = None,
) -> None:
    enqueue = enqueue_video_download_fn or enqueue_video_download
    try:
        enqueue(
            task_id,
            countdown=countdown,
            external_task_id=external_task_id,
        )
    except Exception as e:  # noqa: BLE001
        hold_video_download_for_reconciliation(
            db,
            task_id,
            str(e),
            expected_status=expected_status,
            expected_phase=expected_phase,
            expected_external_task_id=external_task_id,
        )


def finalize_or_retry_video_download(
    db,
    task: GenTask,
    model,
    result: dict,
    *,
    expected_status: str | None = None,
    expected_phase: str | None = None,
    expected_external_task_id: str | None = None,
    enqueue_video_download_fn: EnqueueVideoDownload | None = None,
) -> bool:
    """Return True when finalised; False when a recoverable download retry was queued."""
    task_id = task.id
    task_user_id = task.user_id
    task_stage = task.stage
    observed_status = expected_status if expected_status is not None else task.status
    observed_phase = expected_phase if expected_phase is not None else task.phase
    observed_external_task_id = (
        expected_external_task_id
        if expected_external_task_id is not None
        else task.external_task_id
    )
    observed_params = dict(task.params or {})
    attempts = int(observed_params.get("_video_download_attempts") or 0) + 1
    lock_key = download_lock_key(task_id)
    lock_token = locks.acquire(lock_key, ttl=VIDEO_DOWNLOAD_LIVENESS_TTL)
    if not lock_token:
        log.info("video task %s download/finalize already in progress", task_id)
        return False
    mark_video_download_alive(task_id, observed_external_task_id)
    task._download_lock_token = lock_token
    try:
        return finalize_video_success(
            db,
            task,
            model,
            result,
            expected_status=observed_status,
            expected_phase=observed_phase,
            expected_external_task_id=observed_external_task_id,
        )
    except VideoResultValidationError as e:
        # The provider already reported a terminal success, so an invalid or
        # unverifiable local file is an accounting ambiguity, not a clean user
        # retry. Keep frozen credits for reconciliation instead of refunding.
        usage.record_call(
            db,
            kind="video_download",
            model_id=model.model_id,
            user_id=task_user_id,
            task_id=task_id,
            status="failed",
            detail={
                "stage": task_stage,
                "external_task_id": observed_external_task_id,
                "permanent": True,
                "needs_review": True,
                "error": str(e)[:300],
            },
        )
        hold_video_download_for_reconciliation(
            db,
            task_id,
            str(e),
            expected_status=observed_status,
            expected_phase=observed_phase,
            expected_external_task_id=observed_external_task_id,
        )
        return True
    except LocalVideoSettlementError as e:
        usage.record_call(
            db,
            kind="video_download",
            model_id=model.model_id,
            user_id=task_user_id,
            task_id=task_id,
            status="failed",
            detail={
                "stage": task_stage,
                "external_task_id": observed_external_task_id,
                "local_settlement": True,
                "error": str(e)[:300],
            },
        )
        hold_video_download_for_reconciliation(
            db,
            task_id,
            str(e),
            expected_status=observed_status,
            expected_phase=observed_phase,
            expected_external_task_id=observed_external_task_id,
        )
        return True
    except SoftTimeLimitExceeded:
        log.warning("video task %s download/finalize hit soft time limit; holding for review", task_id)
        hold_video_download_for_reconciliation(
            db,
            task_id,
            "视频结果下载执行超时",
            expected_status=observed_status,
            expected_phase=observed_phase,
            expected_external_task_id=observed_external_task_id,
        )
        return True
    except Exception as e:  # noqa: BLE001
        mark_video_download_alive(task_id, observed_external_task_id)
        usage_detail = {
            "stage": task_stage,
            "external_task_id": observed_external_task_id,
            "attempt": attempts,
            "error": str(e)[:300],
        }
        if attempts < VIDEO_DOWNLOAD_MAX_ATTEMPTS:
            log.warning(
                "video task %s download/finalize attempt %s/%s failed: %s",
                task_id,
                attempts,
                VIDEO_DOWNLOAD_MAX_ATTEMPTS,
                e,
            )
            usage.record_call(
                db,
                kind="video_download",
                model_id=model.model_id,
                user_id=task_user_id,
                task_id=task_id,
                status="failed",
                detail=usage_detail,
            )
            try_enqueue_video_download(
                db,
                task_id,
                countdown=VIDEO_POLL_INTERVAL,
                external_task_id=observed_external_task_id,
                expected_status=observed_status,
                expected_phase=observed_phase,
                enqueue_video_download_fn=enqueue_video_download_fn,
            )
            return False
        usage_detail["permanent"] = True
        usage_detail["needs_review"] = True
        usage.record_call(
            db,
            kind="video_download",
            model_id=model.model_id,
            user_id=task_user_id,
            task_id=task_id,
            status="failed",
            detail=usage_detail,
        )
        hold_video_download_for_reconciliation(
            db,
            task_id,
            str(e),
            expected_status=observed_status,
            expected_phase=observed_phase,
            expected_external_task_id=observed_external_task_id,
        )
        return True
    finally:
        if hasattr(task, "_download_lock_token"):
            delattr(task, "_download_lock_token")
        locks.release(lock_key, lock_token)


def run_video_download_task(
    task_id: int,
    *,
    expected_external_task_id: str | None = None,
    get_model_config_fn: GetModelConfig | None = None,
    enqueue_video_download_fn: EnqueueVideoDownload | None = None,
) -> None:
    """Persist a completed provider video on the dedicated download queue."""
    db = SessionLocal()
    expected_status = None
    expected_phase = None
    identity_captured = False
    try:
        if not expected_external_task_id:
            log.warning(
                "video download task %s missing external generation identity; ignoring delivery",
                task_id,
            )
            return
        task = db.get(GenTask, task_id)
        if not task or task.external_task_id != expected_external_task_id:
            return
        observed_status = task.status
        observed_phase = task.phase
        observed_category = task.category
        if observed_status in TERMINAL:
            return
        if observed_category != "video" or observed_phase != "downloading":
            return
        expected_status = observed_status
        expected_phase = observed_phase
        identity_captured = True
        model_loader = get_model_config_fn or get_model_config
        model = model_config_for_task(db, task, "video", model_loader)
        if not model:
            hold_video_download_for_reconciliation(
                db,
                task_id,
                "视频模型配置缺失",
                expected_status=expected_status,
                expected_phase=expected_phase,
                expected_external_task_id=expected_external_task_id,
            )
            return
        model = model_from_snapshot(task, model)
        finalize_or_retry_video_download(
            db,
            task,
            model,
            download_result_from_task(task),
            expected_status=expected_status,
            expected_phase=expected_phase,
            expected_external_task_id=expected_external_task_id,
            enqueue_video_download_fn=enqueue_video_download_fn,
        )
    except SoftTimeLimitExceeded:
        log.warning("run_video_download_task %s hit soft time limit; holding for review", task_id)
        if identity_captured:
            hold_video_download_for_reconciliation(
                db,
                task_id,
                "视频结果下载执行超时",
                expected_status=expected_status,
                expected_phase=expected_phase,
                expected_external_task_id=expected_external_task_id,
            )
    except Exception as e:  # noqa: BLE001
        log.exception("run_video_download_task %s failed", task_id)
        if identity_captured:
            hold_video_download_for_reconciliation(
                db,
                task_id,
                str(e),
                expected_status=expected_status,
                expected_phase=expected_phase,
                expected_external_task_id=expected_external_task_id,
            )
    finally:
        db.close()


def finalize_video_success(
    db,
    task: GenTask,
    model,
    result: dict,
    *,
    expected_status: str | None = None,
    expected_phase: str | None = None,
    expected_external_task_id: str | None = None,
) -> bool:
    task_id = task.id
    task_user_id = task.user_id
    task_stage = task.stage
    task_cost_frozen = task.cost_frozen
    observed_status = expected_status if expected_status is not None else task.status
    observed_phase = expected_phase if expected_phase is not None else task.phase
    observed_external_task_id = (
        expected_external_task_id
        if expected_external_task_id is not None
        else task.external_task_id
    )
    observed_params = dict(task.params or {})
    params = dict(observed_params)
    video_url = result.get("url") or params.get("_video_result_url")
    # Only mock mode may legitimately lack a URL; a real "succeeded" with no URL
    # is a failure (don't fabricate a placeholder and charge for it).
    if not result.get("mock") and not video_url:
        raise VideoResultValidationError("视频网关返回成功但未提供视频地址")
    if video_url:
        params["_video_result_url"] = video_url
    params.setdefault("_video_download_started_at", datetime.now(timezone.utc).isoformat())
    params["_video_download_attempts"] = int(params.get("_video_download_attempts") or 0) + 1
    real_cost = settlement_cost(task, model)
    real_cost = max(0, min(int(real_cost or 0), int(task_cost_frozen or 0)))
    poster_url = video_poster_url(task, params)
    poster_owner = SimpleNamespace(user_id=task_user_id)
    download_lock_token = getattr(task, "_download_lock_token", None)
    conditions = [
        GenTask.id == task_id,
        GenTask.status == observed_status,
        GenTask.params == observed_params,
    ]
    if observed_phase is None:
        conditions.append(GenTask.phase.is_(None))
    else:
        conditions.append(GenTask.phase == observed_phase)
    if observed_external_task_id is None:
        conditions.append(GenTask.external_task_id.is_(None))
    else:
        conditions.append(GenTask.external_task_id == observed_external_task_id)
    transitioned = db.execute(
        update(GenTask)
        .where(*conditions)
        .values(params=params, phase="downloading")
        .execution_options(synchronize_session=False)
    ).rowcount
    if (transitioned or 0) != 1:
        db.rollback()
        return False
    db.commit()
    mark_video_download_alive(task_id, observed_external_task_id)

    fallback_duration = int(params.get("duration", 0)) or None
    media_meta = {"width": None, "height": None, "duration": fallback_duration}
    written_keys: list[str] = []
    if result.get("mock") or not video_url:
        media_subdir = "video_hd" if task_stage == "final" else "video_preview"
        media_key = storage.save_bytes(gateway.mock_video(), media_subdir, "mp4")
        written_keys.append(media_key)
        local = storage.public_url(media_key)
        media_meta = video_media_meta(media_key, fallback_duration)
        if not media_meta.get("width"):
            media_meta = {"width": 640, "height": 360, "duration": 1}
        if task_stage == "final":
            still = gateway.mock_video_preview_image()
            poster_key = storage.save_bytes(still, "preview", "png")
            written_keys.append(poster_key)
            preview_url = storage.public_url(poster_key)
            hd_url = local
        else:
            preview_url = local
            hd_url = None
    else:
        # Download + store the mp4 locally (Ark URLs expire within ~24h). If we
        # can't persist it, hold the task for reconciliation rather than charging
        # for a result behind an expiring URL that will soon 404.
        try:
            set_progress(task_id, 92, "running")

            def mark_download_progress() -> None:
                mark_video_download_alive(task_id, observed_external_task_id)
                locks.refresh(
                    download_lock_key(task_id),
                    download_lock_token,
                    VIDEO_DOWNLOAD_LIVENESS_TTL,
                )

            mark_download_progress()
            media_subdir = "video_hd" if task_stage == "final" else "video_preview"
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
        if task_stage == "final":
            # gate full video behind unlock; poster = the source first frame
            hd_url = local
            preview_url = localize_video_poster(
                db,
                poster_owner,
                poster_url,
                written_keys,
            )
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
            task_id=task_id,
            user_id=task_user_id,
            type="video",
            preview_url=preview_url,
            hd_url=hd_url,
            watermarked=False,
            unlocked=True,
            width=media_meta.get("width"),
            height=media_meta.get("height"),
            duration=media_meta.get("duration"),
            bytes=storage_bytes_for_keys(written_keys),
        )
    )
    claimed_terminal = False
    try:
        claimed_terminal = claim_terminal(
            db,
            task_id,
            "succeeded",
            cost_settled=real_cost,
            expected_status=observed_status,
            expected_phase=observed_phase,
            expected_external_task_id=observed_external_task_id,
        )
        if not claimed_terminal:
            db.rollback()
            unlink_keys(written_keys)
            return False
        credits.settle(
            db,
            task_user_id,
            reserved=task_cost_frozen,
            real_cost=real_cost,
            biz_ref=task_id,
            commit=False,
        )
        db.commit()
    except Exception as e:
        db.rollback()
        if not claimed_terminal:
            unlink_keys(written_keys)
            raise
        fallback_params = {**params, "_video_result_keys": list(written_keys)}
        fallback_conditions = [
            GenTask.id == task_id,
            GenTask.status == observed_status,
            GenTask.params == params,
        ]
        if observed_phase is None:
            fallback_conditions.append(GenTask.phase.is_(None))
        else:
            fallback_conditions.append(GenTask.phase == observed_phase)
        if observed_external_task_id is None:
            fallback_conditions.append(GenTask.external_task_id.is_(None))
        else:
            fallback_conditions.append(
                GenTask.external_task_id == observed_external_task_id
            )
        restored = db.execute(
            update(GenTask)
            .where(*fallback_conditions)
            .values(params=fallback_params)
            .execution_options(synchronize_session=False)
        ).rowcount
        if (restored or 0) == 1:
            db.commit()
        else:
            db.rollback()
            unlink_keys(written_keys)
        raise LocalVideoSettlementError(f"视频已落盘但本地结算失败:{e}") from e
    set_progress(task_id, 100, "succeeded")
    clear_poll_alive(task_id, observed_external_task_id)
    clear_video_download_alive(task_id, observed_external_task_id)
    completed_task = db.get(GenTask, task_id, populate_existing=True)
    if completed_task is not None:
        publish_task_update(completed_task, "succeeded")
    return True


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
    model = model_config_for_task(db, task, "video", model_loader)
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
