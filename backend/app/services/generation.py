"""Generation orchestration (runs inside Celery workers).

Image: gateway returns N images -> we save HD + a watermarked low-res preview
per image, settle the frozen estimate against real cost.
Video: submit async job -> poll to completion -> save preview (and HD for final
stage). Idempotent: a task that already reached a terminal state is skipped.
Failures refund the frozen credits only when the provider state is known. Video
submits with an unknown upstream state are held for admin reconciliation.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select, update

from ..config import settings
from ..db import SessionLocal
from ..models import GenAsset, GenTask
from . import credits, gateway, locks, storage, usage, video_frames
from .config_store import get_model_config, get_setting
from .generation_media import closest_image_size as _closest_image_size
from .generation_media import final_prompt as _final_prompt
from .generation_media import gateway_reference_image as _gateway_reference_image
from .generation_media import gateway_video_first_frame as _gateway_video_first_frame
from .generation_media import localize_video_poster as _localize_video_poster
from .generation_media import reference_dimensions as _reference_dimensions
from .generation_media import video_media_meta as _video_media_meta
from .generation_media import video_poster_url as _video_poster_url
from .generation_media import video_preview_resolution as _video_preview_resolution
from .generation_media import video_ratio as _video_ratio
from .generation_media import video_target_duration as _video_target_duration
from .generation_media import video_target_resolution as _video_target_resolution
from .generation_model_runtime import (
    ModelSnapshotMismatchError,
    assert_model_snapshot_compatible,
    model_snapshot,
)
from .generation_model_runtime import (
    find_video_by_request_id_with_model_config as _find_video_by_request_id_with_model_config,
)
from .generation_model_runtime import gen_image_with_model_config as _gen_image_with_model_config
from .generation_model_runtime import model_from_snapshot as _model_from_snapshot
from .generation_model_runtime import poll_video_with_model_config as _poll_video_with_model_config
from .generation_model_runtime import (
    submit_video_with_model_config as _submit_video_with_model_config,
)
from .generation_state import (
    NEEDS_REVIEW,
    VideoResultValidationError,
    claim_terminal,
    is_terminal_status,
)
from .generation_state import TERMINAL_STATUSES as _TERMINAL
from .generation_video_flow import POLL_MAX_CONSEC_ERRORS as _POLL_MAX_CONSEC_ERRORS
from .generation_video_flow import VIDEO_DOWNLOAD_LIVENESS_TTL as _VIDEO_DOWNLOAD_LIVENESS_TTL
from .generation_video_flow import VIDEO_DOWNLOAD_MAX_ATTEMPTS as _VIDEO_DOWNLOAD_MAX_ATTEMPTS
from .generation_video_flow import VIDEO_POLL_INTERVAL, VIDEO_POLL_MAX_SECONDS
from .generation_video_flow import aware as _aware
from .generation_video_flow import bump_poll_errors as _bump_poll_errors
from .generation_video_flow import download_lock_key as _download_lock_key
from .generation_video_flow import download_result_from_task as _download_result_from_task
from .generation_video_flow import enqueue_poll as _enqueue_poll
from .generation_video_flow import enqueue_video_download as _enqueue_video_download
from .generation_video_flow import has_video_download_result as _has_video_download_result
from .generation_video_flow import mark_poll_alive as _mark_poll_alive
from .generation_video_flow import mark_video_download_alive as _mark_video_download_alive
from .generation_video_flow import persist_video_download_result as _persist_video_download_result
from .generation_video_flow import poll_chain_alive as _poll_chain_alive
from .generation_video_flow import reset_poll_errors as _reset_poll_errors
from .generation_video_flow import unlink_keys as _unlink_keys
from .generation_video_flow import video_download_alive as _video_download_alive
from .model_pricing import estimate_credits_from_usage, usage_from_response
from .progress import set_progress
from .watermark import image_ext, make_image_preview, make_model_reference

log = logging.getLogger("generation")
VIDEO_FIRST_FRAME_MIN_SIDE = 300
VIDEO_FIRST_FRAME_MAX_SIDE = 768

__all__ = ["ModelSnapshotMismatchError", "NEEDS_REVIEW", "TaskLockedError",
           "admin_settle_needs_review_task", "admin_settle_needs_review_video",
           "assert_model_snapshot_compatible",
           "claim_terminal", "is_terminal_status", "model_snapshot", "poll_video_once",
           "resume_stuck_videos", "run_image_task", "run_video_download_task",
           "start_video_task"]


class TaskLockedError(RuntimeError):
    pass


def _try_enqueue_poll(task_id: int) -> None:
    try:
        _enqueue_poll(task_id)
    except Exception:
        log.exception("video poll enqueue failed for task %s", task_id)


def _try_enqueue_video_download(db, task_id: int, *, countdown: int = 0) -> None:
    try:
        _enqueue_video_download(task_id, countdown=countdown)
    except Exception as e:  # noqa: BLE001
        _hold_video_download_for_reconciliation(db, task_id, str(e))


def _hold_image_success_for_reconciliation(
    db,
    task_id: int,
    error: str,
    *,
    written_keys: list[str] | None = None,
    saved_count: int | None = None,
) -> None:
    """Hold provider-success image tasks when only local accounting failed."""
    db.rollback()
    task = db.get(GenTask, task_id)
    if not task:
        return
    params = dict(task.params or {})
    if written_keys:
        params["_image_result_keys"] = list(written_keys)
    if saved_count is not None:
        params["_saved_n"] = int(saved_count)
    task.params = params
    task.phase = "reconciling"
    message = (
        "图片已由上游生成,但本地落账失败,已冻结额度等待人工对账。"
        f"saved_n={saved_count if saved_count is not None else 'unknown'}; "
        f"error={error[:500]}"
    )
    if not claim_terminal(db, task_id, NEEDS_REVIEW, error=message):
        db.rollback()
        return
    db.commit()
    set_progress(task_id, 100, NEEDS_REVIEW)


def _public_image_error(exc: Exception) -> str:
    message = str(exc)
    lowered = message.lower()
    if "timed out" in lowered or "timeout" in lowered or "超时" in message:
        return (
            "图片生成等待超时，已退回冻结积分。大图或高峰期可能需要更久；"
            "请稍后重试，或先降低分辨率/张数。"
        )
    if "没有可用账号" in message or "No available compatible accounts" in message:
        return (
            "图片网关当前没有可用账号支持该模型/参数组合，已退回冻结积分。"
            "请稍后重试或在后台切换图像模型/网关账号。"
        )
    if "images[].image_url" in message or "unknown parameter" in lowered or "invalid_request_error" in lowered:
        return (
            "图片网关参数不匹配，已退回冻结积分。"
            "请检查后台图像模型配置或联系管理员处理。"
        )
    return "图片生成失败，已退回冻结积分，请稍后重试"


def _image_submit_state_unknown(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return False
    return _submit_state_unknown(exc)


def _settlement_cost(task: GenTask, model, *, image_count: int | None = None) -> int:
    """Settled *credit* cost — must match what credits.settle can charge out of
    the frozen estimate. This is the internal credit price (image scales with n,
    video preview is cheap), NOT the provider's real $ cost; real per-call usage
    is logged separately in gateway_calls."""
    base = max(0, int(model.cost_credits or 0))
    if task.category == "video" and task.stage == "preview":
        try:
            base = int((model.extra or {}).get("preview_cost", max(1, base // 10)))
        except (TypeError, ValueError):
            base = max(1, base // 10)
    elif task.category == "image":
        n = int(image_count if image_count is not None else (task.params or {}).get("n") or 1)
        base = base * max(1, n)
    return max(0, min(base, int(task.cost_frozen or 0)))



def run_image_task(task_id: int) -> None:
    lock_key = f"gen:lock:{task_id}"
    lock_token = locks.acquire(lock_key)
    if not lock_token:
        log.warning("image task %s locked by another worker, retry later", task_id)
        raise TaskLockedError(f"image task {task_id} locked")
    db = SessionLocal()
    try:
        task = db.get(GenTask, task_id)
        if not task or task.status in _TERMINAL:
            return
        task.status = "running"
        db.commit()
        set_progress(task_id, 10, "running")

        model = get_model_config(db, "image")
        if not model or not model.enabled:
            raise RuntimeError("未配置可用的图像模型")
        model = _model_from_snapshot(task, model)

        n = int((task.params or {}).get("n") or get_setting(db, "image_n", 4))
        params = task.params or {}
        fallback_size = get_setting(db, "image_size", "1024x1024")
        ref_w, ref_h = _reference_dimensions(task)
        size = params.get("size") or _closest_image_size(ref_w, ref_h, fallback_size)
        prompt = _final_prompt(task)

        set_progress(task_id, 30, "running")
        # reverse-off (image+instruction -> image): pass the reference image to
        # the edit endpoint if one is configured on the image model.
        ref = None
        if (task.prompt or {}).get("instruction") and task.source_type == "image":
            ref = _gateway_reference_image(db, task, task.source_asset_url)
        # reverse-off (图+指令→图): default to the configured edit endpoint so the
        # reference image is actually used; admin can set extra.edit_path="" to off.
        edit_path = (model.extra or {}).get("edit_path", settings.image_edit_path) or None
        extra_payload = {
            "seed": params.get("seed"),
            "negative_prompt": params.get("negative_prompt") or params.get("negative"),
        }
        mode = "edit" if (ref and edit_path) else "txt2img"
        t0 = time.time()
        try:
            images = _gen_image_with_model_config(
                model,
                prompt,
                n=n,
                size=size,
                reference_image_url=ref,
                edit_path=edit_path,
                extra_payload=extra_payload,
            )
            if not images:
                raise RuntimeError("图片网关未返回任何结果")
        except Exception as e:  # noqa: BLE001
            usage.record_call(db, kind="image", model_id=model.model_id,
                              user_id=task.user_id, task_id=task.id, status="failed",
                              latency_ms=int((time.time() - t0) * 1000),
                              detail={"n": n, "size": size, "mode": mode,
                                      "error": str(e)[:300]})
            if _image_submit_state_unknown(e):
                _hold_image_submit_for_reconciliation(db, task_id, str(e))
                return
            raise
        gateway_failure_items = list(getattr(images, "failures", []) or [])
        gateway_unknown_failures = [
            failure
            for failure in gateway_failure_items
            if getattr(failure, "submit_state_unknown", False)
        ]
        gateway_failures = [
            failure.message
            for failure in gateway_failure_items
            if getattr(failure, "message", "")
        ]
        usage.record_call(db, kind="image", model_id=model.model_id,
                          user_id=task.user_id, task_id=task.id, status="ok",
                          latency_ms=int((time.time() - t0) * 1000),
                          detail={
                              "n": n,
                              "actual_n": len(images),
                              "size": size,
                              "mode": mode,
                              **({"errors": gateway_failures[:5]} if gateway_failures else {}),
                          })

        set_progress(task_id, 70, "running")
        written_keys: list[str] = []
        saved_count = 0
        image_errors: list[str] = []
        for raw in images:
            try:
                preview_png, hd_w, hd_h = make_image_preview(
                    raw,
                    max_pixels=int(settings.generated_image_max_pixels),
                )
                model_ref_jpeg, _, _ = make_model_reference(
                    raw,
                    max_pixels=int(settings.generated_image_max_pixels),
                )
                hd_key = storage.save_bytes(raw, "hd", image_ext(raw))
                pv_key = storage.save_bytes(preview_png, "preview", "png")
                model_ref_key = storage.save_bytes_named(
                    model_ref_jpeg,
                    "model_ref",
                    pv_key.split("/", 1)[1].rsplit(".", 1)[0] + ".jpg",
                )
                written_keys += [hd_key, pv_key, model_ref_key]
                db.add(
                    GenAsset(
                        task_id=task.id,
                        user_id=task.user_id,
                        type="image",
                        preview_url=storage.public_url(pv_key),
                        hd_url=storage.public_url(hd_key),
                        watermarked=True,
                        unlocked=False,
                        width=hd_w,
                        height=hd_h,
                    )
                )
                saved_count += 1
            except Exception as e:  # noqa: BLE001
                image_errors.append(str(e)[:160])
                log.warning("image task %s skipped one invalid image: %s", task_id, e)
        if saved_count <= 0:
            raise RuntimeError("图片网关返回结果均无法解析")
        if saved_count < n and gateway_unknown_failures:
            _hold_image_success_for_reconciliation(
                db,
                task_id,
                "图片批量生成部分槽位提交状态未知:"
                + "; ".join(f.message for f in gateway_unknown_failures[:3]),
                written_keys=written_keys,
                saved_count=saved_count,
            )
            return

        # finalize atomically: only the runner that claims the terminal status
        # settles, so a duplicate/raced run can't double-charge or double-credit.
        real_cost = _settlement_cost(task, model, image_count=saved_count)
        skipped_n = max(0, n - saved_count)
        partial_errors = (gateway_failures if saved_count < n else []) + image_errors
        partial_detail = (
            {
                "requested_n": n,
                "saved_n": saved_count,
                "skipped_n": skipped_n,
                "errors": partial_errors[:5],
            }
            if saved_count < n or image_errors else None
        )
        if partial_detail:
            task.params = {
                **(task.params or {}),
                "_partial": True,
                "_requested_n": n,
                "_saved_n": saved_count,
                "_skipped_n": skipped_n,
                "_partial_errors": partial_errors[:5],
            }
        if not claim_terminal(db, task_id, "succeeded", cost_settled=real_cost):
            db.rollback()  # another runner finalized -> discard our row + files
            _unlink_keys(written_keys)
            return
        try:
            credits.settle(db, task.user_id, reserved=task.cost_frozen,
                           real_cost=real_cost, biz_ref=task.id, commit=False)
            db.commit()
        except Exception as e:  # noqa: BLE001
            _hold_image_success_for_reconciliation(
                db,
                task_id,
                f"图片本地结算失败:{e}",
                written_keys=written_keys,
                saved_count=saved_count,
            )
            raise
        if partial_detail:
            usage.record_call(db, kind="image", model_id=model.model_id,
                              user_id=task.user_id, task_id=task.id, status="ok",
                              detail=partial_detail)
        set_progress(task_id, 100, "succeeded")
    except Exception as e:  # noqa: BLE001
        log.exception("image task %s failed", task_id)
        current = db.get(GenTask, task_id)
        if current and current.status == NEEDS_REVIEW:
            return
        _fail_and_refund(db, task_id, str(e), public_error=_public_image_error(e))
    finally:
        db.close()
        locks.release(lock_key, lock_token)


def _video_submit_params(db, task: GenTask) -> dict:
    """Stage-aware effective gateway params for a video render."""
    params = dict(task.params or {})
    target_resolution = _video_target_resolution(params)
    target_duration = _video_target_duration(params)
    # preview = cheap/short low-res render; final = selected quality
    if task.stage == "preview":
        params["target_resolution"] = target_resolution
        params["target_duration"] = target_duration
        params["resolution"] = _video_preview_resolution(target_resolution)
        params["duration"] = min(target_duration, 5)
    else:
        params["target_resolution"] = target_resolution
        params["target_duration"] = target_duration
        params["resolution"] = target_resolution
        params["duration"] = target_duration
    ref_w, ref_h = _reference_dimensions(task)
    if not params.get("ratio"):
        params["ratio"] = _video_ratio(ref_w, ref_h)
    # image-to-video: every client-supplied first-frame/reference URL must be
    # resolved by this backend before it reaches the model gateway. Do not forward
    # user URLs directly, even after SSRF validation, because the gateway would
    # fetch them outside our ownership/content checks.
    first_frame = params.get("first_frame_image") or params.get("reference_image_url")
    if task.source_type == "image":
        first_frame = first_frame or task.source_asset_url
    elif task.source_type == "video" and not first_frame:
        first_frame = _gateway_video_first_frame(db, task)
    if first_frame:
        safe_ref = _gateway_reference_image(
            db,
            task,
            first_frame,
            min_side=VIDEO_FIRST_FRAME_MIN_SIDE,
            max_side=VIDEO_FIRST_FRAME_MAX_SIDE,
        )
        params["first_frame_image"] = safe_ref
        if params.get("reference_image_url"):
            params["reference_image_url"] = safe_ref
    return params


def _video_persisted_params(task: GenTask, original: dict, submitted: dict) -> dict:
    """Persist user intent, not just the low-cost preview submit envelope.

    Preview submits deliberately lower duration/resolution. If we store that
    envelope as-is, the later final render inherits 5s/480p instead of the
    selected full-quality target.
    """
    persisted = dict(original)
    target_resolution = submitted.get("target_resolution")
    target_duration = submitted.get("target_duration")
    if target_resolution:
        persisted["target_resolution"] = target_resolution
    if target_duration:
        persisted["target_duration"] = target_duration
    if task.stage == "preview":
        if target_resolution:
            persisted["resolution"] = target_resolution
        if target_duration:
            persisted["duration"] = target_duration
    else:
        if submitted.get("resolution"):
            persisted["resolution"] = submitted["resolution"]
        if submitted.get("duration"):
            persisted["duration"] = submitted["duration"]
    for key in ("ratio",):
        if submitted.get(key):
            persisted[key] = submitted[key]
    if original.get("first_frame_image"):
        persisted["first_frame_image"] = original["first_frame_image"]
    elif task.source_type == "image" and task.source_asset_url:
        persisted["first_frame_image"] = task.source_asset_url
    if task.stage != "preview":
        return persisted
    persisted["preview_resolution"] = submitted.get("resolution")
    persisted["preview_duration"] = submitted.get("duration")
    return persisted


def _finalize_or_retry_video_download(db, task: GenTask, model, result: dict) -> bool:
    """Return True when finalised; False when a recoverable download retry was queued."""
    lock_key = _download_lock_key(task.id)
    lock_token = locks.acquire(lock_key, ttl=_VIDEO_DOWNLOAD_LIVENESS_TTL)
    if not lock_token:
        log.info("video task %s download/finalize already in progress", task.id)
        return False
    _mark_video_download_alive(task.id)
    try:
        _finalize_video_success(db, task, model, result)
        return True
    except VideoResultValidationError as e:
        usage.record_call(db, kind="video_download", model_id=model.model_id,
                          user_id=task.user_id, task_id=task.id,
                          status="failed",
                          detail={"stage": task.stage,
                                  "external_task_id": task.external_task_id,
                                  "permanent": True,
                                  "error": str(e)[:300]})
        _fail_and_refund(db, task.id, str(e), public_error="视频结果下载失败，已退回冻结积分，请稍后重试")
        return True
    except Exception as e:  # noqa: BLE001
        _mark_video_download_alive(task.id)
        params = dict(task.params or {})
        attempts = int(params.get("_video_download_attempts") or 0)
        usage_detail = {
            "stage": task.stage,
            "external_task_id": task.external_task_id,
            "attempt": attempts,
            "error": str(e)[:300],
        }
        if attempts < _VIDEO_DOWNLOAD_MAX_ATTEMPTS:
            log.warning(
                "video task %s download/finalize attempt %s/%s failed: %s",
                task.id,
                attempts,
                _VIDEO_DOWNLOAD_MAX_ATTEMPTS,
                e,
            )
            usage.record_call(db, kind="video_download", model_id=model.model_id,
                              user_id=task.user_id, task_id=task.id,
                              status="failed",
                              detail=usage_detail)
            _try_enqueue_video_download(db, task.id, countdown=VIDEO_POLL_INTERVAL)
            return False
        usage_detail["permanent"] = True
        usage.record_call(db, kind="video_download", model_id=model.model_id,
                          user_id=task.user_id, task_id=task.id,
                          status="failed",
                          detail=usage_detail)
        _hold_video_download_for_reconciliation(db, task.id, str(e))
        return True
    finally:
        locks.release(lock_key, lock_token)


def _recover_unknown_submit_by_request_id(
    db,
    task: GenTask,
    model,
    original_params: dict,
    submitted_params: dict,
) -> bool:
    request_id = original_params.get("_video_request_id") or submitted_params.get("request_id")
    if not request_id:
        return False
    try:
        found = _find_video_by_request_id_with_model_config(model, str(request_id))
    except Exception as e:  # noqa: BLE001
        usage.record_call(
            db,
            kind="video_submit",
            model_id=model.model_id,
            user_id=task.user_id,
            task_id=task.id,
            status="failed",
            detail={"reconcile": True, "request_id": request_id, "error": str(e)[:300]},
        )
        return False
    if not found:
        usage.record_call(
            db,
            kind="video_submit",
            model_id=model.model_id,
            user_id=task.user_id,
            task_id=task.id,
            status="failed",
            detail={"reconcile": True, "request_id": request_id, "result": "miss"},
        )
        return False

    params = _video_persisted_params(task, original_params, submitted_params)
    result_url = found.get("url")
    if result_url:
        params["_video_result_url"] = result_url
    task.params = params
    task.external_task_id = str(found["external_task_id"])
    task.external_submitted_at = datetime.now(timezone.utc)
    task.phase = "downloading" if found.get("status") == "succeeded" and result_url else "polling"
    task.status = "running"
    db.commit()
    set_progress(task.id, 60 if task.phase == "downloading" else 30, "running")
    usage.record_call(
        db,
        kind="video_submit",
        model_id=model.model_id,
        user_id=task.user_id,
        task_id=task.id,
        status="ok",
        detail={
            "reconcile": True,
            "request_id": request_id,
            "external_task_id": task.external_task_id,
            "provider_status": found.get("status"),
        },
    )
    return True


def start_video_task(task_id: int) -> None:
    """Submit the async video render, then hand off to the non-blocking poller.

    The worker returns as soon as the job is submitted; a self-re-enqueuing poll
    task drives it to completion, so a long render never holds a worker slot.
    State (external_task_id, submit time, effective params, phase) is persisted
    so the render can be resumed from the DB even if this worker dies."""
    lock_key = f"gen:lock:{task_id}"
    lock_token = locks.acquire(lock_key)
    if not lock_token:
        log.warning("video task %s locked by another worker, retry later", task_id)
        raise TaskLockedError(f"video task {task_id} locked")
    submitted = False
    db = SessionLocal()
    try:
        task = db.get(GenTask, task_id)
        if not task or task.status in _TERMINAL:
            return
        # Idempotent under acks_late redelivery: if the external job was already
        # submitted, NEVER submit again (that would create a duplicate render and
        # overwrite the task id) — just (re)attach the poll chain and return.
        if task.external_task_id:
            if task.status != "running" or task.phase != "polling":
                task.status = "running"
                task.phase = "polling"
                db.commit()
            submitted = True
        else:
            # Atomic claim: only the runner that moves queued -> running submits,
            # so a redelivery can't double-submit and overwrite the external id.
            claimed = db.execute(
                update(GenTask)
                .where(GenTask.id == task_id, GenTask.status == "queued")
                .values(status="running", phase="submitting")
            ).rowcount
            if (claimed or 0) != 1:
                return  # not queued (already being handled / stale) -> don't submit
            db.commit()
            set_progress(task_id, 10, "running")

            model = get_model_config(db, "video")
            if not model or not model.enabled:
                raise RuntimeError("未配置可用的视频模型")
            model = _model_from_snapshot(task, model)

            prompt = _final_prompt(task)
            original_params = dict(task.params or {})
            if not original_params.get("_video_request_id"):
                original_params["_video_request_id"] = f"video-{task.id}-{uuid4().hex}"
                task.params = original_params
                db.commit()
            params = _video_submit_params(db, task)
            params.setdefault("request_id", original_params["_video_request_id"])

            submit_t0 = time.time()
            try:
                ext_id = _submit_video_with_model_config(model, prompt, params)
            except Exception as e:  # noqa: BLE001
                unknown = _submit_state_unknown(e)
                usage.record_call(db, kind="video_submit", model_id=model.model_id,
                                  user_id=task.user_id, task_id=task.id, status="failed",
                                  latency_ms=int((time.time() - submit_t0) * 1000),
                                  detail={"stage": task.stage, "resolution": params.get("resolution"),
                                          "target_resolution": params.get("target_resolution"),
                                          "duration": params.get("duration"),
                                          "target_duration": params.get("target_duration"),
                                          "ratio": params.get("ratio"),
                                          "request_id": params.get("request_id"),
                                          "submit_state_unknown": unknown,
                                          "error": str(e)[:300]})
                if unknown:
                    recovered = _recover_unknown_submit_by_request_id(
                        db,
                        task,
                        model,
                        original_params,
                        params,
                    )
                    if recovered:
                        if task.phase == "downloading" and _has_video_download_result(task):
                            _try_enqueue_video_download(db, task_id)
                        else:
                            _mark_poll_alive(task_id)
                            _try_enqueue_poll(task_id)
                        return
                    _hold_for_reconciliation(db, task_id, str(e))
                    return
                raise
            usage.record_call(db, kind="video_submit", model_id=model.model_id,
                              user_id=task.user_id, task_id=task.id, status="ok",
                              latency_ms=int((time.time() - submit_t0) * 1000),
                              detail={"stage": task.stage, "external_task_id": ext_id,
                                      "resolution": params.get("resolution"),
                                      "target_resolution": params.get("target_resolution"),
                                      "duration": params.get("duration"),
                                      "target_duration": params.get("target_duration"),
                                      "ratio": params.get("ratio")})
            # Persist the external job handle and recoverable target params. For
            # preview, keep the user's final-quality target instead of the cheap
            # preview submit envelope.
            task.params = _video_persisted_params(task, original_params, params)
            task.external_task_id = ext_id
            task.external_submitted_at = datetime.now(timezone.utc)
            task.phase = "polling"
            db.commit()
            set_progress(task_id, 30, "running")
            submitted = True
    except Exception as e:  # noqa: BLE001
        log.exception("video submit %s failed", task_id)
        if submitted or _submit_state_unknown(e):
            _hold_for_reconciliation(db, task_id, str(e))
            return
        _fail_and_refund(db, task_id, str(e), public_error="视频提交失败，已退回冻结积分，请稍后重试")
    finally:
        db.close()
        locks.release(lock_key, lock_token)

    if submitted:
        _mark_poll_alive(task_id)
        _try_enqueue_poll(task_id)


def poll_video_once(task_id: int) -> None:
    """One poll tick: re-enqueues itself until the render is terminal, finalises
    on success/failure, and refunds on a real (submit-time) render timeout."""
    db = SessionLocal()
    try:
        task = db.get(GenTask, task_id)
        if not task or task.status in _TERMINAL:
            return
        if task.category != "video" or not task.external_task_id:
            return
        model = get_model_config(db, "video")
        if not model:
            _fail_and_refund(db, task_id, "视频模型配置缺失")
            return
        model = _model_from_snapshot(task, model)

        _mark_poll_alive(task_id)  # tell the recovery beat this chain is alive

        if task.phase == "downloading" and _has_video_download_result(task):
            _try_enqueue_video_download(db, task_id)
            return

        submitted_at = _aware(task.external_submitted_at) or _aware(task.created_at)
        elapsed = ((datetime.now(timezone.utc) - submitted_at).total_seconds()
                   if submitted_at else 0)
        if elapsed > VIDEO_POLL_MAX_SECONDS:
            usage.record_call(db, kind="video_poll", model_id=model.model_id,
                              user_id=task.user_id, task_id=task.id, status="failed",
                              detail={"stage": task.stage,
                                      "external_task_id": task.external_task_id,
                                      "error": "render timeout"})
            _hold_video_timeout_for_reconciliation(
                db,
                task_id,
                f"视频渲染超过 {VIDEO_POLL_MAX_SECONDS} 秒仍未结束; "
                f"external_task_id={task.external_task_id or 'unknown'}",
            )
            return

        try:
            res = _poll_video_with_model_config(model, task.external_task_id)
        except Exception as e:  # noqa: BLE001
            if isinstance(e, gateway.GatewayError) and getattr(e, "transient", False):
                usage.record_call(db, kind="video_poll", model_id=model.model_id,
                                  user_id=task.user_id, task_id=task.id,
                                  status="failed",
                                  detail={"stage": task.stage,
                                          "external_task_id": task.external_task_id,
                                          "transient": True,
                                          "error": str(e)[:300]})
                _try_enqueue_poll(task_id)
                return
            # a few consecutive failures (e.g. a terminal 4xx / unknown task id)
            # should fail fast, not re-poll for the whole 15-min budget.
            fails = _bump_poll_errors(task_id)
            log.warning("poll error %s/%s for task %s: %s",
                        fails, _POLL_MAX_CONSEC_ERRORS, task_id, e)
            if fails >= _POLL_MAX_CONSEC_ERRORS:
                _fail_and_refund(
                    db,
                    task_id,
                    f"视频轮询连续失败: {e}",
                    public_error="视频状态查询失败，已退回冻结积分，请稍后重试",
                )
            else:
                _try_enqueue_poll(task_id)
            return
        _reset_poll_errors(task_id)
        _mark_poll_alive(task_id)  # refresh after the (possibly slow) poll too

        status = res.get("status")
        if status == "failed":
            usage.record_call(db, kind="video_poll", model_id=model.model_id,
                              user_id=task.user_id, task_id=task.id, status="failed",
                              detail={"stage": task.stage,
                                      "external_task_id": task.external_task_id,
                                      "error": str(res.get("error"))[:300]})
            _fail_and_refund(
                db,
                task_id,
                res.get("error") or "视频网关返回失败",
                public_error="视频生成失败，已退回冻结积分，请稍后重试",
            )
            return
        if status == "succeeded":
            try:
                provider_usage = usage_from_response(res)
                try:
                    usage.record_call(db, kind="video_poll", model_id=model.model_id,
                                      user_id=task.user_id, task_id=task.id, status="ok",
                                      usage=provider_usage,
                                      detail={"stage": task.stage,
                                              "external_task_id": task.external_task_id})
                except Exception:  # noqa: BLE001
                    log.exception("video poll usage record failed for task %s", task_id)
                    db.rollback()
                _persist_video_download_result(db, task, res)
                _try_enqueue_video_download(db, task_id)
            except Exception as e:  # noqa: BLE001
                log.exception("video success persistence failed for task %s", task_id)
                _hold_video_download_for_reconciliation(db, task_id, str(e))
            return

        # still queued/running -> advance progress and poll again later
        pct = min(85, 30 + int(elapsed) * 55 // max(1, VIDEO_POLL_MAX_SECONDS))
        set_progress(task_id, pct, "running")
        _try_enqueue_poll(task_id)
    except Exception as e:  # noqa: BLE001
        log.exception("poll_video_once %s failed", task_id)
        _fail_and_refund(db, task_id, str(e), public_error="视频生成失败，已退回冻结积分，请稍后重试")
    finally:
        db.close()


def run_video_download_task(task_id: int) -> None:
    """Persist a completed provider video on the dedicated download queue."""
    db = SessionLocal()
    try:
        task = db.get(GenTask, task_id)
        if not task or task.status in _TERMINAL:
            return
        if task.category != "video" or task.phase != "downloading":
            return
        model = get_model_config(db, "video")
        if not model:
            _hold_video_download_for_reconciliation(db, task_id, "视频模型配置缺失")
            return
        model = _model_from_snapshot(task, model)
        _finalize_or_retry_video_download(db, task, model, _download_result_from_task(task))
    except Exception as e:  # noqa: BLE001
        log.exception("run_video_download_task %s failed", task_id)
        _hold_video_download_for_reconciliation(db, task_id, str(e))
    finally:
        db.close()


def _finalize_video_success(db, task: GenTask, model, result: dict) -> None:
    params = dict(task.params or {})
    video_url = result.get("url") or params.get("_video_result_url")
    # Only mock mode may legitimately lack a URL; a real "succeeded" with no URL
    # is a failure (don't fabricate a placeholder and charge for it).
    if not result.get("mock") and not video_url:
        raise VideoResultValidationError("视频网关返回成功但未提供视频地址")
    if video_url:
        params["_video_result_url"] = video_url
    params["_video_download_attempts"] = int(params.get("_video_download_attempts") or 0) + 1
    task.params = params
    task.phase = "downloading"
    db.commit()
    _mark_video_download_alive(task.id)

    media_meta = {"width": None, "height": None,
                  "duration": int(params.get("duration", 0)) or None}
    written_keys: list[str] = []
    if result.get("mock") or not video_url:
        # mock / no direct url -> placeholder still so the flow is demonstrable
        still = gateway.mock_video_preview_image()
        pv_key = storage.save_bytes(still, "preview", "png")
        written_keys.append(pv_key)
        preview_url = storage.public_url(pv_key)
        hd_url = preview_url if task.stage == "final" else None
    else:
        # download + store the mp4 locally (Ark URLs expire within ~24h). If we
        # can't persist it, FAIL the task (-> refund) rather than charge for a
        # result behind an expiring URL that will soon 404.
        try:
            set_progress(task.id, 92, "running")
            _mark_video_download_alive(task.id)
            media_subdir = "video_hd" if task.stage == "final" else "video_preview"
            media_key = gateway.download_to_storage(
                video_url,
                media_subdir,
                "mp4",
                max_bytes=int(settings.video_download_max_bytes),
                timeout_seconds=int(settings.video_download_timeout_seconds),
                allowed_content_types=("video/", "application/octet-stream", "binary/octet-stream"),
            )
            _mark_video_download_alive(task.id)
            written_keys.append(media_key)
            local = storage.public_url(media_key)
            media_meta = _video_media_meta(media_key, int(params.get("duration", 0)) or None)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"视频结果下载落盘失败: {e}") from e
        # Reject non-video content: a gateway returning an HTML error page or a
        # corrupt file would otherwise be saved + charged as a "video". Real
        # video mode requires ffprobe so this check cannot silently disappear on
        # a misbuilt worker image.
        if not settings.effective_video_mock and not video_frames.FFPROBE:
            _unlink_keys(written_keys)
            raise VideoResultValidationError("服务器缺少 ffprobe,无法校验视频结果")
        if video_frames.FFPROBE and not media_meta.get("width"):
            _unlink_keys(written_keys)
            raise VideoResultValidationError("视频网关返回的不是有效视频(无视频流)")
        if task.stage == "final":
            # gate full video behind unlock; poster = the source first frame
            hd_url = local
            preview_url = _localize_video_poster(_video_poster_url(task, params), written_keys)
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

    db.add(GenAsset(
        task_id=task.id, user_id=task.user_id, type="video",
        preview_url=preview_url, hd_url=hd_url, watermarked=True,
        unlocked=(task.stage != "final"),
        width=media_meta.get("width"), height=media_meta.get("height"),
        duration=media_meta.get("duration"),
    ))
    usage_cost = estimate_credits_from_usage(getattr(model, "extra", None), usage_from_response(result))
    real_cost = usage_cost if usage_cost is not None else _settlement_cost(task, model)
    real_cost = max(0, min(int(real_cost or 0), int(task.cost_frozen or 0)))
    if not claim_terminal(db, task.id, "succeeded", cost_settled=real_cost):
        db.rollback()  # another runner finalized first -> discard our row + files
        _unlink_keys(written_keys)
        return
    try:
        credits.settle(db, task.user_id, reserved=task.cost_frozen,
                       real_cost=real_cost, biz_ref=task.id, commit=False)
        db.commit()
    except Exception:
        db.rollback()
        _unlink_keys(written_keys)
        raise
    set_progress(task.id, 100, "succeeded")


def admin_settle_needs_review_video(
    db,
    task: GenTask,
    *,
    result_url: str,
    external_task_id: str | None = None,
) -> None:
    claimed_for_reconciliation = task.status == "running" and task.phase == "reconciling"
    if task.status != NEEDS_REVIEW and not claimed_for_reconciliation:
        raise ValueError("仅待对账任务可执行成功结算")
    if task.category != "video":
        raise ValueError("仅视频任务支持补结果结算")
    model = get_model_config(db, "video")
    if not model:
        raise ValueError("未配置视频模型")
    model = _model_from_snapshot(task, model)
    params = dict(task.params or {})
    if external_task_id:
        task.external_task_id = external_task_id
    task.phase = "downloading"
    task.error = None
    task.finished_at = None
    task.params = params
    db.flush()
    task_id = task.id
    try:
        _finalize_video_success(db, task, model, {"status": "succeeded", "url": result_url})
    except Exception as e:
        db.rollback()
        task = db.get(GenTask, task_id)
        if task is not None and task.status not in _TERMINAL:
            task.status = NEEDS_REVIEW
            task.phase = "reconciling"
            task.error = f"补结果结算失败:{str(e)[:900]}"
            task.finished_at = None
            db.commit()
            set_progress(task.id, 100, NEEDS_REVIEW)
        raise


def admin_settle_needs_review_image(
    db,
    task: GenTask,
    *,
    result_url: str | None = None,
) -> None:
    claimed_for_reconciliation = task.status == "running" and task.phase == "reconciling"
    if task.status != NEEDS_REVIEW and not claimed_for_reconciliation:
        raise ValueError("仅待对账任务可执行成功结算")
    if task.category != "image":
        raise ValueError("仅图片任务支持图片补结果结算")
    model = get_model_config(db, "image")
    if not model:
        raise ValueError("未配置图片模型")
    model = _model_from_snapshot(task, model)
    params = dict(task.params or {})
    keys = [str(key) for key in (params.get("_image_result_keys") or []) if key]
    image_count = max(1, int(params.get("_saved_n") or 0))
    task.phase = "reconciling"
    task.error = None
    task.finished_at = None
    db.flush()
    task_id = task.id
    try:
        if not db.execute(select(GenAsset.id).where(GenAsset.task_id == task.id).limit(1)).first():
            if result_url:
                raw = gateway.download_bytes_limited(
                    result_url,
                    max_bytes=int(settings.generated_image_max_bytes),
                    allowed_content_types=("image/",),
                    timeout_seconds=int(settings.image_download_timeout_seconds),
                )
                preview_png, hd_w, hd_h = make_image_preview(
                    raw,
                    max_pixels=int(settings.generated_image_max_pixels),
                )
                hd_key = storage.save_bytes(raw, "hd", image_ext(raw))
                pv_key = storage.save_bytes(preview_png, "preview", "png")
                keys += [hd_key, pv_key]
                db.add(
                    GenAsset(
                        task_id=task.id,
                        user_id=task.user_id,
                        type="image",
                        preview_url=storage.public_url(pv_key),
                        hd_url=storage.public_url(hd_key),
                        watermarked=True,
                        unlocked=False,
                        width=hd_w,
                        height=hd_h,
                    )
                )
                image_count = 1
            else:
                hd_keys = [key for key in keys if key.startswith("hd/")]
                preview_keys = [key for key in keys if key.startswith("preview/")]
                if not hd_keys or not preview_keys:
                    raise ValueError("图片待对账任务缺少本地结果,请填写结果 URL")
                for idx, hd_key in enumerate(hd_keys):
                    preview_key = preview_keys[min(idx, len(preview_keys) - 1)]
                    hd_path = storage.local_path(hd_key)
                    preview_path = storage.local_path(preview_key)
                    if not hd_path.exists() or not preview_path.exists():
                        raise ValueError("图片待对账任务本地结果文件缺失,请填写结果 URL")
                    db.add(
                        GenAsset(
                            task_id=task.id,
                            user_id=task.user_id,
                            type="image",
                            preview_url=storage.public_url(preview_key),
                            hd_url=storage.public_url(hd_key),
                            watermarked=True,
                            unlocked=False,
                        )
                    )
                image_count = len(hd_keys)
        real_cost = _settlement_cost(task, model, image_count=image_count)
        if not claim_terminal(db, task.id, "succeeded", cost_settled=real_cost):
            db.rollback()
            return
        credits.settle(db, task.user_id, reserved=task.cost_frozen,
                       real_cost=real_cost, biz_ref=task.id, commit=False)
        db.commit()
        set_progress(task.id, 100, "succeeded")
    except Exception as e:  # noqa: BLE001
        db.rollback()
        task = db.get(GenTask, task_id)
        if task is not None and task.status not in _TERMINAL:
            task.status = NEEDS_REVIEW
            task.phase = "reconciling"
            task.error = f"图片补结果结算失败:{str(e)[:900]}"
            task.finished_at = None
            db.commit()
            set_progress(task.id, 100, NEEDS_REVIEW)
        raise


def admin_settle_needs_review_task(
    db,
    task: GenTask,
    *,
    result_url: str | None = None,
    external_task_id: str | None = None,
) -> None:
    if task.category == "video":
        if not result_url:
            raise ValueError("视频补结果结算需要结果 URL")
        admin_settle_needs_review_video(
            db,
            task,
            result_url=result_url,
            external_task_id=external_task_id,
        )
        return
    if task.category == "image":
        admin_settle_needs_review_image(db, task, result_url=result_url)
        return
    raise ValueError("不支持的任务类型")


def resume_stuck_videos(db) -> int:
    """Recovery: re-attach a poll to any in-flight video whose poll chain looks
    dead (liveness key expired), so a worker crash never strands a submitted
    external job. Healthy chains (alive key present) are left untouched."""
    rows = list(db.execute(
        select(GenTask).where(
            GenTask.status == "running",
            GenTask.category == "video",
            GenTask.external_task_id.isnot(None),
        )
    ).scalars())
    resumed = 0
    for t in rows:
        if _poll_chain_alive(t.id) or _video_download_alive(t.id):
            continue
        if t.phase == "downloading" and _has_video_download_result(t):
            _try_enqueue_video_download(db, t.id)
        else:
            _mark_poll_alive(t.id)
            _try_enqueue_poll(t.id)
        resumed += 1
    if resumed:
        log.info("resumed %s stuck video poll chain(s)", resumed)
    return resumed


def _hold_for_reconciliation(db, task_id: int, error: str) -> None:
    """Terminal hold for video submits where the provider may already be working.

    The user's reservation stays frozen until an admin/operator checks the
    provider side and either refunds or settles manually. This avoids turning
    client-side submit timeouts into free duplicate upstream renders.
    """
    try:
        db.rollback()
        task = db.get(GenTask, task_id)
        if not task:
            return
        params = dict(task.params or {})
        request_id = params.get("_video_request_id")
        message = (
            "视频提交状态未知,已冻结额度等待人工对账。"
            f"request_id={request_id or 'unknown'}; error={error[:500]}"
        )
        if not claim_terminal(db, task_id, NEEDS_REVIEW, error=message):
            db.rollback()
            return
        db.commit()
        set_progress(task_id, 100, NEEDS_REVIEW)
    except Exception:
        log.exception("reconciliation hold failed for task %s", task_id)
        db.rollback()


def _hold_image_submit_for_reconciliation(db, task_id: int, error: str) -> None:
    """Hold image submits whose upstream state is unknown instead of refunding.

    Image generation is non-idempotent in the configured gateway. A timeout or
    5xx can still mean the provider accepted work, so refunding immediately can
    create free duplicate upstream renders on retry.
    """
    try:
        db.rollback()
        task = db.get(GenTask, task_id)
        if not task:
            return
        task.phase = "reconciling"
        message = (
            "图片提交状态未知,已冻结额度等待人工对账。"
            f"error={error[:700]}"
        )
        if not claim_terminal(db, task_id, NEEDS_REVIEW, error=message):
            db.rollback()
            return
        db.commit()
        set_progress(task_id, 100, NEEDS_REVIEW)
    except Exception:
        log.exception("image reconciliation hold failed for task %s", task_id)
        db.rollback()


def _hold_video_timeout_for_reconciliation(db, task_id: int, error: str) -> None:
    """Hold long-running submitted videos instead of refunding immediately.

    A provider can still finish after our polling budget expires. Keeping the
    frozen reservation and requiring admin reconciliation avoids paying the
    upstream bill while also returning the user's credits.
    """
    task = db.get(GenTask, task_id)
    if not task:
        return
    task.phase = "reconciling"
    message = "视频渲染超时,已冻结额度等待人工对账。" + error[:700]
    if not claim_terminal(db, task_id, NEEDS_REVIEW, error=message):
        db.rollback()
        return
    db.commit()
    set_progress(task_id, 100, NEEDS_REVIEW)


def _hold_video_download_for_reconciliation(db, task_id: int, error: str) -> None:
    """Hold provider-success tasks when only local result persistence failed."""
    db.rollback()
    task = db.get(GenTask, task_id)
    if not task:
        return
    params = dict(task.params or {})
    message = (
        "视频已由上游生成,但结果下载落盘失败,已冻结额度等待人工对账。"
        f"external_task_id={task.external_task_id or 'unknown'}; "
        f"result_url={'present' if params.get('_video_result_url') else 'missing'}; "
        f"error={error[:500]}"
    )
    task.phase = "reconciling"
    if not claim_terminal(db, task_id, NEEDS_REVIEW, error=message):
        db.rollback()
        return
    db.commit()
    set_progress(task_id, 100, NEEDS_REVIEW)


def _submit_state_unknown(exc: Exception) -> bool:
    """True when the upstream submit may have been accepted.

    Non-idempotent submit calls use retries=0, so network timeouts, 429 and 5xx
    leave us unsure whether the provider accepted the render. Terminal 4xx
    request/auth errors are treated as definite failures and refunded.
    """
    if isinstance(exc, gateway.GatewayError):
        explicit = getattr(exc, "submit_state_unknown", None)
        if explicit is not None:
            return bool(explicit)
        status_code = getattr(exc, "status_code", None)
        if status_code is not None and status_code < 500 and status_code != 429:
            return False
        return bool(getattr(exc, "transient", False) or status_code is None or status_code >= 500)
    return True


def _fail_and_refund(db, task_id: int, error: str, *, public_error: str | None = None) -> None:
    try:
        db.rollback()  # clear any partial work from the failed attempt
        task = db.get(GenTask, task_id)
        if not task:
            return
        # Idempotent: only the runner that claims the failed transition refunds.
        # If another runner (success path / reaper / duplicate) already finalized
        # this task, rowcount is 0 and we must NOT refund again.
        if not claim_terminal(db, task_id, "failed", error=public_error or error):
            db.rollback()
            return
        if task.cost_frozen and task.cost_settled == 0:
            credits.refund(db, task.user_id, task.cost_frozen,
                           biz_ref=task.id, commit=False)
        db.commit()
        set_progress(task_id, 100, "failed")
    except Exception:
        log.exception("fail handler errored for task %s", task_id)
        db.rollback()
