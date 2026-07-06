"""Video submit and polling orchestration."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from uuid import uuid4

from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy import update

from ..db import SessionLocal
from ..models import GenTask
from . import gateway, locks, usage
from .config_store import get_model_config
from .generation_common import (
    TaskCanceled,
    TaskLockedError,
    cancel_and_refund,
    fail_and_refund,
    mark_needs_review,
    raise_if_cancel_requested,
)
from .generation_media import (
    final_prompt,
    gateway_reference_image,
    gateway_video_first_frame,
    reference_dimensions,
    video_preview_resolution,
    video_ratio,
    video_target_duration,
    video_target_resolution,
)
from .generation_model_runtime import (
    ModelSnapshotMismatchError,
    find_video_by_request_id_with_model_config,
    model_from_snapshot,
    poll_video_with_model_config,
    submit_video_with_model_config,
)
from .generation_prompts import generation_prompt_for_model, product_fidelity_prompt
from .generation_state import TERMINAL_STATUSES as TERMINAL
from .generation_video_download import hold_video_download_for_reconciliation
from .generation_video_flow import (
    POLL_MAX_CONSEC_ERRORS,
    VIDEO_POLL_MAX_SECONDS,
    aware,
    bump_poll_errors,
    enqueue_poll,
    enqueue_video_download,
    has_video_download_result,
    mark_poll_alive,
    persist_video_download_result,
    reset_poll_errors,
)
from .model_pricing import usage_from_response
from .progress import set_progress

log = logging.getLogger("generation")
VIDEO_FIRST_FRAME_MIN_SIDE = 300
VIDEO_FIRST_FRAME_MAX_SIDE = 768


def _enqueue_poll_safely(task_id: int) -> None:
    try:
        enqueue_poll(task_id)
    except Exception:
        log.exception("video poll enqueue failed for task %s", task_id)


def _enqueue_video_download_safely(db, task_id: int, *, countdown: int = 0) -> None:
    try:
        enqueue_video_download(task_id, countdown=countdown)
    except Exception as e:  # noqa: BLE001
        hold_video_download_for_reconciliation(db, task_id, str(e))


def hold_video_submit_unknown_for_reconciliation(
    db,
    task_id: int,
    error: str,
    *,
    params_update: dict | None = None,
) -> None:
    """Hold video submits that may already be accepted upstream."""
    db.rollback()
    task = db.get(GenTask, task_id)
    if not task:
        return
    params = dict(task.params or {})
    if params_update:
        params.update(params_update)
    params["_video_submit_state_unknown"] = True
    mark_needs_review(
        db,
        task_id,
        (
            "视频提交状态未知,上游可能已接受任务,冻结积分暂不退回。"
            "请通过外部任务结果补结果结算,或确认未生成后人工退款。"
            f"request_id={params.get('_video_request_id') or params.get('request_id') or 'unknown'}; "
            f"error={error[:500]}"
        ),
        params_update=params,
    )


def hold_video_poll_for_reconciliation(db, task_id: int, error: str) -> None:
    """Hold submitted video tasks when provider status cannot be trusted locally."""
    db.rollback()
    task = db.get(GenTask, task_id)
    if not task:
        return
    params = dict(task.params or {})
    params["_video_poll_state_unknown"] = True
    mark_needs_review(
        db,
        task_id,
        (
            "视频上游任务状态未知,冻结积分暂不退回。"
            "请继续查询外部任务结果补结果结算,或确认上游未生成后人工退款。"
            f"external_task_id={task.external_task_id or 'unknown'}; "
            f"error={error[:500]}"
        ),
        params_update=params,
        rollback=False,
    )


def video_submit_params(db, task: GenTask) -> dict:
    """Stage-aware effective gateway params for a video render."""
    params = dict(task.params or {})
    target_resolution = video_target_resolution(params)
    target_duration = video_target_duration(params)
    # preview = cheap/short low-res render; final = selected quality
    if task.stage == "preview":
        params["target_resolution"] = target_resolution
        params["target_duration"] = target_duration
        params["resolution"] = video_preview_resolution(target_resolution)
        params["duration"] = min(target_duration, 5)
    else:
        params["target_resolution"] = target_resolution
        params["target_duration"] = target_duration
        params["resolution"] = target_resolution
        params["duration"] = target_duration
    ref_w, ref_h = reference_dimensions(task)
    if not params.get("ratio"):
        params["ratio"] = video_ratio(ref_w, ref_h)
    # image-to-video: every client-supplied first-frame/reference URL must be
    # resolved by this backend before it reaches the model gateway.
    first_frame = params.get("first_frame_image") or params.get("reference_image_url")
    if task.source_type == "image":
        first_frame = first_frame or task.source_asset_url
    elif task.source_type == "video" and not first_frame:
        first_frame = gateway_video_first_frame(db, task)
    if first_frame:
        safe_ref = gateway_reference_image(
            db,
            task,
            first_frame,
            min_side=VIDEO_FIRST_FRAME_MIN_SIDE,
            max_side=VIDEO_FIRST_FRAME_MAX_SIDE,
        )
        params["first_frame_image"] = safe_ref
        if params.get("reference_image_url"):
            params["reference_image_url"] = safe_ref
        last_frame = params.get("last_frame_image")
        subject_mode = str(params.get("subject_mode") or "").lower()
        product_lock_mode = str(params.get("product_lock_mode") or "locked").lower()
        should_auto_lock_last_frame = (
            task.source_type == "image"
            and not last_frame
            and (subject_mode == "portrait" or product_lock_mode != "free")
        )
        if should_auto_lock_last_frame:
            params["last_frame_image"] = safe_ref
            if subject_mode == "portrait":
                params["_portrait_locked"] = True
            else:
                params["_product_locked"] = True
        elif last_frame:
            params["last_frame_image"] = gateway_reference_image(
                db,
                task,
                last_frame,
                min_side=VIDEO_FIRST_FRAME_MIN_SIDE,
                max_side=VIDEO_FIRST_FRAME_MAX_SIDE,
            )
    character_ref = params.get("character_reference_image")
    if character_ref:
        safe_character_ref = gateway_reference_image(
            db,
            task,
            character_ref,
            min_side=VIDEO_FIRST_FRAME_MIN_SIDE,
            max_side=VIDEO_FIRST_FRAME_MAX_SIDE,
        )
        params["character_reference_image"] = safe_character_ref
        if str(params.get("subject_mode") or "").lower() == "portrait":
            params.setdefault("first_frame_image", safe_character_ref)
            params.setdefault("reference_image_url", safe_character_ref)
            params.setdefault("last_frame_image", safe_character_ref)
            params["_portrait_locked"] = True
    return params


def video_persisted_params(task: GenTask, original: dict, submitted: dict) -> dict:
    """Persist user intent, not just the low-cost preview submit envelope."""
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
    if original.get("last_frame_image"):
        persisted["last_frame_image"] = original["last_frame_image"]
    elif task.source_type == "image" and task.source_asset_url:
        persisted["last_frame_image"] = task.source_asset_url
    if original.get("character_reference_image"):
        persisted["character_reference_image"] = original["character_reference_image"]
    for key in ("subject_mode",):
        if original.get(key):
            persisted[key] = original[key]
    if task.stage != "preview":
        return persisted
    persisted["preview_resolution"] = submitted.get("resolution")
    persisted["preview_duration"] = submitted.get("duration")
    return persisted


def recover_unknown_submit_by_request_id(
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
        found = find_video_by_request_id_with_model_config(model, str(request_id))
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

    params = video_persisted_params(task, original_params, submitted_params)
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


def submit_state_unknown(exc: Exception) -> bool:
    """True when the upstream submit may have been accepted."""
    if isinstance(exc, gateway.GatewayError):
        explicit = getattr(exc, "submit_state_unknown", None)
        if explicit is not None:
            return bool(explicit)
        status_code = getattr(exc, "status_code", None)
        if status_code is not None:
            return status_code == 429 or 500 <= int(status_code) < 600
        return bool(getattr(exc, "transient", False))
    return True


def start_video_task(
    task_id: int,
    *,
    get_model_config_fn=None,
    submit_video_fn=None,
    try_enqueue_poll_fn=None,
    try_enqueue_video_download_fn=None,
) -> None:
    """Submit the async video render, then hand off to the non-blocking poller."""
    model_loader = get_model_config_fn or get_model_config
    submitter = submit_video_fn or submit_video_with_model_config
    enqueue_poll_fn = try_enqueue_poll_fn or _enqueue_poll_safely
    enqueue_download_fn = try_enqueue_video_download_fn or _enqueue_video_download_safely
    lock_key = f"gen:lock:{task_id}"
    lock_token = locks.acquire(lock_key)
    if not lock_token:
        log.warning("video task %s locked by another worker, retry later", task_id)
        raise TaskLockedError(f"video task {task_id} locked")
    submitted = False
    db = SessionLocal()
    try:
        task = db.get(GenTask, task_id)
        if not task or task.status in TERMINAL:
            return
        raise_if_cancel_requested(db, task)
        if task.external_task_id:
            if task.status != "running" or task.phase != "polling":
                task.status = "running"
                task.phase = "polling"
                db.commit()
            submitted = True
        else:
            claimed = db.execute(
                update(GenTask)
                .where(GenTask.id == task_id, GenTask.status == "queued")
                .values(status="running", phase="submitting")
            ).rowcount
            if (claimed or 0) != 1:
                return
            db.commit()
            set_progress(task_id, 10, "running")
            task = db.get(GenTask, task_id)
            raise_if_cancel_requested(db, task)

            model = model_loader(db, "video")
            if not model or not model.enabled:
                raise RuntimeError("未配置可用的视频模型")
            model = model_from_snapshot(task, model)

            prompt = final_prompt(task)
            prompt = generation_prompt_for_model(prompt, task)
            prompt = product_fidelity_prompt(prompt, task)
            original_params = dict(task.params or {})
            if not original_params.get("_video_request_id"):
                original_params["_video_request_id"] = f"video-{task.id}-{uuid4().hex}"
                task.params = original_params
                db.commit()
            params = video_submit_params(db, task)
            params.setdefault("request_id", original_params["_video_request_id"])
            raise_if_cancel_requested(db, db.get(GenTask, task_id))

            submit_t0 = time.time()
            try:
                ext_id = submitter(model, prompt, params)
            except Exception as e:  # noqa: BLE001
                unknown = submit_state_unknown(e)
                usage.record_call(
                    db,
                    kind="video_submit",
                    model_id=model.model_id,
                    user_id=task.user_id,
                    task_id=task.id,
                    status="failed",
                    latency_ms=int((time.time() - submit_t0) * 1000),
                    detail={
                        "stage": task.stage,
                        "resolution": params.get("resolution"),
                        "target_resolution": params.get("target_resolution"),
                        "duration": params.get("duration"),
                        "target_duration": params.get("target_duration"),
                        "ratio": params.get("ratio"),
                        "request_id": params.get("request_id"),
                        "submit_state_unknown": unknown,
                        "error": str(e)[:300],
                    },
                )
                if unknown:
                    recovered = recover_unknown_submit_by_request_id(db, task, model, original_params, params)
                    if recovered:
                        if task.phase == "downloading" and has_video_download_result(task):
                            enqueue_download_fn(db, task_id)
                        else:
                            mark_poll_alive(task_id)
                            enqueue_poll_fn(task_id)
                        return
                    hold_video_submit_unknown_for_reconciliation(
                        db,
                        task_id,
                        str(e),
                        params_update=video_persisted_params(task, original_params, params),
                    )
                    return
                raise
            usage.record_call(
                db,
                kind="video_submit",
                model_id=model.model_id,
                user_id=task.user_id,
                task_id=task.id,
                status="ok",
                latency_ms=int((time.time() - submit_t0) * 1000),
                detail={
                    "stage": task.stage,
                    "external_task_id": ext_id,
                    "resolution": params.get("resolution"),
                    "target_resolution": params.get("target_resolution"),
                    "duration": params.get("duration"),
                    "target_duration": params.get("target_duration"),
                    "ratio": params.get("ratio"),
                },
            )
            task.params = video_persisted_params(task, original_params, params)
            task.external_task_id = ext_id
            task.external_submitted_at = datetime.now(timezone.utc)
            task.phase = "polling"
            db.commit()
            set_progress(task_id, 30, "running")
            submitted = True
    except TaskCanceled as e:
        cancel_and_refund(db, task_id, str(e))
    except Exception as e:  # noqa: BLE001
        log.exception("video submit %s failed", task_id)
        current = db.get(GenTask, task_id)
        if submitted or (current and current.external_task_id):
            mark_poll_alive(task_id)
            enqueue_poll_fn(task_id)
            return
        public_error = "视频提交失败，已退回冻结积分，请稍后重试"
        fail_and_refund(db, task_id, str(e), public_error=public_error)
    finally:
        db.close()
        locks.release(lock_key, lock_token)

    if submitted:
        mark_poll_alive(task_id)
        enqueue_poll_fn(task_id)


def poll_video_once(
    task_id: int,
    *,
    get_model_config_fn=None,
    poll_video_fn=None,
    try_enqueue_poll_fn=None,
    try_enqueue_video_download_fn=None,
    poll_max_seconds: int | None = None,
) -> None:
    """One poll tick that re-enqueues itself until the render is terminal."""
    model_loader = get_model_config_fn or get_model_config
    poller = poll_video_fn or poll_video_with_model_config
    enqueue_poll_fn = try_enqueue_poll_fn or _enqueue_poll_safely
    enqueue_download_fn = try_enqueue_video_download_fn or _enqueue_video_download_safely
    db = SessionLocal()
    try:
        task = db.get(GenTask, task_id)
        if not task or task.status in TERMINAL:
            return
        if task.category != "video" or not task.external_task_id:
            return
        model = model_loader(db, "video")
        if not model:
            hold_video_download_for_reconciliation(db, task_id, "视频模型配置缺失")
            return
        try:
            model = model_from_snapshot(task, model)
        except ModelSnapshotMismatchError as e:
            hold_video_download_for_reconciliation(db, task_id, str(e))
            return

        mark_poll_alive(task_id)

        if task.phase == "downloading" and has_video_download_result(task):
            enqueue_download_fn(db, task_id)
            return

        poll_budget = int(poll_max_seconds or VIDEO_POLL_MAX_SECONDS)
        submitted_at = aware(task.external_submitted_at) or aware(task.created_at)
        elapsed = (datetime.now(timezone.utc) - submitted_at).total_seconds() if submitted_at else 0
        if elapsed > poll_budget:
            usage.record_call(
                db,
                kind="video_poll",
                model_id=model.model_id,
                user_id=task.user_id,
                task_id=task.id,
                status="failed",
                detail={
                    "stage": task.stage,
                    "external_task_id": task.external_task_id,
                    "error": "render timeout",
                },
            )
            hold_video_poll_for_reconciliation(
                db,
                task_id,
                f"视频渲染超过 {poll_budget} 秒仍未结束; "
                f"external_task_id={task.external_task_id or 'unknown'}",
            )
            return

        try:
            res = poller(model, task.external_task_id)
        except SoftTimeLimitExceeded:
            log.warning("poll_video_once soft time limit for task %s; holding for review", task_id)
            hold_video_poll_for_reconciliation(db, task_id, "视频状态查询执行超时")
            return
        except Exception as e:  # noqa: BLE001
            is_transient_poll_error = isinstance(e, gateway.GatewayError) and getattr(e, "transient", False)
            if is_transient_poll_error:
                usage.record_call(
                    db,
                    kind="video_poll",
                    model_id=model.model_id,
                    user_id=task.user_id,
                    task_id=task.id,
                    status="failed",
                    detail={
                        "stage": task.stage,
                        "external_task_id": task.external_task_id,
                        "transient": True,
                        "error": str(e)[:300],
                    },
                )
            fails = bump_poll_errors(task_id)
            log.warning("poll error %s/%s for task %s: %s", fails, POLL_MAX_CONSEC_ERRORS, task_id, e)
            if fails >= POLL_MAX_CONSEC_ERRORS:
                hold_video_poll_for_reconciliation(
                    db,
                    task_id,
                    f"视频轮询连续失败: {e}",
                )
            else:
                enqueue_poll_fn(task_id)
            return
        reset_poll_errors(task_id)
        mark_poll_alive(task_id)

        status = res.get("status")
        if status == "failed":
            usage.record_call(
                db,
                kind="video_poll",
                model_id=model.model_id,
                user_id=task.user_id,
                task_id=task.id,
                status="failed",
                detail={
                    "stage": task.stage,
                    "external_task_id": task.external_task_id,
                    "error": str(res.get("error"))[:300],
                },
            )
            fail_and_refund(
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
                    usage.record_call(
                        db,
                        kind="video_poll",
                        model_id=model.model_id,
                        user_id=task.user_id,
                        task_id=task.id,
                        status="ok",
                        usage=provider_usage,
                        detail={"stage": task.stage, "external_task_id": task.external_task_id},
                    )
                except Exception:  # noqa: BLE001
                    log.exception("video poll usage record failed for task %s", task_id)
                    db.rollback()
                persist_video_download_result(db, task, res)
                enqueue_download_fn(db, task_id)
            except Exception as e:  # noqa: BLE001
                log.exception("video success persistence failed for task %s", task_id)
                hold_video_download_for_reconciliation(db, task_id, str(e))
            return

        pct = min(85, 30 + int(elapsed) * 55 // max(1, poll_budget))
        set_progress(task_id, pct, "running")
        enqueue_poll_fn(task_id)
    except SoftTimeLimitExceeded:
        log.warning("poll_video_once soft time limit for task %s; holding for review", task_id)
        hold_video_poll_for_reconciliation(db, task_id, "视频状态查询执行超时")
    except Exception as e:  # noqa: BLE001
        log.exception("poll_video_once %s failed", task_id)
        current = db.get(GenTask, task_id)
        if current and current.external_task_id:
            hold_video_poll_for_reconciliation(db, task_id, str(e))
        else:
            fail_and_refund(db, task_id, str(e), public_error="视频生成失败，已退回冻结积分，请稍后重试")
    finally:
        db.close()
