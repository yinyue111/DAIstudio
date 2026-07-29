"""Production adapters between Tool Workflow nodes and durable domain tasks.

The workflow engine owns orchestration state.  This module only translates a
node request into an existing parse/reverse/compile/generation contract, then
binds asynchronous work to its durable domain record.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ValidationError
from sqlalchemy import select, update

from ..db import SessionLocal
from ..models import (
    GenTask,
    ParseRecord,
    ReverseOperation,
    ReverseResultRevision,
    ToolNodeRun,
    User,
    WorkflowRun,
)
from ..prompt_optimization_schemas import StudioPromptOptimizationIn
from ..schemas import GenerateIn, GenerationQuoteIn, ParseIn, ReverseOperationCreate
from . import prompt_optimization, reverse_operations, tool_workflows

log = logging.getLogger("workflow.node_adapters")

_TERMINAL_GENERATION_STATUSES = {"succeeded", "failed", "needs_review", "canceled"}
_CONFIG_CONTROL_KEYS = {
    "defaults",
    "request",
    "timeout_seconds",
    "asset_index",
}


def _unconfigured_dependency(*_args, **_kwargs):
    raise RuntimeError("工作流生产适配器尚未完成应用层依赖配置")


# Keep these namespaces as the stable adapter surface used by tests and local
# overrides. Application entrypoints populate them without making services
# import FastAPI router modules in the opposite dependency direction.
parse_router = SimpleNamespace(submit_parse=_unconfigured_dependency)
prompt_router = SimpleNamespace(create_reverse_operation=_unconfigured_dependency)
generate_router = SimpleNamespace(
    quote_generation=_unconfigured_dependency,
    generate=_unconfigured_dependency,
)
task_router = SimpleNamespace(
    build_task_out=_unconfigured_dependency,
    cancel_task=_unconfigured_dependency,
)


def configure_workflow_node_dependencies(
    *,
    submit_parse,
    create_reverse_operation,
    quote_generation,
    generate,
    build_task_out,
    cancel_task,
) -> None:
    """Bind application adapters at the composition root.

    The namespaces are mutated in place so existing monkeypatch and worker
    registrations keep the same object identity across repeated wiring calls.
    """
    parse_router.submit_parse = submit_parse
    prompt_router.create_reverse_operation = create_reverse_operation
    generate_router.quote_generation = quote_generation
    generate_router.generate = generate
    task_router.build_task_out = build_task_out
    task_router.cancel_task = cancel_task


def _internal_request(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 0),
            "server": ("workflow", 80),
        }
    )


@contextmanager
def _workflow_actor(context: tool_workflows.NodeExecutionContext):
    db = SessionLocal()
    try:
        run = db.get(WorkflowRun, int(context.workflow_run_id))
        if run is None:
            raise RuntimeError("工作流运行不存在")
        user = db.get(User, int(run.user_id))
        if user is None or user.status != "active":
            raise RuntimeError("工作流用户不存在或不可用")
        yield db, user
    finally:
        db.close()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _request_payload(
    context: tool_workflows.NodeExecutionContext,
    model: type[BaseModel],
) -> dict[str, Any]:
    """Resolve a node request while keeping catalog-owned overrides last.

    A run may use flat input fields or ``shared`` / node-type / node-key maps.
    Catalog config supports ``defaults`` and a final locked ``request`` map.
    Only fields declared by the destination Pydantic contract are forwarded.
    """
    allowed = set(model.model_fields)
    workflow_input = _mapping(context.workflow_input)
    config = _mapping(context.config)
    sources = [
        _mapping(config.get("defaults")),
        workflow_input,
        _mapping(workflow_input.get("shared")),
        _mapping(workflow_input.get(context.node_type)),
        _mapping(workflow_input.get(context.node_key)),
        {key: value for key, value in config.items() if key not in _CONFIG_CONTROL_KEYS},
        _mapping(config.get("request")),
    ]
    result: dict[str, Any] = {}
    for source in sources:
        result.update({key: value for key, value in source.items() if key in allowed})
    return result


def _iter_mappings(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_mappings(child)


def _dependency_lineage(context: tool_workflows.NodeExecutionContext) -> tuple[int, int] | None:
    for item in _iter_mappings(context.dependency_outputs):
        operation_id = item.get("reverse_operation_id")
        revision_id = item.get("reverse_revision_id")
        if operation_id and revision_id:
            try:
                return int(operation_id), int(revision_id)
            except (TypeError, ValueError):
                continue
    return None


def _dependency_asset(context: tool_workflows.NodeExecutionContext) -> dict[str, Any] | None:
    assets: list[dict[str, Any]] = []
    for item in _iter_mappings(context.dependency_outputs):
        raw_assets = item.get("assets")
        if isinstance(raw_assets, list):
            assets.extend(asset for asset in raw_assets if isinstance(asset, dict))
    if not assets:
        return None
    try:
        index = int(context.config.get("asset_index", 0))
    except (TypeError, ValueError):
        index = 0
    if index < 0 or index >= len(assets):
        raise ValueError("asset_index 超出解析素材范围")
    return assets[index]


def _dependency_prompt(context: tool_workflows.NodeExecutionContext) -> dict[str, Any] | None:
    """Return only an ordinary compiled prompt, never bypass reverse lineage."""
    for item in _iter_mappings(context.dependency_outputs):
        provenance = item.get("provenance")
        if isinstance(provenance, dict) and provenance.get("reverse_operation_id"):
            continue
        suggestion = item.get("suggestion")
        if isinstance(suggestion, dict) and (
            suggestion.get("final_text") or suggestion.get("structured")
        ):
            return dict(suggestion)
        prompt = item.get("compiled_prompt")
        if isinstance(prompt, str) and prompt.strip():
            return {"final_text": prompt.strip()}
    return None


def _default_request_id(context: tool_workflows.NodeExecutionContext, prefix: str) -> str:
    # Retries are execution attempts for the same logical side effect. Keeping
    # the attempt number out lets the domain service return the original task.
    return f"{prefix}-{int(context.workflow_run_id)}-{context.node_key}"[:128]


def _message_from_http(exc: HTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("detail") or detail.get("code") or detail)
    return str(detail)


def _failed_request(kind: str, exc: Exception) -> tool_workflows.NodeExecutionResult:
    if isinstance(exc, ValidationError):
        message = "; ".join(
            str(item.get("msg") or "输入无效") for item in exc.errors(include_url=False)[:5]
        )
        return tool_workflows.NodeExecutionResult.failed(
            message,
            code="NODE_INPUT_INVALID",
            output={"adapter": kind},
        )
    if isinstance(exc, HTTPException):
        return tool_workflows.NodeExecutionResult.failed(
            _message_from_http(exc),
            code=f"{kind.upper()}_REQUEST_REJECTED",
            output={"adapter": kind, "http_status": int(exc.status_code)},
        )
    status_code = getattr(exc, "status_code", None)
    return tool_workflows.NodeExecutionResult.failed(
        str(exc),
        code=f"{kind.upper()}_REQUEST_REJECTED",
        output={
            "adapter": kind,
            **({"http_status": int(status_code)} if status_code is not None else {}),
        },
    )


def _parse_output(record: ParseRecord) -> dict[str, Any]:
    return jsonable_encoder(
        {
            "parse_id": int(record.id),
            "status": record.status,
            "url": record.url,
            "assets": list(record.assets or []),
            "error": record.error,
        }
    )


def _parse_result(record: ParseRecord) -> tool_workflows.NodeExecutionResult:
    output = _parse_output(record)
    if record.status == "done":
        return tool_workflows.NodeExecutionResult.succeeded(output)
    if record.status == "failed":
        return tool_workflows.NodeExecutionResult.failed(
            record.error or "链接解析失败",
            code="PARSE_FAILED",
            output=output,
        )
    return tool_workflows.NodeExecutionResult.waiting_external(
        external_kind="parse_record",
        external_id=str(record.id),
        output=output,
    )


def parse_node(context: tool_workflows.NodeExecutionContext) -> tool_workflows.NodeExecutionResult:
    try:
        payload = _request_payload(context, ParseIn)
        body = ParseIn.model_validate(payload)
        with _workflow_actor(context) as (db, user):
            submitted = parse_router.submit_parse(
                body,
                request=_internal_request("/api/parse"),
                db=db,
                user=user,
            )
            db.expire_all()
            record = db.get(ParseRecord, int(submitted.id))
            if record is None or int(record.user_id) != int(user.id):
                raise RuntimeError("解析记录创建后不可见")
            return _parse_result(record)
    except Exception as exc:  # noqa: BLE001 - persisted as a node failure
        return _failed_request("parse", exc)


def _latest_reverse_revision(db, operation: ReverseOperation) -> ReverseResultRevision | None:
    return db.scalar(
        select(ReverseResultRevision)
        .where(
            ReverseResultRevision.operation_id == int(operation.id),
            ReverseResultRevision.source.in_(("normalized", "user_edit", "applied")),
        )
        .order_by(ReverseResultRevision.version.desc())
        .limit(1)
    )


def _reverse_output(db, operation: ReverseOperation) -> dict[str, Any]:
    revision = _latest_reverse_revision(db, operation)
    result = operation.normalized_result if isinstance(operation.normalized_result, dict) else {}
    return jsonable_encoder(
        {
            "reverse_operation_id": int(operation.id),
            "reverse_revision_id": int(revision.id) if revision is not None else None,
            "reverse_revision_source": revision.source if revision is not None else None,
            "status": operation.status,
            "phase": operation.phase,
            "progress": int(operation.progress or 0),
            "target": operation.target,
            "output_purpose": operation.output_purpose,
            "result_schema_version": operation.result_schema_version,
            "prompt_summary": {
                key: result.get(key)
                for key in ("final_text", "negative_prompt", "parameters")
                if result.get(key) is not None
            },
            "cost_frozen": int(operation.cost_frozen or 0),
            "cost_settled": int(operation.cost_settled or 0),
            "error_code": operation.error_code,
            "error": operation.error,
        }
    )


def _reverse_result(db, operation: ReverseOperation) -> tool_workflows.NodeExecutionResult:
    output = _reverse_output(db, operation)
    if operation.status == "succeeded":
        return tool_workflows.NodeExecutionResult.succeeded(output)
    if operation.status in {"failed", "canceled"}:
        return tool_workflows.NodeExecutionResult.failed(
            operation.error or ("反推任务已取消" if operation.status == "canceled" else "反推任务失败"),
            code=operation.error_code or ("CANCELED" if operation.status == "canceled" else "REVERSE_FAILED"),
            output=output,
        )
    return tool_workflows.NodeExecutionResult.waiting_external(
        external_kind="reverse_operation",
        external_id=str(operation.id),
        output=output,
    )


def reverse_node(context: tool_workflows.NodeExecutionContext) -> tool_workflows.NodeExecutionResult:
    try:
        payload = _request_payload(context, ReverseOperationCreate)
        payload.setdefault("client_request_id", _default_request_id(context, "workflow-reverse"))
        if not payload.get("asset_url") and not payload.get("sources"):
            asset = _dependency_asset(context)
            if asset is not None and asset.get("url"):
                payload["sources"] = [
                    {
                        "asset_url": asset["url"],
                        "source_type": asset.get("type") if asset.get("type") in {"image", "video"} else "image",
                        "role": "primary",
                    }
                ]
        body = ReverseOperationCreate.model_validate(payload)
        with _workflow_actor(context) as (db, user):
            submitted = prompt_router.create_reverse_operation(body, db=db, user=user)
            operation_id = int(submitted["id"] if isinstance(submitted, dict) else submitted.id)
            db.expire_all()
            operation = db.get(ReverseOperation, operation_id)
            if operation is None or int(operation.user_id) != int(user.id):
                raise RuntimeError("反推记录创建后不可见")
            return _reverse_result(db, operation)
    except Exception as exc:  # noqa: BLE001
        return _failed_request("reverse", exc)


def compile_node(context: tool_workflows.NodeExecutionContext) -> tool_workflows.NodeExecutionResult:
    try:
        payload = _request_payload(context, StudioPromptOptimizationIn)
        lineage = _dependency_lineage(context)
        if lineage and not payload.get("reverse_operation_id") and not payload.get("reverse_revision_id"):
            payload["reverse_operation_id"], payload["reverse_revision_id"] = lineage
        payload.setdefault("mode", "target_model_adaptation")
        payload.setdefault("idempotency_key", _default_request_id(context, "workflow-compile"))
        body = StudioPromptOptimizationIn.model_validate(payload)
        with _workflow_actor(context) as (db, user):
            proposal = prompt_optimization.create_proposal(db, user_id=int(user.id), body=body)
        suggestion = _mapping(proposal.get("suggestion"))
        return tool_workflows.NodeExecutionResult.succeeded(
            jsonable_encoder(
                {
                    **proposal,
                    "compiled_prompt": suggestion.get("final_text"),
                }
            )
        )
    except Exception as exc:  # noqa: BLE001
        return _failed_request("compile", exc)


def _generation_output(db, task: GenTask) -> dict[str, Any]:
    output = task_router.build_task_out(db, task).model_dump(mode="json")
    return {
        "generation_task_id": int(task.id),
        **jsonable_encoder(output),
    }


def _generation_result(db, task: GenTask) -> tool_workflows.NodeExecutionResult:
    output = _generation_output(db, task)
    if task.status == "succeeded":
        return tool_workflows.NodeExecutionResult.succeeded(output)
    if task.status in _TERMINAL_GENERATION_STATUSES:
        code = {
            "failed": "GENERATION_FAILED",
            "needs_review": "GENERATION_NEEDS_REVIEW",
            "canceled": "CANCELED",
        }.get(task.status, "GENERATION_FAILED")
        return tool_workflows.NodeExecutionResult.failed(
            task.error or f"生成任务状态为 {task.status}",
            code=code,
            output=output,
        )
    return tool_workflows.NodeExecutionResult.waiting_external(
        external_kind="generation_task",
        external_id=str(task.id),
        output=output,
    )


def generate_node(context: tool_workflows.NodeExecutionContext) -> tool_workflows.NodeExecutionResult:
    try:
        payload = _request_payload(context, GenerationQuoteIn)
        payload.setdefault("client_request_id", _default_request_id(context, "workflow-generate"))
        lineage = _dependency_lineage(context)
        if lineage and not payload.get("reverse_operation_id") and not payload.get("reverse_revision_id"):
            payload["reverse_operation_id"], payload["reverse_revision_id"] = lineage
        if not payload.get("source_asset_url"):
            asset = _dependency_asset(context)
            if asset is not None and asset.get("url"):
                payload["source_asset_url"] = asset["url"]
                if asset.get("type") in {"image", "video"}:
                    payload["source_type"] = asset["type"]
        if not payload.get("prompt") and not payload.get("instruction") and not lineage:
            compiled_prompt = _dependency_prompt(context)
            if compiled_prompt:
                payload["prompt"] = compiled_prompt
        quote_body = GenerationQuoteIn.model_validate(payload)
        with _workflow_actor(context) as (db, user):
            quote = generate_router.quote_generation(quote_body, db=db, user=user)
            quote_id = int(quote["quote_id"] if isinstance(quote, dict) else quote.quote_id)
            body = GenerateIn.model_validate({**quote_body.model_dump(), "quote_id": quote_id})
            submitted = generate_router.generate(
                body,
                request=_internal_request("/api/generate"),
                response=Response(),
                db=db,
                user=user,
            )
            task_id = int(submitted.id)
            db.expire_all()
            task = db.get(GenTask, task_id)
            if task is None or int(task.user_id) != int(user.id):
                raise RuntimeError("生成记录创建后不可见")
            return _generation_result(db, task)
    except Exception as exc:  # noqa: BLE001
        return _failed_request("generate", exc)


def _video_composition_failure(
    node_type: str,
    exc: Exception,
) -> tool_workflows.NodeExecutionResult:
    capability_status = getattr(exc, "capability_status", None)
    output = {"adapter": node_type}
    if capability_status:
        output.update(
            {
                "capability": f"video_{node_type}",
                "capability_status": str(capability_status),
            }
        )
    return tool_workflows.NodeExecutionResult.failed(
        str(exc),
        code=str(getattr(exc, "code", None) or f"VIDEO_{node_type.upper()}_FAILED"),
        output=output,
    )


def compose_node(context: tool_workflows.NodeExecutionContext) -> tool_workflows.NodeExecutionResult:
    try:
        from . import video_composition

        return tool_workflows.NodeExecutionResult.succeeded(
            jsonable_encoder(video_composition.compose_workflow_node(context))
        )
    except Exception as exc:  # noqa: BLE001 - persisted as a node failure
        return _video_composition_failure("compose", exc)


def export_node(context: tool_workflows.NodeExecutionContext) -> tool_workflows.NodeExecutionResult:
    try:
        from . import video_composition

        return tool_workflows.NodeExecutionResult.succeeded(
            jsonable_encoder(video_composition.export_workflow_node(context))
        )
    except Exception as exc:  # noqa: BLE001 - persisted as a node failure
        return _video_composition_failure("export", exc)


def cleanup_video_composition(
    context: tool_workflows.NodeExecutionContext,
) -> tool_workflows.NodeExecutionResult:
    try:
        from . import video_composition

        return tool_workflows.NodeExecutionResult.succeeded(
            jsonable_encoder(video_composition.compensate_workflow_node(context))
        )
    except Exception as exc:  # noqa: BLE001 - persisted as a compensation failure
        return _video_composition_failure("compensation", exc)


def _external_record_result(
    db,
    *,
    external_kind: str,
    external_id: str,
    expected_user_id: int,
) -> tool_workflows.NodeExecutionResult | None:
    try:
        record_id = int(external_id)
    except (TypeError, ValueError):
        return tool_workflows.NodeExecutionResult.failed(
            "外部任务 ID 非法",
            code="INVALID_EXTERNAL_BINDING",
        )
    if external_kind == "parse_record":
        record = db.get(ParseRecord, record_id)
        if record is None:
            return tool_workflows.NodeExecutionResult.failed("解析记录不存在", code="EXTERNAL_TASK_NOT_FOUND")
        if int(record.user_id) != expected_user_id:
            return tool_workflows.NodeExecutionResult.failed("解析记录所有者不匹配", code="EXTERNAL_OWNER_MISMATCH")
        return None if record.status in {"queued", "running"} else _parse_result(record)
    if external_kind == "reverse_operation":
        operation = db.get(ReverseOperation, record_id)
        if operation is None:
            return tool_workflows.NodeExecutionResult.failed("反推记录不存在", code="EXTERNAL_TASK_NOT_FOUND")
        if int(operation.user_id) != expected_user_id:
            return tool_workflows.NodeExecutionResult.failed("反推记录所有者不匹配", code="EXTERNAL_OWNER_MISMATCH")
        if operation.status in {"queued", "running", "needs_confirmation"}:
            return None
        return _reverse_result(db, operation)
    if external_kind == "generation_task":
        task = db.get(GenTask, record_id)
        if task is None:
            return tool_workflows.NodeExecutionResult.failed("生成记录不存在", code="EXTERNAL_TASK_NOT_FOUND")
        if int(task.user_id) != expected_user_id:
            return tool_workflows.NodeExecutionResult.failed("生成记录所有者不匹配", code="EXTERNAL_OWNER_MISMATCH")
        if task.status not in _TERMINAL_GENERATION_STATUSES:
            return None
        return _generation_result(db, task)
    return tool_workflows.NodeExecutionResult.failed(
        f"不支持的外部任务类型: {external_kind}",
        code="EXTERNAL_KIND_UNSUPPORTED",
    )


def sync_external_workflow_nodes(external_kind: str, external_id: str | int) -> dict[str, int]:
    """Complete every matching waiting node after a domain task reaches terminal state."""
    external_id = str(external_id)
    db = SessionLocal()
    run_ids: set[int] = set()
    counts = {"matched": 0, "completed": 0, "pending": 0, "conflicts": 0}
    try:
        nodes = list(
            db.scalars(
                select(ToolNodeRun).where(
                    ToolNodeRun.status == "waiting_external",
                    ToolNodeRun.external_kind == external_kind,
                    ToolNodeRun.external_id == external_id,
                )
            )
        )
        counts["matched"] = len(nodes)
        for node in nodes:
            run = db.get(WorkflowRun, int(node.workflow_run_id))
            if run is None:
                counts["conflicts"] += 1
                continue
            result = _external_record_result(
                db,
                external_kind=external_kind,
                external_id=external_id,
                expected_user_id=int(run.user_id),
            )
            if result is None or result.status == "waiting_external":
                counts["pending"] += 1
                continue
            try:
                tool_workflows.complete_external_node(
                    db,
                    run_id=int(run.id),
                    node_key=node.node_key,
                    status="succeeded" if result.status == "succeeded" else "failed",
                    output=dict(result.output or {}),
                    error_code=result.error_code,
                    error=result.error,
                    external_kind=external_kind,
                    external_id=external_id,
                )
            except tool_workflows.WorkflowConflict:
                db.rollback()
                counts["conflicts"] += 1
                continue
            counts["completed"] += 1
            run_ids.add(int(run.id))
    finally:
        db.close()
    for run_id in run_ids:
        try:
            tool_workflows.enqueue_run(run_id)
        except Exception:  # noqa: BLE001 - durable state is reconciled by the periodic task
            log.exception("failed to resume workflow run=%s after external completion", run_id)
    return counts


def reconcile_external_workflow_nodes(*, limit: int = 200) -> dict[str, int]:
    db = SessionLocal()
    try:
        bindings = list(
            db.execute(
                select(ToolNodeRun.external_kind, ToolNodeRun.external_id)
                .where(
                    ToolNodeRun.status == "waiting_external",
                    ToolNodeRun.external_kind.is_not(None),
                    ToolNodeRun.external_id.is_not(None),
                )
                .distinct()
                .limit(max(1, min(int(limit), 500)))
            )
        )
    finally:
        db.close()
    totals = {"bindings": len(bindings), "completed": 0, "pending": 0, "conflicts": 0}
    for external_kind, external_id in bindings:
        result = sync_external_workflow_nodes(str(external_kind), str(external_id))
        for key in ("completed", "pending", "conflicts"):
            totals[key] += int(result[key])
    return totals


def cancel_external_task(
    context: tool_workflows.NodeExecutionContext,
) -> tool_workflows.NodeExecutionResult:
    external_kind = str(context.external_kind or "")
    external_id = str(context.external_id or "")
    if not external_kind or not external_id:
        return tool_workflows.NodeExecutionResult.failed(
            "补偿节点缺少外部任务绑定",
            code="INVALID_EXTERNAL_BINDING",
        )
    try:
        record_id = int(external_id)
    except ValueError:
        return tool_workflows.NodeExecutionResult.failed(
            "外部任务 ID 非法",
            code="INVALID_EXTERNAL_BINDING",
        )
    try:
        with _workflow_actor(context) as (db, user):
            if external_kind == "parse_record":
                record = db.get(ParseRecord, record_id)
                if record is None or int(record.user_id) != int(user.id):
                    raise RuntimeError("解析记录不存在")
                if record.status in {"done", "failed"}:
                    return tool_workflows.NodeExecutionResult.succeeded(
                        {"external_kind": external_kind, "external_id": external_id, "status": record.status}
                    )
                if record.status == "running":
                    return tool_workflows.NodeExecutionResult.failed(
                        "链接解析已开始且底层抓取器不支持安全中断",
                        code="EXTERNAL_CANCEL_UNSUPPORTED",
                    )
                db.execute(
                    update(ParseRecord)
                    .where(
                        ParseRecord.id == record_id,
                        ParseRecord.user_id == int(user.id),
                        ParseRecord.status == "queued",
                    )
                    .values(status="failed", error="所属工作流已取消")
                )
                db.commit()
                return tool_workflows.NodeExecutionResult.succeeded(
                    {"external_kind": external_kind, "external_id": external_id, "status": "canceled_before_start"}
                )
            if external_kind == "reverse_operation":
                operation = reverse_operations.request_cancel(
                    db,
                    operation_id=record_id,
                    user_id=int(user.id),
                )
                return tool_workflows.NodeExecutionResult.succeeded(
                    {
                        "external_kind": external_kind,
                        "external_id": external_id,
                        "status": operation.status,
                        "cancel_requested": bool(operation.cancel_requested),
                    }
                )
            if external_kind == "generation_task":
                task = task_router.cancel_task(record_id, db=db, user=user)
                return tool_workflows.NodeExecutionResult.succeeded(
                    {
                        "external_kind": external_kind,
                        "external_id": external_id,
                        "status": task.status,
                    }
                )
            return tool_workflows.NodeExecutionResult.unsupported(
                f"cancel_{external_kind}",
                reason=f"外部任务类型 {external_kind} 没有取消适配器",
            )
    except Exception as exc:  # noqa: BLE001
        return _failed_request("external_cancel", exc)


def register_production_workflow_adapters(*, overwrite: bool = True) -> None:
    handlers = {
        "parse": parse_node,
        "reverse": reverse_node,
        "compile": compile_node,
        "generate": generate_node,
        "compose": compose_node,
        "export": export_node,
    }
    for node_type, handler in handlers.items():
        if overwrite or not tool_workflows.has_node_handler(node_type):
            tool_workflows.register_node_handler(node_type, handler)
    if overwrite or not tool_workflows.has_compensation_handler("cancel_external_task"):
        tool_workflows.register_compensation_handler("cancel_external_task", cancel_external_task)
    if overwrite or not tool_workflows.has_compensation_handler("cleanup_video_composition"):
        tool_workflows.register_compensation_handler(
            "cleanup_video_composition",
            cleanup_video_composition,
        )
