"""Generation orchestration (runs inside Celery workers).

Image: gateway returns N images -> we save HD + a watermarked low-res preview
per image, settle the frozen estimate against real cost.
Video: submit async job -> poll to completion -> save preview (and HD for final
stage). Idempotent: a task that already reached a terminal state is skipped.
Recoverable provider-success/local-persistence failures are held for review;
ordinary pre-submit failures refund the frozen credits, while submitted video
tasks with unknown upstream state are held for reconciliation.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from ..config import settings as _settings
from ..models import AppSetting, GenTask
from . import credits, gateway, locks, storage  # noqa: F401
from .config_store import get_model_config
from .generation_common import (
    TaskCanceled,
    TaskLockedError,
    fail_and_refund,
    mark_needs_review,
    publish_task_update,
)
from .generation_image_flow import image_review_has_local_results
from .generation_image_flow import run_image_task as _run_image_task_impl
from .generation_image_review import (
    admin_settle_needs_review_image as _admin_settle_needs_review_image_impl,
)
from .generation_model_runtime import (
    ModelSnapshotMismatchError,
    assert_model_snapshot_compatible,
    model_snapshot,
)
from .generation_model_runtime import gen_image_with_model_config as _gen_image_with_model_config
from .generation_model_runtime import poll_video_with_model_config as _poll_video_with_model_config
from .generation_model_runtime import (
    submit_video_with_model_config as _submit_video_with_model_config,
)
from .generation_state import (
    NEEDS_REVIEW,
    LocalVideoSettlementError,
    claim_terminal,
    is_terminal_status,
)
from .generation_state import cancel_requested as _cancel_requested
from .generation_video_download import (
    admin_settle_needs_review_video as _admin_settle_needs_review_video_impl,
)
from .generation_video_download import (
    finalize_or_retry_video_download as _finalize_or_retry_video_download_impl,
)
from .generation_video_download import finalize_video_success as _finalize_video_success_impl
from .generation_video_download import (
    hold_video_download_for_reconciliation as _hold_video_download_for_reconciliation_impl,
)
from .generation_video_download import run_video_download_task as _run_video_download_task_impl
from .generation_video_download import (
    try_enqueue_video_download as _try_enqueue_video_download_impl,
)
from .generation_video_download import video_review_has_local_results
from .generation_video_flow import POLL_MAX_CONSEC_ERRORS as _POLL_MAX_CONSEC_ERRORS  # noqa: F401
from .generation_video_flow import (
    VIDEO_DOWNLOAD_MAX_ATTEMPTS as _VIDEO_DOWNLOAD_MAX_ATTEMPTS,  # noqa: F401
)
from .generation_video_flow import VIDEO_POLL_MAX_SECONDS
from .generation_video_flow import clear_video_download_alive as _clear_video_download_alive
from .generation_video_flow import enqueue_poll as _enqueue_poll
from .generation_video_flow import enqueue_video_download as _enqueue_video_download
from .generation_video_flow import mark_poll_alive as _mark_poll_alive
from .generation_video_flow import (
    mark_video_download_alive as _mark_video_download_alive,
)
from .generation_video_flow import poll_chain_alive as _poll_chain_alive
from .generation_video_flow import video_download_alive as _video_download_alive
from .generation_video_flow import video_task_action as _video_task_action
from .generation_video_submit import (
    hold_video_poll_for_reconciliation as _hold_video_poll_for_reconciliation_impl,
)
from .generation_video_submit import (
    hold_video_submit_unknown_for_reconciliation as _hold_video_submit_unknown_for_reconciliation_impl,
)
from .generation_video_submit import poll_video_once as _poll_video_once_impl
from .generation_video_submit import (
    recover_unknown_submit_by_request_id as _recover_unknown_submit_by_request_id_impl,
)
from .generation_video_submit import start_video_task as _start_video_task_impl
from .generation_video_submit import submit_state_unknown as _submit_state_unknown_impl
from .generation_video_submit import video_persisted_params as _video_persisted_params_impl
from .generation_video_submit import video_submit_params as _video_submit_params_impl
from .progress import set_progress as _set_progress

log = logging.getLogger("generation")
VIDEO_FIRST_FRAME_MIN_SIDE = 300
VIDEO_FIRST_FRAME_MAX_SIDE = 768
settings = _settings  # legacy facade attribute
_fail_and_refund = fail_and_refund  # legacy test shim
_VIDEO_RESUME_CURSOR_SETTING = "video_resume_cursor"
_VIDEO_RESUME_SCAN_LOCK_KEY = "video:resume:scan"
_VIDEO_RESUME_SCAN_LOCK_TTL = 60

__all__ = [
    "ModelSnapshotMismatchError",
    "NEEDS_REVIEW",
    "TaskCanceled",
    "TaskLockedError",
    "admin_settle_needs_review_task",
    "admin_settle_needs_review_video",
    "assert_model_snapshot_compatible",
    "LocalVideoSettlementError",
    "claim_terminal",
    "image_review_has_local_results",
    "is_terminal_status",
    "model_snapshot",
    "poll_video_once",
    "resume_stuck_videos",
    "run_image_task",
    "run_video_download_task",
    "start_video_task",
    "video_review_has_local_results",
]


def _try_enqueue_poll(task_id: int, external_task_id: str | None = None) -> None:
    try:
        _enqueue_poll(task_id, external_task_id)
    except Exception:
        log.exception("video poll enqueue failed for task %s", task_id)


def _try_enqueue_video_download(
    db,
    task_id: int,
    *,
    countdown: int = 0,
    external_task_id: str | None = None,
) -> None:
    _try_enqueue_video_download_impl(
        db,
        task_id,
        countdown=countdown,
        external_task_id=external_task_id,
        enqueue_video_download_fn=_enqueue_video_download,
    )


def _enqueue_video_download_raw(
    db,
    task_id: int,
    *,
    countdown: int = 0,
    external_task_id: str | None = None,
) -> None:
    del db
    _enqueue_video_download(
        task_id,
        countdown=countdown,
        external_task_id=external_task_id,
    )


def run_image_task(task_id: int) -> None:
    _run_image_task_impl(task_id, gen_image_fn=_gen_image_with_model_config)


def _hold_video_submit_unknown_for_reconciliation(
    db,
    task_id: int,
    error: str,
    *,
    params_update: dict | None = None,
) -> None:
    _hold_video_submit_unknown_for_reconciliation_impl(
        db,
        task_id,
        error,
        params_update=params_update,
    )


def _hold_video_poll_for_reconciliation(db, task_id: int, error: str) -> None:
    _hold_video_poll_for_reconciliation_impl(db, task_id, error)



def _video_submit_params(db, task: GenTask) -> dict:
    return _video_submit_params_impl(db, task)


def _video_persisted_params(task: GenTask, original: dict, submitted: dict) -> dict:
    return _video_persisted_params_impl(task, original, submitted)


def _finalize_or_retry_video_download(db, task: GenTask, model, result: dict) -> bool:
    return _finalize_or_retry_video_download_impl(
        db,
        task,
        model,
        result,
        enqueue_video_download_fn=_enqueue_video_download,
    )


def _recover_unknown_submit_by_request_id(
    db,
    task: GenTask,
    model,
    original_params: dict,
    submitted_params: dict,
) -> bool:
    return _recover_unknown_submit_by_request_id_impl(
        db,
        task,
        model,
        original_params,
        submitted_params,
    )


def start_video_task(task_id: int) -> None:
    _start_video_task_impl(
        task_id,
        get_model_config_fn=get_model_config,
        submit_video_fn=_submit_video_with_model_config,
        try_enqueue_poll_fn=_try_enqueue_poll,
        try_enqueue_video_download_fn=_enqueue_video_download_raw,
    )


def poll_video_once(task_id: int) -> None:
    _poll_video_once_impl(
        task_id,
        get_model_config_fn=get_model_config,
        poll_video_fn=_poll_video_with_model_config,
        try_enqueue_poll_fn=_try_enqueue_poll,
        try_enqueue_video_download_fn=_enqueue_video_download_raw,
        poll_max_seconds=VIDEO_POLL_MAX_SECONDS,
    )


def run_video_download_task(
    task_id: int,
    expected_external_task_id: str | None = None,
) -> None:
    _run_video_download_task_impl(
        task_id,
        expected_external_task_id=expected_external_task_id,
        get_model_config_fn=get_model_config,
        enqueue_video_download_fn=_enqueue_video_download,
    )


def _finalize_video_success(db, task: GenTask, model, result: dict) -> bool:
    return _finalize_video_success_impl(db, task, model, result)


def admin_settle_needs_review_video(
    db,
    task: GenTask,
    *,
    result_url: str | None = None,
    external_task_id: str | None = None,
) -> None:
    _admin_settle_needs_review_video_impl(
        db,
        task,
        result_url=result_url,
        external_task_id=external_task_id,
        get_model_config_fn=get_model_config,
    )


def admin_settle_needs_review_image(
    db,
    task: GenTask,
    *,
    result_url: str | None = None,
) -> None:
    _admin_settle_needs_review_image_impl(
        db,
        task,
        result_url=result_url,
        get_model_config_fn=get_model_config,
    )


def admin_settle_needs_review_task(
    db,
    task: GenTask,
    *,
    result_url: str | None = None,
    external_task_id: str | None = None,
) -> None:
    if task.category == "video":
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


def _video_resume_position(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _claim_video_resume_state(db, owner: str) -> tuple[int, int, dict] | None:
    row = db.get(AppSetting, _VIDEO_RESUME_CURSOR_SETTING, populate_existing=True)
    observed = dict(row.value or {}) if row else None
    cursor = _video_resume_position((observed or {}).get("v"))
    high_water = _video_resume_position((observed or {}).get("high_water"))
    claimed = {"v": cursor, "high_water": high_water, "owner": owner}
    if not locks.refresh(
        _VIDEO_RESUME_SCAN_LOCK_KEY,
        owner,
        _VIDEO_RESUME_SCAN_LOCK_TTL,
    ):
        db.rollback()
        return None
    if row is None:
        db.add(AppSetting(key=_VIDEO_RESUME_CURSOR_SETTING, value=claimed))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return None
    else:
        result = db.execute(
            update(AppSetting)
            .where(
                AppSetting.key == _VIDEO_RESUME_CURSOR_SETTING,
                AppSetting.value == observed,
            )
            .values(value=claimed)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            db.rollback()
            return None
        db.commit()
    if not locks.refresh(
        _VIDEO_RESUME_SCAN_LOCK_KEY,
        owner,
        _VIDEO_RESUME_SCAN_LOCK_TTL,
    ):
        return None
    return cursor, high_water, claimed


def _persist_video_resume_state(
    db,
    *,
    owner: str,
    claimed: dict,
    cursor: int,
    high_water: int,
) -> bool:
    if not locks.refresh(
        _VIDEO_RESUME_SCAN_LOCK_KEY,
        owner,
        _VIDEO_RESUME_SCAN_LOCK_TTL,
    ):
        db.rollback()
        return False
    persisted = {
        "v": _video_resume_position(cursor),
        "high_water": _video_resume_position(high_water),
        "owner": owner,
    }
    result = db.execute(
        update(AppSetting)
        .where(
            AppSetting.key == _VIDEO_RESUME_CURSOR_SETTING,
            AppSetting.value == claimed,
        )
        .values(value=persisted)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        return False
    db.commit()
    return True


def _video_resume_candidates(db, *, cursor: int, high_water: int, limit: int) -> list[GenTask]:
    return list(
        db.execute(
            select(GenTask)
            .where(
                GenTask.status == "running",
                GenTask.category == "video",
                GenTask.external_task_id.isnot(None),
                GenTask.id > cursor,
                GenTask.id <= high_water,
            )
            .order_by(GenTask.id.asc())
            .limit(limit)
        ).scalars()
    )


def _recover_reconciling_video_cancel(db, task: GenTask) -> bool:
    params = task.params if isinstance(task.params, dict) else {}
    external_task_id = task.external_task_id
    if (
        task.status != "running"
        or task.phase != "reconciling"
        or not external_task_id
        or not _cancel_requested(task)
    ):
        return False
    try:
        if not claim_terminal(
            db,
            task.id,
            "canceled",
            error="用户已取消任务",
            expected_status="running",
            expected_phase="reconciling",
            expected_external_task_id=external_task_id,
            expected_params=params,
        ):
            db.rollback()
            return False
        if task.cost_frozen and int(task.cost_settled or 0) == 0:
            credits.refund(db, task.user_id, task.cost_frozen, biz_ref=task.id, commit=False)
        db.commit()
        _set_progress(task.id, 100, "canceled")
        publish_task_update(task, "canceled")
        return True
    except Exception as e:  # noqa: BLE001
        log.exception("video cancel recovery refund failed for task %s", task.id)
        db.rollback()
        mark_needs_review(
            db,
            task.id,
            f"视频取消恢复退款失败,需要人工对账: {e}",
            params_update={"_cancel_requested": True},
            expected_status="running",
            expected_phase="reconciling",
            expected_external_task_id=external_task_id,
        )
        return True


def resume_stuck_videos(db) -> int:
    """Recovery: re-attach a poll to any in-flight video whose poll chain looks
    dead (liveness key expired), so a worker crash never strands a submitted
    external job. Healthy chains (alive key present) are left untouched."""
    scan_token = locks.acquire(
        _VIDEO_RESUME_SCAN_LOCK_KEY,
        ttl=_VIDEO_RESUME_SCAN_LOCK_TTL,
    )
    if not scan_token:
        return 0
    try:
        claimed_state = _claim_video_resume_state(db, scan_token)
        if claimed_state is None:
            return 0
        cursor, high_water, claimed = claimed_state
        batch_size = max(1, min(int(settings.video_resume_batch_size), 1000))
        if high_water <= cursor:
            high_water = _video_resume_position(
                db.execute(
                    select(func.max(GenTask.id)).where(
                        GenTask.status == "running",
                        GenTask.category == "video",
                        GenTask.external_task_id.isnot(None),
                    )
                ).scalar_one_or_none()
            )
            cursor = 0
        rows = _video_resume_candidates(
            db,
            cursor=cursor,
            high_water=high_water,
            limit=batch_size,
        ) if high_water else []
        resumed = 0
        owns_scan = True
        for candidate in rows:
            if not locks.refresh(
                _VIDEO_RESUME_SCAN_LOCK_KEY,
                scan_token,
                _VIDEO_RESUME_SCAN_LOCK_TTL,
            ):
                owns_scan = False
                break
            lock_key = f"video:resume:{candidate.id}"
            lock_token = locks.acquire(lock_key, ttl=60)
            if not lock_token:
                continue
            try:
                current = db.get(GenTask, candidate.id, populate_existing=True)
                if _recover_reconciling_video_cancel(db, current):
                    resumed += 1
                    continue
                action = _video_task_action(current)
                if action == "poll":
                    if _poll_chain_alive(current.id, current.external_task_id):
                        continue
                    _mark_poll_alive(current.id, current.external_task_id)
                    _try_enqueue_poll(current.id, current.external_task_id)
                elif action == "download":
                    if _video_download_alive(current.id, current.external_task_id):
                        continue
                    _mark_video_download_alive(current.id, current.external_task_id)
                    try:
                        _enqueue_video_download(
                            current.id,
                            external_task_id=current.external_task_id,
                        )
                    except Exception:  # noqa: BLE001
                        _clear_video_download_alive(
                            current.id,
                            current.external_task_id,
                        )
                        log.exception(
                            "video recovery download enqueue failed for task %s; will retry",
                            current.id,
                        )
                        continue
                else:
                    continue
                resumed += 1
            finally:
                locks.release(lock_key, lock_token)
        if owns_scan:
            next_cursor = rows[-1].id if rows else high_water
            _persist_video_resume_state(
                db,
                owner=scan_token,
                claimed=claimed,
                cursor=next_cursor,
                high_water=high_water,
            )
        if resumed:
            log.info("resumed %s stuck video poll chain(s)", resumed)
        return resumed
    finally:
        locks.release(_VIDEO_RESUME_SCAN_LOCK_KEY, scan_token)


def _hold_video_download_for_reconciliation(
    db,
    task_id: int,
    error: str,
    *,
    expected_status: str | None = None,
    expected_phase: str | None = None,
    expected_external_task_id: str | None = None,
) -> bool:
    return _hold_video_download_for_reconciliation_impl(
        db,
        task_id,
        error,
        expected_status=expected_status,
        expected_phase=expected_phase,
        expected_external_task_id=expected_external_task_id,
    )


def _submit_state_unknown(exc: Exception) -> bool:
    return _submit_state_unknown_impl(exc)
