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
from . import credits, gateway, locks, usage
from .config_store import get_model_config
from .generation_common import (
    TaskCanceled,
    TaskLockedError,
    cancel_and_refund,
    fail_and_refund,
    mark_needs_review,
    publish_task_update,
    raise_if_cancel_requested,
)
from .generation_media import (
    final_prompt,
    gateway_reference_image,
    gateway_video_first_frame,
    reference_dimensions,
    video_preview_resolution,
    video_ratio,
    video_render_duration,
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
from .generation_prompts import (
    product_video_negative_prompt,
)
from .generation_state import TERMINAL_STATUSES as TERMINAL
from .generation_state import cancel_requested, claim_terminal
from .generation_video_download import hold_video_download_for_reconciliation
from .generation_video_flow import (
    POLL_MAX_CONSEC_ERRORS,
    VIDEO_POLL_MAX_SECONDS,
    aware,
    bump_poll_errors,
    enqueue_poll,
    enqueue_video_download,
    mark_poll_alive,
    persist_video_download_result,
    reset_poll_errors,
    video_task_action,
)
from .model_pricing import usage_from_response
from .progress import set_progress
from .video_prompt_compiler import (
    build_video_prompt_references,
    compile_video_prompt,
    store_video_prompt_compile,
)

log = logging.getLogger("generation")
VIDEO_FIRST_FRAME_MIN_SIDE = 300
VIDEO_FIRST_FRAME_MAX_SIDE = 768
PRODUCT_VIDEO_REFERENCE_MAX_SIDE = 1280


def _compile_legacy_video_prompt(task: GenTask, model, params: dict) -> tuple[str, dict]:
    """Compile retry/legacy tasks that predate request-time video compilation."""
    references = build_video_prompt_references(
        source_asset_url=task.source_asset_url,
        source_type=task.source_type,
        params=params,
    )
    extra = getattr(model, "extra", None) if isinstance(getattr(model, "extra", None), dict) else {}
    model_profiles = extra.get("video_prompt_profiles") or extra.get("prompt_profiles")
    if not isinstance(model_profiles, dict):
        model_profiles = None
    compiled = compile_video_prompt(
        task.prompt or final_prompt(task),
        duration=video_render_duration(params, task.stage),
        model_id=str(getattr(model, "model_id", "") or ""),
        provider=str(getattr(model, "provider", "") or ""),
        extra=extra,
        references=references,
        product_reference=any(item.get("role") == "product" for item in references),
        portrait_reference=any(item.get("role") == "character" for item in references),
        product_lock_mode=str(params.get("product_lock_mode") or "locked"),
        product_video_template=str(params.get("product_video_template") or "stable_showcase"),
        model_profiles=model_profiles,
    )
    if compiled.get("sequence_required"):
        raise RuntimeError("当前脚本超过所选模型和时长的单段承载能力，请拆分为多段视频后重试")

    persisted = dict(params)
    store_video_prompt_compile(persisted, compiled, references)
    return persisted["_generation_prompt"], persisted


def _enqueue_poll_safely(task_id: int, external_task_id: str | None = None) -> None:
    try:
        enqueue_poll(task_id, external_task_id)
    except Exception:
        log.exception("video poll enqueue failed for task %s", task_id)


def _enqueue_video_download_safely(
    db,
    task_id: int,
    *,
    countdown: int = 0,
    external_task_id: str | None = None,
) -> None:
    try:
        enqueue_video_download(
            task_id,
            countdown=countdown,
            external_task_id=external_task_id,
        )
    except Exception as e:  # noqa: BLE001
        hold_video_download_for_reconciliation(
            db,
            task_id,
            str(e),
            expected_status="running",
            expected_phase="downloading",
            expected_external_task_id=external_task_id,
        )


def _reload_polled_video_task(db, task_id: int, external_task_id: str):
    db.rollback()
    current = db.get(GenTask, task_id, populate_existing=True)
    if not current or current.external_task_id != external_task_id:
        return current, None
    return current, video_task_action(current)


def _handoff_video_download_best_effort(
    db,
    task_id: int,
    external_task_id: str,
    enqueue_download_fn=None,
) -> None:
    try:
        if enqueue_download_fn is None:
            enqueue_video_download(task_id, external_task_id=external_task_id)
        else:
            # Handoffs must not let an injected enqueue wrapper mutate task state
            # through its failure path. Successful wrappers do not use the DB.
            enqueue_download_fn(
                None,
                task_id,
                external_task_id=external_task_id,
            )
    except Exception:  # noqa: BLE001
        log.warning(
            "redundant video download handoff enqueue failed for task %s; recovery will retry",
            task_id,
            exc_info=True,
        )


def _hold_owned_video_download_for_reconciliation(
    db,
    task_id: int,
    external_task_id: str,
    error: str,
) -> None:
    db.rollback()
    task = db.get(GenTask, task_id, populate_existing=True)
    if not task or task.external_task_id != external_task_id:
        return
    params = dict(task.params or {})
    mark_needs_review(
        db,
        task_id,
        (
            "视频已由上游生成,但结果下载落盘失败,需要系统恢复或管理员确认。"
            f"external_task_id={external_task_id}; "
            f"result_url={'present' if params.get('_video_result_url') else 'missing'}; "
            f"error={error[:500]}"
        ),
        expected_status="running",
        expected_phase="downloading",
        expected_external_task_id=external_task_id,
    )


def _enqueue_owned_video_download(
    db,
    task_id: int,
    external_task_id: str,
    enqueue_download_fn=None,
) -> None:
    try:
        if enqueue_download_fn is None:
            enqueue_video_download(task_id, external_task_id=external_task_id)
        else:
            enqueue_download_fn(
                db,
                task_id,
                external_task_id=external_task_id,
            )
    except Exception as e:  # noqa: BLE001
        _hold_owned_video_download_for_reconciliation(
            db,
            task_id,
            external_task_id,
            str(e),
        )


def hold_video_submit_unknown_for_reconciliation(
    db,
    task_id: int,
    error: str,
    *,
    request_id: str | None = None,
    expected_params: dict | None = None,
    params_update: dict | None = None,
) -> None:
    """Hold video submits that may already be accepted upstream."""
    db.rollback()
    task = db.get(GenTask, task_id, populate_existing=True)
    observed_params = task.params if task and isinstance(task.params, dict) else {}
    expected_request_id = request_id or observed_params.get("_video_request_id")
    expected_snapshot = expected_params if expected_params is not None else observed_params
    if (
        not task
        or task.status != "running"
        or task.phase != "submitting"
        or task.external_task_id is not None
        or observed_params != expected_snapshot
        or not expected_request_id
        or str(observed_params.get("_video_request_id") or "") != str(expected_request_id)
    ):
        return
    params = dict(observed_params)
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
        rollback=False,
        expected_status="running",
        expected_phase="submitting",
        require_no_external_task_id=True,
    )


def hold_video_poll_for_reconciliation(
    db,
    task_id: int,
    error: str,
    *,
    expected_status: str | None = None,
    expected_phase: str | None = None,
    expected_external_task_id: str | None = None,
) -> None:
    """Hold submitted video tasks when provider status cannot be trusted locally."""
    db.rollback()
    task = db.get(GenTask, task_id, populate_existing=True)
    if not task:
        return
    if expected_external_task_id is not None and task.external_task_id != expected_external_task_id:
        return
    mark_needs_review(
        db,
        task_id,
        (
            "视频上游任务状态未知,冻结积分暂不退回。"
            "请继续查询外部任务结果补结果结算,或确认上游未生成后人工退款。"
            f"external_task_id={expected_external_task_id or task.external_task_id or 'unknown'}; "
            f"error={error[:500]}"
        ),
        params_update={"_video_poll_state_unknown": True},
        rollback=False,
        expected_status=expected_status,
        expected_phase=expected_phase,
        expected_external_task_id=expected_external_task_id,
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
        params["duration"] = video_render_duration(params, task.stage)
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
    subject_mode = str(params.get("subject_mode") or "").lower()
    product_lock_mode = str(params.get("product_lock_mode") or "locked").lower()
    is_product_image_video = task.source_type == "image" and subject_mode == "product"
    if is_product_image_video:
        params["negative_prompt"] = product_video_negative_prompt(params.get("negative_prompt"))
    if first_frame:
        reference_kwargs = {
            "min_side": VIDEO_FIRST_FRAME_MIN_SIDE,
            "max_side": PRODUCT_VIDEO_REFERENCE_MAX_SIDE if is_product_image_video else VIDEO_FIRST_FRAME_MAX_SIDE,
        }
        if is_product_image_video:
            reference_kwargs.update(
                prefer_original_upload=True,
                quality=92,
                subsampling=0,
            )
        safe_ref = gateway_reference_image(
            db,
            task,
            first_frame,
            **reference_kwargs,
        )
        params["first_frame_image"] = safe_ref
        if params.get("reference_image_url"):
            params["reference_image_url"] = safe_ref
        last_frame = params.get("last_frame_image")
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
                **reference_kwargs,
            )
    style_ref = params.get("style_reference_image")
    if style_ref:
        params["style_reference_image"] = gateway_reference_image(
            db,
            task,
            style_ref,
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


def persist_video_submit_result(
    db,
    task_id: int,
    *,
    request_id: str,
    external_task_id: str,
    submitted_params: dict,
    provider_status: str | None = None,
    result_url: str | None = None,
) -> str | None:
    """Fence a provider submit result against the latest task generation.

    Provider submits are non-idempotent and may run while another transaction
    requests cancellation. Always merge into freshly observed params so a stale
    submit worker cannot erase that request and continue into polling/settlement.
    """
    target_phase = "downloading" if provider_status == "succeeded" and result_url else "polling"
    for _attempt in range(3):
        db.rollback()
        task = db.get(GenTask, task_id, populate_existing=True)
        if not task:
            return None
        observed_params = task.params if isinstance(task.params, dict) else {}
        if (
            task.status != "running"
            or task.phase != "submitting"
            or task.external_task_id is not None
            or str(observed_params.get("_video_request_id") or "") != str(request_id)
        ):
            return None

        persisted = video_persisted_params(task, dict(observed_params), submitted_params)
        if result_url:
            persisted["_video_result_url"] = result_url
        canceled = cancel_requested(task)
        durable_phase = "reconciling" if canceled else target_phase
        updated = db.execute(
            update(GenTask)
            .where(
                GenTask.id == task_id,
                GenTask.status == "running",
                GenTask.phase == "submitting",
                GenTask.external_task_id.is_(None),
                GenTask.params == task.params,
            )
            .values(
                params=persisted,
                external_task_id=str(external_task_id),
                external_submitted_at=datetime.now(timezone.utc),
                phase=durable_phase,
            )
            .execution_options(synchronize_session=False)
        ).rowcount
        if (updated or 0) != 1:
            continue

        db.commit()

        if canceled:
            try:
                db.rollback()
                current = db.get(GenTask, task_id, populate_existing=True)
                if (
                    not current
                    or not cancel_requested(current)
                    or not claim_terminal(
                        db,
                        task_id,
                        "canceled",
                        error="用户已取消任务",
                        expected_status="running",
                        expected_phase=durable_phase,
                        expected_external_task_id=str(external_task_id),
                    )
                ):
                    db.rollback()
                    return None
                if current.cost_frozen and int(current.cost_settled or 0) == 0:
                    credits.refund(
                        db,
                        current.user_id,
                        current.cost_frozen,
                        biz_ref=current.id,
                        commit=False,
                    )
                db.commit()
                set_progress(task_id, 100, "canceled")
                publish_task_update(current, "canceled")
                return "canceled"
            except Exception as e:  # noqa: BLE001
                log.exception("video submit cancel refund failed for task %s", task_id)
                db.rollback()
                mark_needs_review(
                    db,
                    task_id,
                    f"视频已由上游接受,但取消退款失败,需要人工对账: {e}",
                    expected_status="running",
                    expected_phase=durable_phase,
                    expected_external_task_id=str(external_task_id),
                )
                return "needs_review"

        set_progress(task_id, 60 if target_phase == "downloading" else 30, "running")
        return target_phase
    db.rollback()
    return None


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

    result_url = found.get("url")
    phase = persist_video_submit_result(
        db,
        task.id,
        request_id=str(request_id),
        external_task_id=str(found["external_task_id"]),
        submitted_params=submitted_params,
        provider_status=found.get("status"),
        result_url=result_url,
    )
    if phase not in ("polling", "downloading"):
        return False
    task = db.get(GenTask, task.id, populate_existing=True)
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
    submitted_external_task_id: str | None = None
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
            action = video_task_action(task)
            if action == "download":
                _handoff_video_download_best_effort(
                    db,
                    task_id,
                    task.external_task_id,
                    try_enqueue_video_download_fn,
                )
                return
            if action != "poll":
                return
            submitted = True
            submitted_external_task_id = task.external_task_id
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

            original_params = dict(task.params or {})
            prompt = str(original_params.get("_generation_prompt") or "").strip()
            if not prompt:
                prompt, original_params = _compile_legacy_video_prompt(task, model, original_params)
            if not prompt:
                raise RuntimeError("视频提示词为空，无法提交生成")
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
                        action = video_task_action(task)
                        if action == "download":
                            enqueue_download_fn(
                                db,
                                task_id,
                                external_task_id=task.external_task_id,
                            )
                        elif action == "poll":
                            mark_poll_alive(task_id, task.external_task_id)
                            enqueue_poll_fn(task_id, task.external_task_id)
                        return
                    hold_video_submit_unknown_for_reconciliation(
                        db,
                        task_id,
                        str(e),
                        request_id=str(original_params["_video_request_id"]),
                        expected_params=original_params,
                        params_update=video_persisted_params(task, original_params, params),
                    )
                    return
                raise
            phase = persist_video_submit_result(
                db,
                task_id,
                request_id=str(original_params["_video_request_id"]),
                external_task_id=str(ext_id),
                submitted_params=params,
            )
            if phase != "polling":
                return
            submitted_external_task_id = str(ext_id)
            # The external task id is the recovery point. Persist it before any
            # best-effort accounting/progress side effects so a worker crash
            # after upstream acceptance can be resumed instead of stranded in
            # submitting.
            try:
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
            except Exception:  # noqa: BLE001
                log.exception("video submit usage record failed for task %s", task_id)
            set_progress(task_id, 30, "running")
            submitted = True
    except TaskCanceled as e:
        cancel_and_refund(db, task_id, str(e))
    except Exception as e:  # noqa: BLE001
        log.exception("video submit %s failed", task_id)
        current = db.get(GenTask, task_id)
        if submitted or (current and current.external_task_id):
            action = video_task_action(current)
            if action == "download":
                _handoff_video_download_best_effort(
                    db,
                    task_id,
                    current.external_task_id,
                    try_enqueue_video_download_fn,
                )
            elif action == "poll":
                mark_poll_alive(task_id, current.external_task_id)
                enqueue_poll_fn(task_id, current.external_task_id)
            return
        public_error = "视频提交失败，已退回冻结积分，请稍后重试"
        fail_and_refund(db, task_id, str(e), public_error=public_error)
    finally:
        db.close()
        locks.release(lock_key, lock_token)

    if submitted:
        mark_poll_alive(task_id, submitted_external_task_id)
        enqueue_poll_fn(task_id, submitted_external_task_id)


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
    polled_external_task_id: str | None = None
    db = SessionLocal()
    try:
        task = db.get(GenTask, task_id)
        if not task or task.status in TERMINAL:
            return
        action = video_task_action(task)
        if action == "download":
            _handoff_video_download_best_effort(
                db,
                task_id,
                task.external_task_id,
                try_enqueue_video_download_fn,
            )
            return
        if action != "poll":
            return
        polled_external_task_id = task.external_task_id
        model = model_loader(db, "video")
        if not model:
            hold_video_poll_for_reconciliation(
                db,
                task_id,
                "视频模型配置缺失",
                expected_status="running",
                expected_phase="polling",
                expected_external_task_id=polled_external_task_id,
            )
            return
        try:
            model = model_from_snapshot(task, model)
        except ModelSnapshotMismatchError as e:
            hold_video_poll_for_reconciliation(
                db,
                task_id,
                str(e),
                expected_status="running",
                expected_phase="polling",
                expected_external_task_id=polled_external_task_id,
            )
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
                f"external_task_id={polled_external_task_id or 'unknown'}",
                expected_status="running",
                expected_phase="polling",
                expected_external_task_id=polled_external_task_id,
            )
            return

        current, current_action = _reload_polled_video_task(
            db,
            task_id,
            polled_external_task_id,
        )
        if current_action == "download":
            _handoff_video_download_best_effort(
                db,
                task_id,
                current.external_task_id,
                try_enqueue_video_download_fn,
            )
            return
        if current_action != "poll":
            return
        task = current
        mark_poll_alive(task_id, polled_external_task_id)
        try:
            res = poller(model, polled_external_task_id)
        except SoftTimeLimitExceeded:
            log.warning("poll_video_once soft time limit for task %s; holding for review", task_id)
            current, current_action = _reload_polled_video_task(
                db,
                task_id,
                polled_external_task_id,
            )
            if current_action == "download":
                _handoff_video_download_best_effort(
                    db,
                    task_id,
                    current.external_task_id,
                    try_enqueue_video_download_fn,
                )
            elif current_action == "poll":
                hold_video_poll_for_reconciliation(
                    db,
                    task_id,
                    "视频状态查询执行超时",
                    expected_status="running",
                    expected_phase="polling",
                    expected_external_task_id=polled_external_task_id,
                )
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
                        "external_task_id": polled_external_task_id,
                        "transient": True,
                        "error": str(e)[:300],
                    },
                )
            current, current_action = _reload_polled_video_task(
                db,
                task_id,
                polled_external_task_id,
            )
            if current_action == "download":
                _handoff_video_download_best_effort(
                    db,
                    task_id,
                    current.external_task_id,
                    try_enqueue_video_download_fn,
                )
                return
            if current_action != "poll":
                return
            fails = bump_poll_errors(task_id, polled_external_task_id)
            log.warning("poll error %s/%s for task %s: %s", fails, POLL_MAX_CONSEC_ERRORS, task_id, e)
            if fails >= POLL_MAX_CONSEC_ERRORS:
                hold_video_poll_for_reconciliation(
                    db,
                    task_id,
                    f"视频轮询连续失败: {e}",
                    expected_status="running",
                    expected_phase="polling",
                    expected_external_task_id=polled_external_task_id,
                )
            else:
                mark_poll_alive(task_id, polled_external_task_id)
                current, current_action = _reload_polled_video_task(
                    db,
                    task_id,
                    polled_external_task_id,
                )
                if current_action == "download":
                    _handoff_video_download_best_effort(
                        db,
                        task_id,
                        current.external_task_id,
                        try_enqueue_video_download_fn,
                    )
                elif current_action == "poll":
                    enqueue_poll_fn(task_id, polled_external_task_id)
            return

        current, current_action = _reload_polled_video_task(
            db,
            task_id,
            polled_external_task_id,
        )
        if current_action == "download":
            _handoff_video_download_best_effort(
                db,
                task_id,
                current.external_task_id,
                try_enqueue_video_download_fn,
            )
            return
        if current_action != "poll":
            return
        reset_poll_errors(task_id, polled_external_task_id)

        status = res.get("status")
        if status == "failed":
            usage.record_call(
                db,
                kind="video_poll",
                model_id=model.model_id,
                user_id=current.user_id,
                task_id=current.id,
                status="failed",
                detail={
                    "stage": current.stage,
                    "external_task_id": polled_external_task_id,
                    "error": str(res.get("error"))[:300],
                },
            )
            fail_and_refund(
                db,
                task_id,
                res.get("error") or "视频网关返回失败",
                public_error="视频生成失败，已退回冻结积分，请稍后重试",
                expected_status="running",
                expected_phase="polling",
                expected_external_task_id=polled_external_task_id,
            )
            return
        if status == "succeeded":
            provider_usage = usage_from_response(res)
            try:
                usage.record_call(
                    db,
                    kind="video_poll",
                    model_id=model.model_id,
                    user_id=current.user_id,
                    task_id=current.id,
                    status="ok",
                    usage=provider_usage,
                    detail={
                        "stage": current.stage,
                        "external_task_id": polled_external_task_id,
                    },
                )
            except Exception:  # noqa: BLE001
                log.exception("video poll usage record failed for task %s", task_id)
                db.rollback()
            try:
                won_download = persist_video_download_result(db, current, res)
            except Exception as e:  # noqa: BLE001
                log.exception("video success persistence failed for task %s", task_id)
                latest, latest_action = _reload_polled_video_task(
                    db,
                    task_id,
                    polled_external_task_id,
                )
                if latest_action == "download":
                    _handoff_video_download_best_effort(
                        db,
                        task_id,
                        latest.external_task_id,
                        try_enqueue_video_download_fn,
                    )
                elif latest_action == "poll":
                    hold_video_poll_for_reconciliation(
                        db,
                        task_id,
                        str(e),
                        expected_status="running",
                        expected_phase="polling",
                        expected_external_task_id=polled_external_task_id,
                    )
                return
            if won_download:
                _enqueue_owned_video_download(
                    db,
                    task_id,
                    polled_external_task_id,
                    try_enqueue_video_download_fn,
                )
                return
            latest, latest_action = _reload_polled_video_task(
                db,
                task_id,
                polled_external_task_id,
            )
            if latest_action == "download":
                _handoff_video_download_best_effort(
                    db,
                    task_id,
                    latest.external_task_id,
                    try_enqueue_video_download_fn,
                )
            return

        mark_poll_alive(task_id, polled_external_task_id)
        current, current_action = _reload_polled_video_task(
            db,
            task_id,
            polled_external_task_id,
        )
        if current_action == "download":
            _handoff_video_download_best_effort(
                db,
                task_id,
                current.external_task_id,
                try_enqueue_video_download_fn,
            )
            return
        if current_action != "poll":
            return
        pct = min(85, 30 + int(elapsed) * 55 // max(1, poll_budget))
        set_progress(task_id, pct, "running")
        enqueue_poll_fn(task_id, polled_external_task_id)
    except SoftTimeLimitExceeded:
        log.warning("poll_video_once soft time limit for task %s; holding for review", task_id)
        if polled_external_task_id is not None:
            current, current_action = _reload_polled_video_task(
                db,
                task_id,
                polled_external_task_id,
            )
            if current_action == "download":
                _handoff_video_download_best_effort(
                    db,
                    task_id,
                    current.external_task_id,
                    try_enqueue_video_download_fn,
                )
            elif current_action == "poll":
                hold_video_poll_for_reconciliation(
                    db,
                    task_id,
                    "视频状态查询执行超时",
                    expected_status="running",
                    expected_phase="polling",
                    expected_external_task_id=polled_external_task_id,
                )
    except Exception as e:  # noqa: BLE001
        log.exception("poll_video_once %s failed", task_id)
        if polled_external_task_id is None:
            return
        current, current_action = _reload_polled_video_task(
            db,
            task_id,
            polled_external_task_id,
        )
        if current_action == "download":
            _handoff_video_download_best_effort(
                db,
                task_id,
                current.external_task_id,
                try_enqueue_video_download_fn,
            )
        elif current_action == "poll":
            hold_video_poll_for_reconciliation(
                db,
                task_id,
                str(e),
                expected_status="running",
                expected_phase="polling",
                expected_external_task_id=polled_external_task_id,
            )
    finally:
        db.close()
