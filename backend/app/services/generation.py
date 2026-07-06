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

from sqlalchemy import select

from ..config import settings as _settings
from ..models import GenTask
from . import credits, gateway, locks, storage  # noqa: F401
from .config_store import get_model_config
from .generation_common import TaskCanceled, TaskLockedError, fail_and_refund
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
from .generation_video_flow import enqueue_poll as _enqueue_poll
from .generation_video_flow import enqueue_video_download as _enqueue_video_download
from .generation_video_flow import has_video_download_result as _has_video_download_result
from .generation_video_flow import mark_poll_alive as _mark_poll_alive
from .generation_video_flow import (
    mark_video_download_alive as _mark_video_download_alive,  # noqa: F401
)
from .generation_video_flow import poll_chain_alive as _poll_chain_alive
from .generation_video_flow import video_download_alive as _video_download_alive
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

log = logging.getLogger("generation")
VIDEO_FIRST_FRAME_MIN_SIDE = 300
VIDEO_FIRST_FRAME_MAX_SIDE = 768
settings = _settings  # legacy facade attribute
_fail_and_refund = fail_and_refund  # legacy test shim

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


def _try_enqueue_poll(task_id: int) -> None:
    try:
        _enqueue_poll(task_id)
    except Exception:
        log.exception("video poll enqueue failed for task %s", task_id)


def _try_enqueue_video_download(db, task_id: int, *, countdown: int = 0) -> None:
    _try_enqueue_video_download_impl(
        db,
        task_id,
        countdown=countdown,
        enqueue_video_download_fn=_enqueue_video_download,
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
        try_enqueue_video_download_fn=_try_enqueue_video_download,
    )


def poll_video_once(task_id: int) -> None:
    _poll_video_once_impl(
        task_id,
        get_model_config_fn=get_model_config,
        poll_video_fn=_poll_video_with_model_config,
        try_enqueue_poll_fn=_try_enqueue_poll,
        try_enqueue_video_download_fn=_try_enqueue_video_download,
        poll_max_seconds=VIDEO_POLL_MAX_SECONDS,
    )


def run_video_download_task(task_id: int) -> None:
    _run_video_download_task_impl(
        task_id,
        get_model_config_fn=get_model_config,
        enqueue_video_download_fn=_enqueue_video_download,
    )


def _finalize_video_success(db, task: GenTask, model, result: dict) -> None:
    _finalize_video_success_impl(db, task, model, result)


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


def _hold_video_download_for_reconciliation(db, task_id: int, error: str) -> None:
    _hold_video_download_for_reconciliation_impl(db, task_id, error)


def _submit_state_unknown(exc: Exception) -> bool:
    return _submit_state_unknown_impl(exc)
