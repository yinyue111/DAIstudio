"""Unified, read-only task projection across every durable task engine."""

from __future__ import annotations

import base64
import binascii
import json
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..models import (
    GenAsset,
    GenerationDispatch,
    GenTask,
    MediaProjectTask,
    ModelConfig,
    ParseRecord,
    ReverseOperation,
    ToolDefinition,
    ToolNodeRun,
    ToolRun,
    WorkflowRun,
)
from ..schemas import ReverseOperationCreate
from .error_codes import classify_error_type, task_error_type
from .generation_request import estimate_generation_cost
from .generation_state import TERMINAL_STATUSES
from .model_capabilities import (
    ModelCapabilityError,
    assert_generation_capability,
    assert_reverse_capability,
)
from .model_gateway_config import ModelGatewayConfigError, runtime_config_for_model
from .model_routes import model_route_summary
from .progress import get_progress
from .user_assets import generated_asset_ref

_KIND_ORDER = {"workflow": 4, "generation": 3, "reverse": 2, "parse": 1}
_STATUS_GROUPS = (
    "active",
    "succeeded",
    "failed",
    "canceled",
    "needs_attention",
)
_RAW_STATUS_GROUPS = {
    "generation": {
        "queued": "active",
        "running": "active",
        "succeeded": "succeeded",
        "failed": "failed",
        "canceled": "canceled",
        "needs_review": "needs_attention",
    },
    "reverse": {
        "queued": "active",
        "running": "active",
        "needs_confirmation": "needs_attention",
        "succeeded": "succeeded",
        "failed": "failed",
        "canceled": "canceled",
    },
    "parse": {
        "queued": "active",
        "running": "active",
        "done": "succeeded",
        "failed": "failed",
    },
    "workflow": {
        "queued": "active",
        "running": "active",
        "waiting_review": "needs_attention",
        "succeeded": "succeeded",
        "failed": "failed",
        "canceled": "canceled",
        "compensating": "active",
    },
}


class InvalidTaskCursor(ValueError):
    pass


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _row_sort_key(kind: str, row: Any) -> tuple[datetime, int, int]:
    return (_utc(row.created_at), _KIND_ORDER[kind], int(row.id))


def _encode_cursor(kind: str, row: Any) -> str:
    payload = {
        "created_at": _utc(row.created_at).isoformat(),
        "kind": kind,
        "id": int(row.id),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(value: str | None) -> tuple[datetime, int, int] | None:
    if not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        kind = str(payload["kind"])
        if kind not in _KIND_ORDER:
            raise ValueError
        created_at = datetime.fromisoformat(str(payload["created_at"]).replace("Z", "+00:00"))
        task_id = int(payload["id"])
        if task_id <= 0:
            raise ValueError
        return (_utc(created_at), _KIND_ORDER[kind], task_id)
    except (KeyError, TypeError, ValueError, UnicodeError, binascii.Error) as exc:
        raise InvalidTaskCursor("任务游标无效") from exc


def _raw_statuses(kind: str, status_group: str) -> set[str] | None:
    if status_group == "all":
        return None
    return {raw for raw, group in _RAW_STATUS_GROUPS[kind].items() if group == status_group}


def _category_filter(kind: str, model: Any, category: str):
    if category == "all":
        return None
    if kind == "generation":
        return model.category == category
    if kind == "reverse":
        return model.target == category
    # ToolDefinition categories describe the catalog surface, not whether a
    # workflow ultimately emits image or video media. Do not misclassify these
    # runs under a media-only filter.
    if kind == "workflow":
        return False
    return False


def _load_kind_rows(
    db: Session,
    *,
    kind: str,
    model: Any,
    user_id: int,
    status_group: str,
    category: str,
    cursor_key: tuple[datetime, int, int] | None,
    limit: int,
) -> list[Any]:
    category_clause = _category_filter(kind, model, category)
    if category_clause is False:
        return []
    statuses = _raw_statuses(kind, status_group)
    if statuses == set():
        return []
    stmt = select(model).where(model.user_id == user_id)
    if category_clause is not None:
        stmt = stmt.where(category_clause)
    if statuses is not None:
        stmt = stmt.where(model.status.in_(statuses))
    if cursor_key is not None:
        stmt = stmt.where(model.created_at <= cursor_key[0])
    return list(
        db.scalars(stmt.order_by(model.created_at.desc(), model.id.desc()).limit(limit + 1))
    )


def _truncate(value: object, limit: int = 160) -> str | None:
    text = " ".join(str(value or "").split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "..."


def _positive_id(value: object) -> int | None:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _source_model_identity(row: GenTask | ReverseOperation) -> tuple[int | None, str]:
    if isinstance(row, ReverseOperation):
        snapshot_value = row.model_snapshot
    else:
        params = row.params if isinstance(row.params, dict) else {}
        snapshot_value = params.get("_model_snapshot")
    snapshot = snapshot_value if isinstance(snapshot_value, dict) else {}
    return (
        _positive_id(row.model_config_id) or _positive_id(snapshot.get("model_config_id")),
        str(snapshot.get("model_id") or "").strip(),
    )


def _same_source_model(
    model: ModelConfig,
    *,
    source_model_config_id: int | None,
    source_model_id: str,
) -> bool:
    if source_model_config_id is not None:
        return int(model.id) == source_model_config_id
    return bool(source_model_id and str(model.model_id).strip() == source_model_id)


def _generation_retry_reason(task: GenTask, params: dict[str, Any]) -> str:
    if task.category == "image":
        return "支持当前图片参考与编辑参数" if (
            task.source_asset_url
            or any(params.get(key) for key in (
                "reference_image_url",
                "first_frame_image",
                "style_reference_image",
                "character_reference_image",
                "product_reference_image",
            ))
        ) else "支持当前文生图参数"
    if task.source_type == "video" and task.source_asset_url:
        return "支持当前视频参考生成"
    if task.source_asset_url or any(params.get(key) for key in (
        "reference_image_url",
        "first_frame_image",
        "last_frame_image",
        "style_reference_image",
        "character_reference_image",
        "product_reference_image",
    )):
        return "支持当前图片参考与视频参数"
    return "支持当前文生视频参数"


def _generation_retry_models(
    db: Session,
    task: GenTask,
    *,
    models: list[ModelConfig],
    route_summaries: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    from .generation_retry import public_retry_params

    params = public_retry_params(task)
    source_model_config_id, source_model_id = _source_model_identity(task)
    candidates: list[dict[str, Any]] = []
    for model in models:
        if model.use != task.category or _same_source_model(
            model,
            source_model_config_id=source_model_config_id,
            source_model_id=source_model_id,
        ):
            continue
        try:
            assert_generation_capability(
                model,
                category=task.category,
                source_asset_url=task.source_asset_url,
                source_type=task.source_type,
                params=params,
            )
            route = route_summaries.get(int(model.id))
            if route is None:
                route = model_route_summary(db, model)
                route_summaries[int(model.id)] = route
            if route.get("status") != "available" or int(route.get("available") or 0) <= 0:
                continue
            try:
                image_count = int(params.get("n") or 1) if task.category == "image" else 1
            except (TypeError, ValueError):
                image_count = 1
            estimated_credits = estimate_generation_cost(
                model,
                task.category,
                task.stage,
                max(1, image_count),
                params=params,
                source_type=task.source_type,
            )
        except (ModelCapabilityError, TypeError, ValueError):
            continue
        candidates.append({
            "model_config_id": int(model.id),
            "display_name": str(model.display_name or model.model_id),
            "model_id": str(model.model_id),
            "provider": str(model.provider or "platform_route"),
            "estimated_credits": max(0, int(estimated_credits)),
            "route_status": str(route.get("status") or "unavailable"),
            "reason": _generation_retry_reason(task, params),
        })
    return candidates


def _reverse_source_type(operation: ReverseOperation) -> str:
    context = operation.request_context if isinstance(operation.request_context, dict) else {}
    value = str(context.get("source_type") or "").strip().lower()
    if value in {"image", "video"}:
        return value
    path = str(operation.asset_url or "").split("?", 1)[0].lower()
    return "video" if path.endswith((".mp4", ".webm", ".mov", ".m3u8")) else "image"


def _reverse_retry_reason(operation: ReverseOperation, source_type: str) -> str:
    if operation.target == "product_profile":
        return "支持商品档案识别"
    if operation.target == "portrait_profile":
        return "支持人物档案识别"
    if operation.target == "video" and source_type == "video":
        return "支持视频证据反推"
    if operation.target == "video":
        return "支持图片到视频提示词分析"
    return "支持图片证据反推"


def _reverse_retry_body(
    operation: ReverseOperation,
    *,
    model_config_id: int,
    source_type: str,
) -> ReverseOperationCreate:
    context = deepcopy(operation.request_context) if isinstance(operation.request_context, dict) else {}
    context.pop("quote_id", None)
    context.update({
        "client_request_id": f"task-center-{int(operation.id)}-{model_config_id}",
        "asset_url": operation.asset_url,
        "target": operation.target,
        "source_type": source_type,
        "model_config_id": model_config_id,
        "analysis_focus": operation.analysis_focus,
        "analysis_precision": operation.analysis_precision,
        "output_purpose": operation.output_purpose,
        "include_audio": bool(operation.include_audio),
    })
    return ReverseOperationCreate.model_validate(context)


def _reverse_retry_models(
    db: Session,
    operation: ReverseOperation,
    *,
    models: list[ModelConfig],
    runtime_cache: dict[int, Any | None],
) -> list[dict[str, Any]]:
    from . import project_collection, reverse_operations

    source_model_config_id, source_model_id = _source_model_identity(operation)
    source_type = _reverse_source_type(operation)
    candidates: list[dict[str, Any]] = []
    for model in models:
        if model.use != "vision" or _same_source_model(
            model,
            source_model_config_id=source_model_config_id,
            source_model_id=source_model_id,
        ):
            continue
        try:
            assert_reverse_capability(
                model,
                target=operation.target,
                source_type=source_type,
            )
        except ModelCapabilityError:
            continue

        model_id = int(model.id)
        if model_id not in runtime_cache:
            try:
                runtime_cache[model_id] = runtime_config_for_model(model, "vision")
            except ModelGatewayConfigError:
                runtime_cache[model_id] = None
        runtime = runtime_cache[model_id]
        if (
            runtime is None
            or not runtime.configured
            or runtime.gateway_format not in {"openai", "anthropic", "ark"}
        ):
            continue

        try:
            body = _reverse_retry_body(
                operation,
                model_config_id=model_id,
                source_type=source_type,
            )
            prepared = reverse_operations.prepare_reverse_quote(
                db,
                user_id=int(operation.user_id),
                body=body,
            )
        except (
            HTTPException,
            ValidationError,
            ValueError,
            project_collection.ProjectNotFound,
            reverse_operations.ReverseOperationInvalid,
        ):
            continue
        candidates.append({
            "model_config_id": model_id,
            "display_name": str(model.display_name or model.model_id),
            "model_id": str(model.model_id),
            "provider": str(runtime.provider or model.provider or "vision_gateway"),
            "estimated_credits": max(0, int(prepared["estimated_credits"])),
            "route_status": "configured",
            "reason": _reverse_retry_reason(operation, source_type),
        })
    return candidates


def _failure_suggestion(
    *,
    kind: str,
    status: str,
    error_type: str | None,
    has_compatible_models: bool,
    reconciliation_required: bool = False,
) -> dict[str, str] | None:
    if reconciliation_required:
        return {
            "error_type": error_type or "provider_error",
            "title": "任务状态待核对",
            "message": "上游可能已接收请求，平台尚未确认最终状态。请先查看详情，避免重复扣费或生成。",
            "recommended_action": "view",
        }
    if status not in {"failed", "needs_review"}:
        return None
    resolved_type = error_type or "system_error"
    if resolved_type == "moderation":
        return {
            "error_type": resolved_type,
            "title": "内容需要审核",
            "message": "请查看审核原因和结果状态，未确认前不要重复提交。",
            "recommended_action": "review" if status == "needs_review" else "view",
        }
    if resolved_type == "user_input":
        return {
            "error_type": resolved_type,
            "title": "检查素材和参数",
            "message": "当前素材、格式、余额或模型约束未通过。请先打开详情修正输入。",
            "recommended_action": "view",
        }
    if resolved_type == "provider_timeout":
        title = "上游模型响应超时"
        message = "可以按当前价格新建重试任务"
    elif resolved_type == "provider_error":
        title = "上游模型执行失败"
        message = "可以重新报价后新建任务"
    else:
        title = "任务执行失败"
        message = "请查看错误详情，再以新价格和新任务重试"
    if has_compatible_models:
        message += "；也可切换已验证兼容的模型"
        recommended_action = "retry_compatible_model"
    else:
        recommended_action = "retry_requote" if kind == "generation" else "retry"
    return {
        "error_type": resolved_type,
        "title": title,
        "message": f"{message}。",
        "recommended_action": recommended_action,
    }


def workflow_output_asset_refs(value: Any) -> list[str]:
    """Collect ordered, unique output asset refs from a workflow result."""
    refs: list[str] = []
    seen: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            ref = item.get("asset_ref")
            if isinstance(ref, str):
                normalized = ref.strip()
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    refs.append(normalized)
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return refs


def workflow_node_progress(nodes: list[ToolNodeRun]) -> int:
    """Project progress from durable node completion, not elapsed time."""
    if not nodes:
        return 0
    completed = sum(node.status in {"succeeded", "failed", "canceled"} for node in nodes)
    return min(100, max(0, round((completed * 100) / len(nodes))))


def workflow_external_task_ids(nodes: list[ToolNodeRun]) -> tuple[set[int], set[int]]:
    generation_ids: set[int] = set()
    reverse_ids: set[int] = set()
    targets = {
        "generation_task": generation_ids,
        "reverse_operation": reverse_ids,
    }
    for node in nodes:
        target = targets.get(str(node.external_kind or ""))
        if target is None:
            continue
        try:
            external_id = int(node.external_id or 0)
        except (TypeError, ValueError):
            continue
        if external_id > 0:
            target.add(external_id)
    return generation_ids, reverse_ids


def _workflow_node_payload(node: ToolNodeRun) -> dict[str, Any]:
    return {
        "key": node.node_key,
        "type": node.node_type,
        "status": node.status,
        "attempt_count": max(0, int(node.attempt_count or 0)),
        "max_attempts": max(1, int(node.max_attempts or 1)),
        "compensation_status": node.compensation_status,
    }


def _workflow_action_state(
    run: WorkflowRun,
    nodes: list[ToolNodeRun],
    *,
    has_results: bool,
    allow_retry: bool,
) -> tuple[list[str], str | None, str | None]:
    review_node = next(
        (
            node
            for node in nodes
            if node.node_type == "manual_review" and node.status == "waiting_review"
        ),
        None,
    )
    failed_node = next((node for node in nodes if node.status == "failed"), None)
    retry_node = None
    if (
        run.status == "failed"
        and allow_retry
        and failed_node is not None
        and int(failed_node.attempt_count or 0) < int(failed_node.max_attempts or 1)
        and all(node.compensation_status == "none" for node in nodes)
    ):
        retry_node = failed_node

    actions = ["view"]
    if run.status not in {"succeeded", "failed", "canceled"}:
        actions.append("cancel")
    if run.status in {"queued", "running", "compensating"}:
        actions.append("resume")
    elif run.status == "waiting_review" and review_node is not None:
        actions.append("review")
    elif retry_node is not None:
        actions.append("retry")
    if run.status == "succeeded" and has_results:
        actions.append("download")
    return (
        list(dict.fromkeys(actions)),
        review_node.node_key if review_node is not None else None,
        failed_node.node_key if failed_node is not None else None,
    )


def _generation_cancel_unavailable_reason(task: GenTask) -> str | None:
    """未终态但真实不可取消时，返回给前端展示的中文说明；可取消则返回 None。

    视频任务一旦提交到外部网关（存在 external_task_id），网关只支持轮询查询、
    不支持中途撤销，此时展示取消按钮只会让用户撞上 409。这里按真实可取消性
    过滤动作，并给出诚实的解释文案。
    """
    if (
        task.status in {"queued", "running"}
        and task.category == "video"
        and task.external_task_id
    ):
        return (
            "视频任务已提交到外部网关，网关不支持中途取消；"
            "请等待任务完成，若长时间无结果，系统会在超时后自动结束并退回冻结积分。"
        )
    return None


def _generation_actions(
    task: GenTask,
    has_results: bool,
    dispatch: GenerationDispatch | None,
    *,
    has_compatible_models: bool = False,
) -> list[str]:
    actions = ["view"]
    if task.status in {"queued", "running"}:
        if _generation_cancel_unavailable_reason(task) is None:
            actions.append("cancel")
    elif task.status == "failed":
        params = task.params if isinstance(task.params, dict) else {}
        unknown_submit = bool(
            params.get("_video_submit_state_unknown") or params.get("_image_submit_state_unknown")
        )
        unreconciled_dispatch = bool(
            dispatch and dispatch.status in {"pending", "publishing", "unknown", "needs_review"}
        )
        if not unknown_submit and not unreconciled_dispatch:
            actions.append("retry_requote")
            if has_compatible_models:
                actions.append("retry_compatible_model")
    elif task.status == "needs_review":
        actions.append("review")
    elif task.status == "succeeded":
        actions.append("create_similar")
        if has_results:
            actions.append("download")
    return actions


def _reverse_actions(
    operation: ReverseOperation,
    *,
    has_compatible_models: bool = False,
) -> list[str]:
    actions = ["view"]
    if operation.status in {"queued", "running"}:
        actions.append("cancel")
    elif operation.status == "needs_confirmation":
        actions.extend(("confirm", "cancel"))
    elif operation.status in {"failed", "canceled"}:
        actions.append("retry")
        if has_compatible_models:
            actions.append("retry_compatible_model")
    elif operation.status == "succeeded":
        actions.extend(("apply", "save_recipe"))
    return list(dict.fromkeys(actions))


def _generation_item(
    task: GenTask,
    result_refs: list[str],
    dispatch: GenerationDispatch | None = None,
    retry_models: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    params = task.params if isinstance(task.params, dict) else {}
    snapshot = (
        params.get("_model_snapshot") if isinstance(params.get("_model_snapshot"), dict) else {}
    )
    prompt = task.prompt if isinstance(task.prompt, dict) else {}
    summary = _truncate(
        prompt.get("final_text")
        or prompt.get("instruction")
        or prompt.get("optimized_text")
        or prompt.get("raw_text")
    )
    terminal = task.status in TERMINAL_STATUSES
    progress = 100 if terminal else int(get_progress(task.id).get("percent") or 0)
    compatible_retry_models = list(retry_models or [])
    actions = _generation_actions(
        task,
        bool(result_refs),
        dispatch,
        has_compatible_models=bool(compatible_retry_models),
    )
    error_type = task_error_type(params, task.error, status=task.status)
    reconciliation_required = bool(
        dispatch and dispatch.status in {"pending", "publishing", "unknown", "needs_review"}
    )
    return {
        "key": f"generation:{task.id}",
        "kind": "generation",
        "id": task.id,
        "status": task.status,
        "status_group": _RAW_STATUS_GROUPS["generation"][task.status],
        "category": task.category,
        "stage": task.stage,
        "retry_of_task_id": task.retry_of_task_id,
        "phase": task.phase,
        "progress": min(100, max(0, progress)),
        "title": f"{'视频' if task.category == 'video' else '图片'}生成",
        "summary": summary,
        "model_config_id": task.model_config_id,
        "model_name": _truncate(snapshot.get("model_name"), 128),
        "model_id": _truncate(snapshot.get("model_id"), 128),
        "model_provider": _truncate(snapshot.get("provider"), 64),
        "dispatch_attempt": dispatch.attempt if dispatch is not None else None,
        "dispatch_status": dispatch.status if dispatch is not None else None,
        "dispatch_task_id": dispatch.celery_task_id if dispatch is not None else None,
        "dispatch_publish_attempts": int(dispatch.publish_attempts or 0) if dispatch else 0,
        "dispatch_reconciliation_required": reconciliation_required,
        "cost_frozen": max(0, int(task.cost_frozen or 0)),
        "cost_settled": max(0, int(task.cost_settled or 0)),
        "result_refs": result_refs,
        "available_actions": actions,
        "cancel_unavailable_reason": _generation_cancel_unavailable_reason(task),
        "error_type": error_type,
        "error_message": task.error,
        "failure_suggestion": _failure_suggestion(
            kind="generation",
            status=task.status,
            error_type=error_type,
            has_compatible_models=bool(compatible_retry_models),
            reconciliation_required=reconciliation_required,
        ),
        "compatible_retry_models": compatible_retry_models,
        "created_at": task.created_at,
        "updated_at": task.finished_at,
        "finished_at": task.finished_at,
    }


def _reverse_item(
    operation: ReverseOperation,
    retry_models: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    snapshot = operation.model_snapshot if isinstance(operation.model_snapshot, dict) else {}
    terminal = operation.status in {"succeeded", "failed", "canceled"}
    progress = max(int(operation.progress or 0), 100 if terminal else 0)
    context = operation.request_context if isinstance(operation.request_context, dict) else {}
    compatible_retry_models = list(retry_models or [])
    actions = _reverse_actions(
        operation,
        has_compatible_models=bool(compatible_retry_models),
    )
    error_type = (
        task_error_type(context, operation.error, status=operation.status)
        if operation.error
        else None
    )
    return {
        "key": f"reverse:{operation.id}",
        "kind": "reverse",
        "id": operation.id,
        "status": operation.status,
        "status_group": _RAW_STATUS_GROUPS["reverse"][operation.status],
        "category": operation.target,
        "stage": operation.output_purpose,
        "phase": operation.phase,
        "progress": min(100, max(0, progress)),
        "title": f"{'视频' if operation.target == 'video' else '图片'}反推",
        "summary": _truncate(operation.analysis_focus, 128),
        "model_config_id": operation.model_config_id,
        "model_name": _truncate(snapshot.get("model_name"), 128),
        "model_id": _truncate(snapshot.get("model_id"), 128),
        "model_provider": _truncate(snapshot.get("provider"), 64),
        "cost_frozen": max(0, int(operation.cost_frozen or 0)),
        "cost_settled": max(0, int(operation.cost_settled or 0)),
        "result_refs": [],
        "available_actions": actions,
        "error_type": error_type,
        "error_message": operation.error,
        "failure_suggestion": _failure_suggestion(
            kind="reverse",
            status=operation.status,
            error_type=error_type,
            has_compatible_models=bool(compatible_retry_models),
        ),
        "compatible_retry_models": compatible_retry_models,
        "created_at": operation.created_at,
        "updated_at": operation.updated_at,
        "finished_at": operation.finished_at,
    }


def _parse_item(record: ParseRecord) -> dict[str, Any]:
    progress = {"queued": 5, "running": 50, "done": 100, "failed": 100}.get(record.status, 0)
    return {
        "key": f"parse:{record.id}",
        "kind": "parse",
        "id": record.id,
        "status": record.status,
        "status_group": _RAW_STATUS_GROUPS["parse"][record.status],
        "category": "parse",
        "stage": None,
        "phase": None,
        "progress": progress,
        "title": "链接解析",
        "summary": _truncate(record.url),
        "model_config_id": None,
        "model_name": None,
        "model_id": None,
        "model_provider": None,
        "cost_frozen": 0,
        "cost_settled": 0,
        "result_refs": [],
        "available_actions": ["view", "use_assets"] if record.status == "done" else ["view"],
        "error_type": classify_error_type(record.error, status=record.status)
        if record.error
        else None,
        "error_message": record.error,
        "created_at": record.created_at,
        "updated_at": None,
        "finished_at": None,
    }


def _workflow_item(
    run: WorkflowRun,
    *,
    tool_run: ToolRun | None,
    tool: ToolDefinition | None,
    nodes: list[ToolNodeRun],
    cost_frozen: int,
    cost_settled: int,
) -> dict[str, Any]:
    output = tool_run.output if tool_run is not None else None
    result_refs = workflow_output_asset_refs(output)
    actions, review_node_key, failed_node_key = _workflow_action_state(
        run,
        nodes,
        has_results=bool(result_refs),
        allow_retry=not bool(tool_run and int(tool_run.cost_frozen or 0) > 0),
    )
    tool_input = tool_run.input_snapshot if tool_run and isinstance(tool_run.input_snapshot, dict) else {}
    composition = tool_input.get("composition") if isinstance(tool_input.get("composition"), dict) else {}
    summary = _truncate(composition.get("title") or (tool.description if tool else None), 160)
    return {
        "key": f"workflow:{run.id}",
        "kind": "workflow",
        "id": run.id,
        "status": run.status,
        "status_group": _RAW_STATUS_GROUPS["workflow"][run.status],
        "category": tool.category if tool is not None else "workflow",
        "stage": tool.slug if tool is not None else None,
        "phase": run.current_node_key,
        "progress": workflow_node_progress(nodes),
        "title": tool.name if tool is not None else "工作流任务",
        "summary": summary,
        "model_config_id": None,
        "model_name": None,
        "model_id": None,
        "model_provider": None,
        "cost_frozen": max(0, int(cost_frozen)),
        "cost_settled": max(0, int(cost_settled)),
        "result_refs": result_refs,
        "available_actions": actions,
        "error_type": classify_error_type(run.error, status=run.status) if run.error else None,
        "error_message": run.error,
        "workflow_tool_slug": tool.slug if tool is not None else None,
        "workflow_entry_path": tool.entry_path if tool is not None else None,
        "workflow_current_node_key": review_node_key or run.current_node_key,
        "workflow_failed_node_key": failed_node_key,
        "workflow_nodes": [_workflow_node_payload(node) for node in nodes],
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "finished_at": run.finished_at,
    }


def _grouped_counts(
    db: Session,
    *,
    kinds: list[tuple[str, Any]],
    user_id: int,
    category: str,
) -> dict[str, int]:
    counts = {key: 0 for key in _STATUS_GROUPS}
    for kind, model in kinds:
        category_clause = _category_filter(kind, model, category)
        if category_clause is False:
            continue
        stmt = select(model.status, func.count()).where(model.user_id == user_id)
        if category_clause is not None:
            stmt = stmt.where(category_clause)
        for raw_status, count in db.execute(stmt.group_by(model.status)):
            group = _RAW_STATUS_GROUPS[kind].get(str(raw_status))
            if group:
                counts[group] += int(count or 0)
    counts["all"] = sum(counts.values())
    return counts


def list_unified_tasks(
    db: Session,
    *,
    user_id: int,
    kind: str = "all",
    status_group: str = "all",
    category: str = "all",
    limit: int = 30,
    cursor: str | None = None,
) -> dict[str, Any]:
    cursor_key = _decode_cursor(cursor)
    all_kinds: list[tuple[str, Any]] = [
        ("workflow", WorkflowRun),
        ("generation", GenTask),
        ("reverse", ReverseOperation),
        ("parse", ParseRecord),
    ]
    selected_kinds = all_kinds if kind == "all" else [item for item in all_kinds if item[0] == kind]
    raw_items: list[tuple[str, Any]] = []
    for selected_kind, model in selected_kinds:
        rows = _load_kind_rows(
            db,
            kind=selected_kind,
            model=model,
            user_id=user_id,
            status_group=status_group,
            category=category,
            cursor_key=cursor_key,
            limit=limit,
        )
        raw_items.extend((selected_kind, row) for row in rows)
    raw_items.sort(key=lambda item: _row_sort_key(*item), reverse=True)
    if cursor_key is not None:
        raw_items = [item for item in raw_items if _row_sort_key(*item) < cursor_key]
    selected = raw_items[: limit + 1]
    has_more = len(selected) > limit
    selected = selected[:limit]

    generation_ids = [row.id for selected_kind, row in selected if selected_kind == "generation"]
    workflow_ids = [row.id for selected_kind, row in selected if selected_kind == "workflow"]
    generation_result_refs: dict[int, list[str]] = defaultdict(list)
    dispatches: dict[int, GenerationDispatch] = {}
    if generation_ids:
        for task_id, asset_id in db.execute(
            select(GenAsset.task_id, GenAsset.id)
            .where(GenAsset.task_id.in_(generation_ids))
            .order_by(GenAsset.id)
        ):
            generation_result_refs[int(task_id)].append(generated_asset_ref(int(asset_id)))
        for dispatch in db.scalars(
            select(GenerationDispatch)
            .where(GenerationDispatch.task_id.in_(generation_ids))
            .order_by(GenerationDispatch.task_id, GenerationDispatch.attempt.desc())
        ):
            dispatches.setdefault(int(dispatch.task_id), dispatch)

    workflow_tool_runs: dict[int, ToolRun] = {}
    workflow_tools: dict[int, ToolDefinition] = {}
    workflow_nodes: dict[int, list[ToolNodeRun]] = defaultdict(list)
    workflow_costs: dict[int, tuple[int, int]] = {}
    if workflow_ids:
        for run_id, tool_run, tool in db.execute(
            select(WorkflowRun.id, ToolRun, ToolDefinition)
            .join(ToolRun, ToolRun.id == WorkflowRun.tool_run_id)
            .join(ToolDefinition, ToolDefinition.id == ToolRun.tool_definition_id)
            .where(WorkflowRun.id.in_(workflow_ids))
        ):
            workflow_tool_runs[int(run_id)] = tool_run
            workflow_tools[int(run_id)] = tool
        for node in db.scalars(
            select(ToolNodeRun)
            .where(ToolNodeRun.workflow_run_id.in_(workflow_ids))
            .order_by(ToolNodeRun.workflow_run_id, ToolNodeRun.topological_index, ToolNodeRun.id)
        ):
            workflow_nodes[int(node.workflow_run_id)].append(node)

        generation_ids_by_run: dict[int, set[int]] = {}
        reverse_ids_by_run: dict[int, set[int]] = {}
        output_asset_ids_by_run: dict[int, set[int]] = defaultdict(set)
        for run_id in workflow_ids:
            nodes = workflow_nodes.get(int(run_id), [])
            child_generation_ids, child_reverse_ids = workflow_external_task_ids(nodes)
            generation_ids_by_run[int(run_id)] = child_generation_ids
            reverse_ids_by_run[int(run_id)] = child_reverse_ids
            tool_run = workflow_tool_runs.get(int(run_id))
            for ref in workflow_output_asset_refs(tool_run.output if tool_run is not None else None):
                for prefix in ("g.", "generated:"):
                    if not ref.startswith(prefix):
                        continue
                    try:
                        asset_id = int(ref[len(prefix) :])
                    except ValueError:
                        break
                    if asset_id > 0:
                        output_asset_ids_by_run[int(run_id)].add(asset_id)
                    break

        all_output_asset_ids = set().union(*output_asset_ids_by_run.values())
        output_task_by_asset: dict[int, int] = {}
        if all_output_asset_ids:
            output_task_by_asset = {
                int(asset_id): int(task_id)
                for asset_id, task_id in db.execute(
                    select(GenAsset.id, GenAsset.task_id).where(
                        GenAsset.user_id == user_id,
                        GenAsset.id.in_(all_output_asset_ids),
                    )
                )
            }
        for run_id, asset_ids in output_asset_ids_by_run.items():
            generation_ids_by_run[run_id].update(
                output_task_by_asset[asset_id]
                for asset_id in asset_ids
                if asset_id in output_task_by_asset
            )

        all_child_generation_ids = set().union(*generation_ids_by_run.values())
        all_child_reverse_ids = set().union(*reverse_ids_by_run.values())
        generation_costs = {
            int(task.id): (
                max(0, int(task.cost_frozen or 0)),
                max(0, int(task.cost_settled or 0)),
            )
            for task in db.scalars(
                select(GenTask).where(
                    GenTask.user_id == user_id,
                    GenTask.id.in_(all_child_generation_ids),
                )
            )
        } if all_child_generation_ids else {}
        reverse_costs = {
            int(task.id): (
                max(0, int(task.cost_frozen or 0)),
                max(0, int(task.cost_settled or 0)),
            )
            for task in db.scalars(
                select(ReverseOperation).where(
                    ReverseOperation.user_id == user_id,
                    ReverseOperation.id.in_(all_child_reverse_ids),
                )
            )
        } if all_child_reverse_ids else {}
        for run_id in workflow_ids:
            tool_run = workflow_tool_runs.get(int(run_id))
            costs: list[tuple[int, int]] = []
            if tool_run is not None:
                costs.append((
                    max(0, int(tool_run.cost_frozen or 0)),
                    max(0, int(tool_run.cost_settled or 0)),
                ))
            costs.extend(
                generation_costs[task_id]
                for task_id in generation_ids_by_run[int(run_id)]
                if task_id in generation_costs
            )
            costs.extend(
                reverse_costs[task_id]
                for task_id in reverse_ids_by_run[int(run_id)]
                if task_id in reverse_costs
            )
            workflow_costs[int(run_id)] = (
                sum(cost[0] for cost in costs),
                sum(cost[1] for cost in costs),
            )

    project_ids: dict[tuple[str, int], list[int]] = defaultdict(list)
    project_conditions = [
        (MediaProjectTask.task_kind == selected_kind) & (MediaProjectTask.task_id == row.id)
        for selected_kind, row in selected
    ]
    if project_conditions:
        for project_id, task_kind, task_id in db.execute(
            select(
                MediaProjectTask.project_id,
                MediaProjectTask.task_kind,
                MediaProjectTask.task_id,
            ).where(or_(*project_conditions))
        ):
            project_ids[(str(task_kind), int(task_id))].append(int(project_id))

    candidate_uses: set[str] = set()
    if any(selected_kind == "generation" and row.status == "failed" for selected_kind, row in selected):
        candidate_uses.update(("image", "video"))
    if any(
        selected_kind == "reverse" and row.status in {"failed", "canceled"}
        for selected_kind, row in selected
    ):
        candidate_uses.add("vision")
    candidate_models = list(
        db.scalars(
            select(ModelConfig)
            .where(
                ModelConfig.enabled.is_(True),
                ModelConfig.use.in_(candidate_uses),
            )
            .order_by(ModelConfig.use, ModelConfig.sort_order, ModelConfig.id)
        )
    ) if candidate_uses else []
    generation_models = [model for model in candidate_models if model.use in {"image", "video"}]
    vision_models = [model for model in candidate_models if model.use == "vision"]
    generation_route_summaries: dict[int, dict[str, Any]] = {}
    vision_runtime_cache: dict[int, Any | None] = {}

    items: list[dict[str, Any]] = []
    for selected_kind, row in selected:
        if selected_kind == "generation":
            dispatch = dispatches.get(int(row.id))
            retry_models: list[dict[str, Any]] = []
            if "retry_requote" in _generation_actions(
                row,
                bool(generation_result_refs.get(int(row.id), [])),
                dispatch,
            ):
                retry_models = _generation_retry_models(
                    db,
                    row,
                    models=generation_models,
                    route_summaries=generation_route_summaries,
                )
            item = _generation_item(
                row,
                generation_result_refs.get(int(row.id), []),
                dispatch,
                retry_models,
            )
        elif selected_kind == "reverse":
            retry_models = (
                _reverse_retry_models(
                    db,
                    row,
                    models=vision_models,
                    runtime_cache=vision_runtime_cache,
                )
                if row.status in {"failed", "canceled"}
                else []
            )
            item = _reverse_item(row, retry_models)
        elif selected_kind == "workflow":
            cost_frozen, cost_settled = workflow_costs.get(int(row.id), (0, 0))
            item = _workflow_item(
                row,
                tool_run=workflow_tool_runs.get(int(row.id)),
                tool=workflow_tools.get(int(row.id)),
                nodes=workflow_nodes.get(int(row.id), []),
                cost_frozen=cost_frozen,
                cost_settled=cost_settled,
            )
        else:
            item = _parse_item(row)
        item["project_ids"] = sorted(project_ids.get((selected_kind, int(row.id)), []))
        items.append(item)

    counts = _grouped_counts(
        db,
        kinds=selected_kinds,
        user_id=user_id,
        category=category,
    )
    return {
        "items": items,
        "next_cursor": _encode_cursor(*selected[-1]) if has_more and selected else None,
        "has_more": has_more,
        "total": counts.get(status_group, 0) if status_group != "all" else counts["all"],
        "counts": counts,
    }
