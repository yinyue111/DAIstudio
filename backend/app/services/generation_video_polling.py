"""Video polling, download handoff, and reconciliation."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from billiard.exceptions import SoftTimeLimitExceeded

from ..db import SessionLocal
from ..models import GenTask
from . import gateway, usage
from .config_store import get_model_config
from .generation_common import fail_and_refund, mark_needs_review
from .generation_model_runtime import (
    ModelSnapshotMismatchError,
    model_config_for_task,
    model_from_snapshot,
    poll_video_with_model_config,
)
from .generation_state import TERMINAL_STATUSES as TERMINAL
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

log = logging.getLogger("generation")


def _enqueue_poll_safely(task_id: int, external_task_id: str | None = None) -> None:
    try:
        enqueue_poll(task_id, external_task_id)
    except Exception:
        log.exception("video poll enqueue failed for task %s", task_id)

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
def _poll_owner_state(
    db,
    task_id: int,
    external_task_id: str,
    enqueue_download_fn=None,
):
    current, action = _reload_polled_video_task(db, task_id, external_task_id)
    if action == "download":
        _handoff_video_download_best_effort(
            db,
            task_id,
            current.external_task_id,
            enqueue_download_fn,
        )
    return current, action


def _load_pollable_video_task(db, task_id: int, enqueue_download_fn=None):
    task = db.get(GenTask, task_id)
    if not task or task.status in TERMINAL:
        return None
    action = video_task_action(task)
    if action == "download":
        _handoff_video_download_best_effort(
            db,
            task_id,
            task.external_task_id,
            enqueue_download_fn,
        )
        return None
    return task if action == "poll" else None


def _poll_runtime_model(
    db,
    task: GenTask,
    task_id: int,
    external_task_id: str,
    model_loader,
):
    model = model_config_for_task(db, task, "video", model_loader)
    if not model:
        hold_video_poll_for_reconciliation(
            db,
            task_id,
            "视频模型配置缺失",
            expected_status="running",
            expected_phase="polling",
            expected_external_task_id=external_task_id,
        )
        return None
    try:
        return model_from_snapshot(task, model, db)
    except ModelSnapshotMismatchError as exc:
        hold_video_poll_for_reconciliation(
            db,
            task_id,
            str(exc),
            expected_status="running",
            expected_phase="polling",
            expected_external_task_id=external_task_id,
        )
        return None


def _poll_timing(task: GenTask, poll_max_seconds: int | None) -> tuple[int, float]:
    poll_budget = int(poll_max_seconds or VIDEO_POLL_MAX_SECONDS)
    submitted_at = aware(task.external_submitted_at) or aware(task.created_at)
    elapsed = (
        (datetime.now(timezone.utc) - submitted_at).total_seconds()
        if submitted_at
        else 0
    )
    return poll_budget, elapsed


def _expire_video_poll(
    db,
    task: GenTask,
    model,
    *,
    task_id: int,
    external_task_id: str,
    poll_budget: int,
    elapsed: float,
) -> bool:
    if elapsed <= poll_budget:
        return False
    timeout_minutes = max(1, (poll_budget + 59) // 60)
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
    fail_and_refund(
        db,
        task_id,
        f"视频生成超过 {poll_budget} 秒仍未完成; "
        f"external_task_id={external_task_id or 'unknown'}",
        public_error=(
            f"视频生成超过 {timeout_minutes} 分钟，"
            "任务已自动失败并退回冻结积分"
        ),
        expected_status="running",
        expected_phase="polling",
        expected_external_task_id=external_task_id,
    )
    return True


def _reconcile_poll_soft_timeout(
    db,
    task_id: int,
    external_task_id: str | None,
    enqueue_download_fn=None,
) -> None:
    log.warning("poll_video_once soft time limit for task %s; holding for review", task_id)
    if external_task_id is None:
        return
    _, action = _poll_owner_state(
        db,
        task_id,
        external_task_id,
        enqueue_download_fn,
    )
    if action == "poll":
        hold_video_poll_for_reconciliation(
            db,
            task_id,
            "视频状态查询执行超时",
            expected_status="running",
            expected_phase="polling",
            expected_external_task_id=external_task_id,
        )


def _handle_poll_provider_error(
    db,
    *,
    task: GenTask,
    model,
    task_id: int,
    external_task_id: str,
    error: Exception,
    enqueue_poll_fn,
    enqueue_download_fn=None,
) -> None:
    is_transient = isinstance(error, gateway.GatewayError) and getattr(
        error,
        "transient",
        False,
    )
    if is_transient:
        usage.record_call(
            db,
            kind="video_poll",
            model_id=model.model_id,
            user_id=task.user_id,
            task_id=task.id,
            status="failed",
            detail={
                "stage": task.stage,
                "external_task_id": external_task_id,
                "transient": True,
                "error": str(error)[:300],
            },
        )
    _, action = _poll_owner_state(
        db,
        task_id,
        external_task_id,
        enqueue_download_fn,
    )
    if action != "poll":
        return

    fails = bump_poll_errors(task_id, external_task_id)
    log.warning(
        "poll error %s/%s for task %s: %s",
        fails,
        POLL_MAX_CONSEC_ERRORS,
        task_id,
        error,
    )
    if fails >= POLL_MAX_CONSEC_ERRORS:
        hold_video_poll_for_reconciliation(
            db,
            task_id,
            f"视频轮询连续失败: {error}",
            expected_status="running",
            expected_phase="polling",
            expected_external_task_id=external_task_id,
        )
        return

    mark_poll_alive(task_id, external_task_id)
    _, action = _poll_owner_state(
        db,
        task_id,
        external_task_id,
        enqueue_download_fn,
    )
    if action == "poll":
        enqueue_poll_fn(task_id, external_task_id)


def _handle_failed_poll_result(
    db,
    *,
    task: GenTask,
    model,
    task_id: int,
    external_task_id: str,
    result: dict[str, Any],
) -> None:
    usage.record_call(
        db,
        kind="video_poll",
        model_id=model.model_id,
        user_id=task.user_id,
        task_id=task.id,
        status="failed",
        detail={
            "stage": task.stage,
            "external_task_id": external_task_id,
            "error": str(result.get("error"))[:300],
        },
    )
    fail_and_refund(
        db,
        task_id,
        result.get("error") or "视频网关返回失败",
        public_error="视频生成失败，已退回冻结积分，请稍后重试",
        expected_status="running",
        expected_phase="polling",
        expected_external_task_id=external_task_id,
    )


def _handle_succeeded_poll_result(
    db,
    *,
    task: GenTask,
    model,
    task_id: int,
    external_task_id: str,
    result: dict[str, Any],
    enqueue_download_fn=None,
) -> None:
    provider_usage = usage_from_response(result)
    try:
        usage.record_call(
            db,
            kind="video_poll",
            model_id=model.model_id,
            user_id=task.user_id,
            task_id=task.id,
            status="ok",
            usage=provider_usage,
            detail={
                "stage": task.stage,
                "external_task_id": external_task_id,
            },
        )
    except Exception:  # noqa: BLE001
        log.exception("video poll usage record failed for task %s", task_id)
        db.rollback()

    try:
        won_download = persist_video_download_result(db, task, result)
    except Exception as exc:  # noqa: BLE001
        log.exception("video success persistence failed for task %s", task_id)
        _, action = _poll_owner_state(
            db,
            task_id,
            external_task_id,
            enqueue_download_fn,
        )
        if action == "poll":
            hold_video_poll_for_reconciliation(
                db,
                task_id,
                str(exc),
                expected_status="running",
                expected_phase="polling",
                expected_external_task_id=external_task_id,
            )
        return

    if won_download:
        _enqueue_owned_video_download(
            db,
            task_id,
            external_task_id,
            enqueue_download_fn,
        )
        return
    _poll_owner_state(
        db,
        task_id,
        external_task_id,
        enqueue_download_fn,
    )


def _continue_pending_poll(
    db,
    *,
    task_id: int,
    external_task_id: str,
    elapsed: float,
    poll_budget: int,
    enqueue_poll_fn,
    enqueue_download_fn=None,
) -> None:
    mark_poll_alive(task_id, external_task_id)
    _, action = _poll_owner_state(
        db,
        task_id,
        external_task_id,
        enqueue_download_fn,
    )
    if action != "poll":
        return
    pct = min(85, 30 + int(elapsed) * 55 // max(1, poll_budget))
    set_progress(task_id, pct, "running")
    enqueue_poll_fn(task_id, external_task_id)


def _reconcile_poll_exception(
    db,
    *,
    task_id: int,
    external_task_id: str | None,
    error: Exception,
    enqueue_download_fn=None,
) -> None:
    if external_task_id is None:
        return
    _, action = _poll_owner_state(
        db,
        task_id,
        external_task_id,
        enqueue_download_fn,
    )
    if action == "poll":
        hold_video_poll_for_reconciliation(
            db,
            task_id,
            str(error),
            expected_status="running",
            expected_phase="polling",
            expected_external_task_id=external_task_id,
        )


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
        task = _load_pollable_video_task(
            db,
            task_id,
            try_enqueue_video_download_fn,
        )
        if task is None:
            return
        polled_external_task_id = task.external_task_id

        model = _poll_runtime_model(
            db,
            task,
            task_id,
            polled_external_task_id,
            model_loader,
        )
        if model is None:
            return

        poll_budget, elapsed = _poll_timing(task, poll_max_seconds)
        if _expire_video_poll(
            db,
            task,
            model,
            task_id=task_id,
            external_task_id=polled_external_task_id,
            poll_budget=poll_budget,
            elapsed=elapsed,
        ):
            return

        task, action = _poll_owner_state(
            db,
            task_id,
            polled_external_task_id,
            try_enqueue_video_download_fn,
        )
        if action != "poll":
            return

        mark_poll_alive(task_id, polled_external_task_id)
        try:
            result = poller(model, polled_external_task_id)
        except SoftTimeLimitExceeded:
            _reconcile_poll_soft_timeout(
                db,
                task_id,
                polled_external_task_id,
                try_enqueue_video_download_fn,
            )
            return
        except Exception as exc:  # noqa: BLE001
            _handle_poll_provider_error(
                db,
                task=task,
                model=model,
                task_id=task_id,
                external_task_id=polled_external_task_id,
                error=exc,
                enqueue_poll_fn=enqueue_poll_fn,
                enqueue_download_fn=try_enqueue_video_download_fn,
            )
            return

        task, action = _poll_owner_state(
            db,
            task_id,
            polled_external_task_id,
            try_enqueue_video_download_fn,
        )
        if action != "poll":
            return
        reset_poll_errors(task_id, polled_external_task_id)

        status = result.get("status")
        if status == "failed":
            _handle_failed_poll_result(
                db,
                task=task,
                model=model,
                task_id=task_id,
                external_task_id=polled_external_task_id,
                result=result,
            )
            return
        if status == "succeeded":
            _handle_succeeded_poll_result(
                db,
                task=task,
                model=model,
                task_id=task_id,
                external_task_id=polled_external_task_id,
                result=result,
                enqueue_download_fn=try_enqueue_video_download_fn,
            )
            return
        _continue_pending_poll(
            db,
            task_id=task_id,
            external_task_id=polled_external_task_id,
            elapsed=elapsed,
            poll_budget=poll_budget,
            enqueue_poll_fn=enqueue_poll_fn,
            enqueue_download_fn=try_enqueue_video_download_fn,
        )
    except SoftTimeLimitExceeded:
        _reconcile_poll_soft_timeout(
            db,
            task_id,
            polled_external_task_id,
            try_enqueue_video_download_fn,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("poll_video_once %s failed", task_id)
        _reconcile_poll_exception(
            db,
            task_id=task_id,
            external_task_id=polled_external_task_id,
            error=exc,
            enqueue_download_fn=try_enqueue_video_download_fn,
        )
    finally:
        db.close()
