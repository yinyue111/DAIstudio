"""Celery task entrypoints. Thin wrappers around services.generation so the
orchestration logic stays import-safe and unit-testable."""
from __future__ import annotations

import logging

from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy import update

from .celery_app import celery_app
from .config import settings
from .models import GenTask, ParseRecord
from .observability import current_request_id
from .services import generation

log = logging.getLogger("tasks")

_LOCK_RETRY_COUNTDOWN_SECONDS = 30
_LOCK_RETRY_MAX = max(
    20,
    int((settings.celery_task_time_limit_seconds + 360) / _LOCK_RETRY_COUNTDOWN_SECONDS) + 1,
)


def enqueue_with_request_context(
    task,
    *args,
    countdown: int | None = None,
    task_id: str | None = None,
    **kwargs,
):
    """Enqueue a Celery task and propagate the current HTTP request id."""
    options = {"headers": {"x-request-id": current_request_id()}}
    if countdown is not None:
        options["countdown"] = countdown
    if task_id is not None:
        options["task_id"] = task_id
    return task.apply_async(args=args, kwargs=kwargs, **options)


def _soft_timeout_public_error(category: str) -> str:
    if category == "image":
        return "图片生成超时，已退回冻结积分，请稍后重试"
    if category == "video":
        return "视频生成超时，已退回冻结积分，请稍后重试"
    return "任务执行超时，请稍后重试"


def _fail_generation_soft_timeout(task_id: int, *, category: str) -> None:
    from .db import SessionLocal
    from .services.generation_common import fail_and_refund

    db = SessionLocal()
    try:
        message = _soft_timeout_public_error(category)
        if category == "video":
            task = db.get(GenTask, task_id)
            params = dict((task.params or {}) if task else {})
            request_id = params.get("_video_request_id") or params.get("request_id")
            if task and task.external_task_id:
                generation._hold_video_poll_for_reconciliation(db, task_id, message)
                return
            if task and request_id:
                generation._hold_video_submit_unknown_for_reconciliation(
                    db,
                    task_id,
                    message,
                    params_update=params,
                )
                return
        fail_and_refund(db, task_id, message, public_error=message)
    finally:
        db.close()


def _fail_parse_soft_timeout(parse_id: int) -> None:
    from .db import SessionLocal

    db = SessionLocal()
    try:
        db.execute(
            update(ParseRecord)
            .where(ParseRecord.id == parse_id, ParseRecord.status.in_(("queued", "running")))
            .values(status="failed", error="抓取任务执行超时,请重新提交链接")
        )
        db.commit()
    finally:
        db.close()


def _sync_workflow_external(external_kind: str, external_id: int) -> None:
    """Best-effort fast path; beat reconciliation covers interrupted callbacks."""
    try:
        from .services.workflow_node_adapters import sync_external_workflow_nodes

        sync_external_workflow_nodes(external_kind, external_id)
    except Exception:  # noqa: BLE001 - never change the domain task outcome
        log.exception(
            "workflow external completion sync failed kind=%s id=%s",
            external_kind,
            external_id,
        )


def _reconcile_reproduction_remediation(external_kind: str, external_id: int) -> None:
    """Best-effort fast path; beat reconciliation covers interrupted callbacks."""
    try:
        from .services import reproduction_remediation

        reproduction_remediation.reconcile_for_external(external_kind, int(external_id))
    except Exception:  # noqa: BLE001 - never change the domain task outcome
        log.exception(
            "reproduction remediation reconciliation failed kind=%s id=%s",
            external_kind,
            external_id,
        )


@celery_app.task(name="generate.image", bind=True, max_retries=_LOCK_RETRY_MAX)
def generate_image_task(self, task_id: int) -> None:
    try:
        generation.run_image_task(task_id)
    except SoftTimeLimitExceeded:
        _fail_generation_soft_timeout(task_id, category="image")
    except generation.TaskLockedError as e:
        raise self.retry(exc=e, countdown=_LOCK_RETRY_COUNTDOWN_SECONDS, max_retries=_LOCK_RETRY_MAX)
    finally:
        _sync_workflow_external("generation_task", task_id)
        _reconcile_reproduction_remediation("generation_task", task_id)


@celery_app.task(name="generate.video", bind=True, max_retries=_LOCK_RETRY_MAX)
def generate_video_task(self, task_id: int) -> None:
    # submit only; a self-re-enqueuing poll task drives it to completion so the
    # worker is never blocked across a long render.
    try:
        generation.start_video_task(task_id)
    except SoftTimeLimitExceeded:
        _fail_generation_soft_timeout(task_id, category="video")
    except generation.TaskLockedError as e:
        raise self.retry(exc=e, countdown=_LOCK_RETRY_COUNTDOWN_SECONDS, max_retries=_LOCK_RETRY_MAX)
    finally:
        _sync_workflow_external("generation_task", task_id)
        _reconcile_reproduction_remediation("generation_task", task_id)


@celery_app.task(name="poll.video", bind=True, max_retries=0)
def poll_video_task(self, task_id: int) -> None:
    try:
        generation.poll_video_once(task_id)
    except SoftTimeLimitExceeded:
        _fail_generation_soft_timeout(task_id, category="video")
    finally:
        _sync_workflow_external("generation_task", task_id)
        _reconcile_reproduction_remediation("generation_task", task_id)


@celery_app.task(name="download.video", bind=True, max_retries=0)
def download_video_task(
    self,
    task_id: int,
    expected_external_task_id: str | None = None,
) -> None:
    try:
        generation.run_video_download_task(task_id, expected_external_task_id)
    except SoftTimeLimitExceeded:
        _fail_generation_soft_timeout(task_id, category="video")
    finally:
        _sync_workflow_external("generation_task", task_id)
        _reconcile_reproduction_remediation("generation_task", task_id)


@celery_app.task(name="cleanup.resume_videos")
def resume_stuck_videos_task() -> int:
    """Re-attach polling to in-flight video renders whose poll chain died (worker
    crash). Run frequently via beat so submitted external jobs aren't stranded."""
    from .db import SessionLocal
    from .services import generation as gen

    db = SessionLocal()
    try:
        return gen.resume_stuck_videos(db)
    finally:
        db.close()


@celery_app.task(name="cleanup.expired_assets")
def cleanup_expired_assets_task() -> dict:
    """Purge expired assets + old tasks/parse-records/audit-logs. Schedule via
    celery beat or cron (see scripts/cleanup_expired.py)."""
    from .db import SessionLocal
    from .services import retention

    db = SessionLocal()
    try:
        return retention.purge_all(db)
    finally:
        db.close()


@celery_app.task(name="cleanup.archive_inactive_projects")
def archive_inactive_projects_task() -> dict[str, object]:
    """Archive active projects whose user-configured inactivity window elapsed."""
    from .db import SessionLocal
    from .services.project_collection import archive_inactive_projects

    db = SessionLocal()
    try:
        return archive_inactive_projects(db)
    finally:
        db.close()


@celery_app.task(name="cleanup.reap_stuck")
def reap_stuck_tasks_task() -> int:
    """Fail + refund tasks stuck past the timeout (worker crash safety net)."""
    from .db import SessionLocal
    from .services import retention

    db = SessionLocal()
    try:
        return retention.reap_stuck_tasks(db)
    finally:
        db.close()


@celery_app.task(name="cleanup.reconcile_generation_dispatches")
def reconcile_generation_dispatches_task() -> dict[str, int]:
    """Re-publish unresolved generation intents with their deterministic ID."""
    from .db import SessionLocal
    from .services.generation_dispatch import reconcile_dispatches

    db = SessionLocal()
    try:
        return reconcile_dispatches(db)
    finally:
        db.close()


@celery_app.task(name="cleanup.reconcile_workflow_dispatches")
def reconcile_workflow_dispatches_task() -> dict[str, int]:
    """Backfill and re-publish unresolved workflow outbox intents."""
    from .db import SessionLocal
    from .services import tool_workflows
    from .services.workflow_dispatch import reconcile_dispatches

    db = SessionLocal()
    try:
        orphaned = tool_workflows.ensure_orphaned_run_dispatches(db)
        reconciled = reconcile_dispatches(db)
        return {
            "orphan_scanned": int(orphaned["scanned"]),
            "orphan_created": int(orphaned["created"]),
            **reconciled,
        }
    finally:
        db.close()


@celery_app.task(name="cleanup.reconcile_workflow_externals")
def reconcile_workflow_externals_task() -> dict[str, int]:
    """Backstop domain-task callbacks for workflow nodes waiting externally."""
    from .services.workflow_node_adapters import reconcile_external_workflow_nodes

    return reconcile_external_workflow_nodes()


@celery_app.task(name="cleanup.reconcile_reproduction_remediations")
def reconcile_reproduction_remediations_task() -> dict[str, int]:
    """Advance remediation state after lost generation/workflow callbacks."""
    from .services import reproduction_remediation

    return reproduction_remediation.reconcile_all_remediations()


@celery_app.task(name="cleanup.reap_parse")
def reap_stuck_parse_records_task() -> int:
    """Fail queued parse records whose worker task disappeared."""
    from .db import SessionLocal
    from .services import retention

    db = SessionLocal()
    try:
        return retention.reap_stuck_parse_records(db)
    finally:
        db.close()


@celery_app.task(name="cleanup.reap_reverse")
def reap_stuck_reverse_operations_task() -> dict[str, int]:
    """Maintain async reverse operations and their frozen credits."""
    from .services import reverse_operations

    return reverse_operations.reap_operations()


@celery_app.task(name="cleanup.reap_prompt_optimizations")
def reap_stale_prompt_optimizations_task() -> int:
    """Refund paid prompt rewrites abandoned by an API process crash."""
    from .db import SessionLocal
    from .services.prompt_optimization import reap_stale_running_proposals

    db = SessionLocal()
    try:
        return reap_stale_running_proposals(db)
    finally:
        db.close()


@celery_app.task(
    name="reverse.run",
    acks_late=True,
    reject_on_worker_lost=True,
)
def reverse_operation_task(operation_id: int) -> None:
    """Execute one idempotently claimed reverse-prompt operation."""
    from .services import reverse_operations

    try:
        reverse_operations.run_operation(operation_id)
    except SoftTimeLimitExceeded:
        reverse_operations.fail_operation(
            operation_id,
            code="OPERATION_TIMEOUT",
            error="反推任务超时,已退回冻结积分",
        )
    finally:
        _sync_workflow_external("reverse_operation", operation_id)


@celery_app.task(name="reproduction.assess", acks_late=True, reject_on_worker_lost=True)
def run_reproduction_assessment_task(assessment_id: int) -> None:
    """Run one persisted source-versus-generation assessment."""
    from .services import reproduction_assessment

    try:
        reproduction_assessment.run_assessment(assessment_id)
    except SoftTimeLimitExceeded:
        reproduction_assessment._mark_failed(  # noqa: SLF001 - stable task timeout state
            assessment_id,
            reproduction_assessment.ReproductionAssessmentError("复刻度评估超时，请重试"),
        )
    finally:
        _reconcile_reproduction_remediation("reproduction_assessment", assessment_id)


@celery_app.task(
    name="workflow.run",
    acks_late=True,
    reject_on_worker_lost=True,
)
def workflow_run_task(run_id: int, dispatch_id: int | None = None) -> None:
    """Continue one workflow from its durable node state."""
    from .services import tool_workflows
    from .services.workflow_dispatch import mark_dispatch_delivered

    try:
        tool_workflows.run_workflow(run_id)
    except SoftTimeLimitExceeded:
        tool_workflows.fail_active_node(
            run_id,
            code="WORKFLOW_NODE_TIMEOUT",
            error="工作流节点执行超时",
        )
    finally:
        mark_dispatch_delivered(dispatch_id)
        _reconcile_reproduction_remediation("workflow_run", run_id)


@celery_app.task(
    name="workflow.node",
    acks_late=True,
    reject_on_worker_lost=True,
)
def workflow_node_task(
    run_id: int,
    node_id: int,
    dispatch_token: str,
    dispatch_id: int | None = None,
) -> None:
    """Execute one claimed node, then durably schedule the next DAG step."""
    from .services import tool_workflows
    from .services.workflow_dispatch import mark_dispatch_delivered

    try:
        tool_workflows.execute_claimed_node(run_id, node_id, dispatch_token)
    except SoftTimeLimitExceeded:
        tool_workflows.fail_active_node(
            run_id,
            code="WORKFLOW_NODE_TIMEOUT",
            error="工作流节点执行超时",
        )
    finally:
        mark_dispatch_delivered(dispatch_id)
        _reconcile_reproduction_remediation("workflow_run", run_id)
    tool_workflows.enqueue_run(run_id)


@celery_app.task(name="payments.reconcile")
def payment_reconcile_task() -> dict:
    """Backstop missed/late payment notifications by querying live providers."""
    from .db import SessionLocal
    from .services import payments

    db = SessionLocal()
    try:
        return payments.reconcile_pending_orders(db)
    finally:
        db.close()


@celery_app.task(name="parse.url")
def parse_url_task(parse_id: int) -> None:
    """Fetch and localize link assets outside the API request path."""
    from .routers import parse

    try:
        parse.run_parse_record(parse_id)
    except SoftTimeLimitExceeded:
        _fail_parse_soft_timeout(parse_id)
    finally:
        _sync_workflow_external("parse_record", parse_id)


# API and Celery worker processes import different entrypoints. Registering at
# task-module import gives workers the same production handlers as FastAPI.
from .workflow_adapter_wiring import (  # noqa: E402
    register_production_workflow_adapters,
)

register_production_workflow_adapters()
