"""Shared validation and request rebuilding for generation retries."""

from __future__ import annotations

from copy import deepcopy

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import GenerationDispatch, GenTask, ReverseResultRevision
from ..schemas import GenerationQuoteIn
from . import project_collection, reverse_lineage
from .generation_submit import validate_generation_lineage

_LINEAGE_FIELDS = (
    "reverse_operation_id",
    "source_revision_id",
    "compiled_revision_id",
    "generation_revision_id",
)
_LEGACY_LINEAGE_PARAM_FIELDS = (
    "reverse_operation_id",
    "reverse_revision_id",
    "source_revision_id",
    "compiled_revision_id",
    "generation_revision_id",
)
_UNRECONCILED_DISPATCH_STATUSES = frozenset({"pending", "publishing", "unknown", "needs_review"})


def public_retry_params(task: GenTask) -> dict:
    """Return only user-facing parameters so a new task gets fresh runtime state."""
    params = task.params if isinstance(task.params, dict) else {}
    return {
        str(key): value
        for key, value in params.items()
        if not str(key).startswith("_")
    }


def source_route_id(task: GenTask) -> int | None:
    params = task.params if isinstance(task.params, dict) else {}
    snapshot = params.get("_model_snapshot")
    route = snapshot.get("route_snapshot") if isinstance(snapshot, dict) else None
    value = route.get("route_id") if isinstance(route, dict) else None
    return _positive_id(value)


def build_requote_input(
    db: Session,
    *,
    task: GenTask,
    user_id: int,
    client_request_id: str,
    model_config_id: int | None = None,
) -> GenerationQuoteIn:
    """Rebuild an executable request from one immutable failed task."""
    params = task.params if isinstance(task.params, dict) else {}
    source_trace = params.get("_source_trace")
    project_id = project_collection.primary_project_id_for_task(
        db,
        user_id=user_id,
        task_kind="generation",
        task_id=int(task.id),
    )
    shot_context = params.get("_shot_context")
    selected_model_config_id = requote_model_config_id(task, model_config_id)
    source_model_config_id = _positive_id(task.model_config_id)
    model_switched = source_model_config_id != selected_model_config_id
    return GenerationQuoteIn(
        client_request_id=client_request_id,
        project_id=project_id,
        reverse_operation_id=_positive_id(task.reverse_operation_id),
        reverse_revision_id=_positive_id(task.source_revision_id),
        source_asset_url=task.source_asset_url,
        source_type=task.source_type if task.source_type in {"image", "video"} else "image",
        source_asset_meta=deepcopy(source_trace) if isinstance(source_trace, dict) else None,
        shot_context=deepcopy(shot_context) if isinstance(shot_context, dict) else None,
        category=task.category,
        stage=task.stage,
        # Preserve legacy final-to-preview association while fresh_route makes
        # pricing and provider selection current instead of inheriting its snapshot.
        parent_task_id=None if model_switched else _positive_id(task.parent_task_id),
        prompt=deepcopy(task.prompt) if isinstance(task.prompt, dict) else None,
        params=public_retry_params(task),
        model_config_id=selected_model_config_id,
    )


def requote_model_config_id(task: GenTask, requested_model_config_id: int | None) -> int:
    selected = _positive_id(requested_model_config_id) or _positive_id(task.model_config_id)
    if selected is None:
        raise HTTPException(
            409,
            detail={
                "code": "RETRY_SOURCE_NOT_REPLAYABLE",
                "message": "原任务缺少模型配置,请选择兼容模型后重新报价",
            },
        )
    return selected


def assert_requote_replay(
    existing: GenTask,
    *,
    source_task_id: int,
    selected_model_config_id: int,
) -> None:
    if (
        int(existing.retry_of_task_id or 0) != int(source_task_id)
        or int(existing.model_config_id or 0) != int(selected_model_config_id)
    ):
        raise HTTPException(
            409,
            detail={
                "code": "IDEMPOTENCY_CONFLICT",
                "message": "client_request_id 已用于其他重试请求或模型",
            },
        )


def assert_retry_dispatch_reconciled(db: Session, task: GenTask) -> None:
    latest = db.scalar(
        select(GenerationDispatch)
        .where(GenerationDispatch.task_id == int(task.id))
        .order_by(GenerationDispatch.attempt.desc())
        .limit(1)
    )
    if latest is not None and latest.status in _UNRECONCILED_DISPATCH_STATUSES:
        raise HTTPException(
            409,
            detail={
                "code": "RETRY_SOURCE_UNRECONCILED",
                "message": "原任务仍在派发核对中,确认未提交上游后才能重试",
            },
        )


def assert_retry_generation_lineage(db: Session, task: GenTask) -> None:
    """Revalidate the complete persisted reverse lineage before any retry side effect."""
    params = task.params if isinstance(task.params, dict) else {}
    persisted = {field: _positive_id(getattr(task, field, None)) for field in _LINEAGE_FIELDS}
    raw_param_lineage = params.get("_reverse_lineage")
    has_param_lineage = "_reverse_lineage" in params or any(
        field in params for field in _LEGACY_LINEAGE_PARAM_FIELDS
    )
    if not any(persisted.values()) and not has_param_lineage:
        return
    if not all(persisted.values()):
        _lineage_error("缺少持久化的完整反推版本链")

    expected_param_lineage = {
        "operation_id": persisted["reverse_operation_id"],
        "source_revision_id": persisted["source_revision_id"],
        "compiled_revision_id": persisted["compiled_revision_id"],
        "generation_revision_id": persisted["generation_revision_id"],
    }
    if "_reverse_lineage" in params and (
        not isinstance(raw_param_lineage, dict)
        or any(
            _positive_id(raw_param_lineage.get(field)) != value
            for field, value in expected_param_lineage.items()
        )
    ):
        _lineage_error("任务参数中的反推血缘与持久化版本不一致")

    legacy_expected = {
        "reverse_operation_id": persisted["reverse_operation_id"],
        "reverse_revision_id": persisted["source_revision_id"],
        "source_revision_id": persisted["source_revision_id"],
        "compiled_revision_id": persisted["compiled_revision_id"],
        "generation_revision_id": persisted["generation_revision_id"],
    }
    if any(
        field in params and _positive_id(params.get(field)) != legacy_expected[field]
        for field in _LEGACY_LINEAGE_PARAM_FIELDS
    ):
        _lineage_error("任务参数中的旧版反推血缘与持久化版本不一致")

    try:
        operation, source_revision = validate_generation_lineage(
            db,
            user_id=int(task.user_id),
            reverse_operation_id=persisted["reverse_operation_id"],
            reverse_revision_id=persisted["source_revision_id"],
            for_update=True,
        )
    except HTTPException as exc:
        _lineage_error(_http_error_message(exc))

    revision_ids = (
        persisted["compiled_revision_id"],
        persisted["generation_revision_id"],
    )
    revisions = {
        int(revision.id): revision
        for revision in db.scalars(
            select(ReverseResultRevision)
            .where(ReverseResultRevision.id.in_(revision_ids))
            .with_for_update()
        )
    }
    compiled_revision = revisions.get(int(persisted["compiled_revision_id"]))
    generation_revision = revisions.get(int(persisted["generation_revision_id"]))
    if compiled_revision is None or generation_revision is None:
        _lineage_error("任务缺少已编译或生成版本")

    try:
        chain = reverse_lineage.validate_revision_chain(
            db,
            generation_revision,
            terminal_source="generation",
        )
    except reverse_lineage.ReverseLineageError as exc:
        _lineage_error(str(exc))

    sources = [revision.source for revision in chain]
    if (
        sources[:3] != ["generation", "model_compiled", "applied"]
        or sources[-2:] != ["normalized", "provider_raw"]
        or not sources[3:-2]
        or any(source != "user_edit" for source in sources[3:-2])
    ):
        _lineage_error("反推版本链不完整")
    by_source = {revision.source: revision for revision in chain}
    if (
        operation is None
        or source_revision is None
        or int(operation.id) != persisted["reverse_operation_id"]
        or int(source_revision.id) != persisted["source_revision_id"]
        or int(by_source["applied"].id) != persisted["source_revision_id"]
        or int(by_source["model_compiled"].id) != persisted["compiled_revision_id"]
        or int(by_source["generation"].id) != persisted["generation_revision_id"]
        or any(
            int(revision.operation_id) != persisted["reverse_operation_id"]
            or int(revision.user_id) != int(task.user_id)
            for revision in chain
        )
    ):
        _lineage_error("反推任务、版本或所有者与生成任务不一致")

    compiled_payload = (
        compiled_revision.payload if isinstance(compiled_revision.payload, dict) else {}
    )
    generation_payload = (
        generation_revision.payload if isinstance(generation_revision.payload, dict) else {}
    )
    compiled_source = (
        compiled_payload.get("source") if isinstance(compiled_payload.get("source"), dict) else {}
    )
    generation_source = (
        generation_payload.get("source")
        if isinstance(generation_payload.get("source"), dict)
        else {}
    )
    generation_meta = (
        generation_payload.get("generation")
        if isinstance(generation_payload.get("generation"), dict)
        else {}
    )
    if (
        _positive_id(compiled_payload.get("generation_task_id")) != int(task.id)
        or _positive_id(compiled_source.get("operation_id")) != persisted["reverse_operation_id"]
        or _positive_id(compiled_source.get("revision_id")) != persisted["source_revision_id"]
        or _positive_id(generation_source.get("operation_id")) != persisted["reverse_operation_id"]
        or _positive_id(generation_source.get("revision_id")) != persisted["source_revision_id"]
        or _positive_id(generation_payload.get("compiled_revision_id"))
        != persisted["compiled_revision_id"]
        or _positive_id(generation_meta.get("task_id")) != int(task.id)
    ):
        _lineage_error("反推版本载荷与生成任务不一致")


def _positive_id(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return int(value)


def _http_error_message(exc: HTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("detail") or "反推血缘校验失败")
    return str(detail or "反推血缘校验失败")


def _lineage_error(reason: str) -> None:
    raise HTTPException(
        409,
        detail={
            "code": "RETRY_LINEAGE_INVALID",
            "message": f"任务反推血缘无效,无法重试: {reason}",
        },
    )
