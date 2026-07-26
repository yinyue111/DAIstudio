"""Submit generation tasks. Freezes the estimated cost, enqueues the worker,
and refunds immediately if enqueue fails."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import get_client_ip, get_current_user
from ..models import GenAsset, GenerationQuote, GenTask, User
from ..prompt_optimization_schemas import StudioPromptOptimizationIn
from ..schemas import (
    ExecutionQuoteIn,
    GenerateIn,
    GenerationQuoteIn,
    GenerationQuoteOut,
    GenerationRequestBase,
    RetryRequoteIn,
    ReverseBatchCreate,
    ReverseOperationCreate,
    TaskOut,
)
from ..services import (
    generation,
    generation_retry,
    locks,
    project_collection,
    recipe_usage,
    reverse_operations,
)
from ..services.config_store import ModelConfigResolutionError, resolve_model_config
from ..services.gateway_config_errors import raise_gateway_config_http
from ..services.generation import (
    model_snapshot,
)
from ..services.generation_dispatch import validate_dispatch_request
from ..services.generation_image_evidence import (
    ReviewedEvidenceMaskError,
    resolve_reviewed_evidence_plan_for_lineage,
)
from ..services.generation_media import video_render_duration
from ..services.generation_model_runtime import model_from_persisted_snapshot
from ..services.generation_policy import (
    assert_generation_request_policy,
    assert_retry_submission_state_known,
    validate_prompt_payload,
)
from ..services.generation_prompts import compact_image_prompt_payload
from ..services.generation_quotes import (
    create_asset_unlock_quote,
    create_generation_quote,
    create_prompt_optimization_quote,
    create_reverse_batch_quote,
    create_reverse_quote,
    create_workflow_quote,
    find_idempotent_quote,
    generation_quote_out,
    lock_generation_quote,
    quoted_model_snapshot,
    validate_generation_quote,
    validate_quote_snapshot_integrity,
)
from ..services.generation_request import (
    assert_client_request_replay,
    default_image_n,
    estimate_generation_cost_from_snapshot,
    validate_generation_params,
    validate_product_detail_images,
)
from ..services.generation_request import (
    request_fingerprint as build_request_fingerprint,
)
from ..services.generation_submit import submit_generation_task, validate_generation_lineage
from ..services.generation_video_submit import (
    lineage_video_analysis_for_compile,
    merge_lineage_video_analysis,
)
from ..services.model_routes import (
    ModelRouteUnavailable,
    attach_route_snapshot,
    select_model_route,
)
from ..services.product_edition import feature_enabled
from ..services.rate_limit import incr_window
from ..services.task_output import build_task_out
from ..services.video_prompt_compiler import (
    build_video_prompt_references,
    compile_video_prompt,
    store_video_prompt_compile,
)
from ..workflow_schemas import ToolRunCreateIn

router = APIRouter(prefix="/api", tags=["generate"])


_SOURCE_META_URL_KEYS = {
    "original_url",
    "original_thumb",
    "source_page_url",
    "selected_url",
    "selected_thumb",
}
_SOURCE_META_TEXT_KEYS = {
    "source_captured_at",
    "selected_type",
    "mode",
    "product_generation_mode",
    "portrait_generation_mode",
    "subject_mode",
    "subject_profile_source",
    "subject_profile_summary",
    "variation_of_asset_id",
}
_SOURCE_META_BOOL_KEYS = {"product_generation_mode", "portrait_generation_mode"}
_SOURCE_META_INT_KEYS = {"variation_of_asset_id"}


def _final_params_from_parent(parent_params: dict | None) -> dict:
    """Build a clean final-render params payload from preview params.

    Final renders inherit user-facing render controls from the preview, but
    runtime bookkeeping such as provider request ids, result URLs, download
    attempts, and reconciliation flags must belong to the final task itself.
    """
    return {
        str(k): v
        for k, v in dict(parent_params or {}).items()
        if not str(k).startswith("_video_") and str(k) != "request_id"
    }


def _clean_source_meta_url(value) -> str | None:
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url or len(url) > 2048:
        return None
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return url


def _clean_source_meta_text(value, *, max_len: int = 128) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:max_len]


def _source_trace(
    *,
    source_asset_url: str | None,
    source_type: str | None,
    source_asset_ref: str | None,
    meta: dict | None,
) -> dict:
    """Store bounded provenance metadata for compliance/audit only.

    The trace is written under ``params._source_trace`` and ignored by request
    fingerprinting, so it cannot alter generation idempotency.
    """
    source_meta = meta if isinstance(meta, dict) else {}
    try:
        raw_size = len(json.dumps(source_meta, ensure_ascii=False))
    except (TypeError, ValueError):
        source_meta = {}
        raw_size = 0
    if raw_size > 8192:
        source_meta = {}

    trace: dict = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "source_asset_url": _clean_source_meta_url(source_asset_url),
        "source_type": source_type if source_type in {"image", "video"} else None,
        "asset_ref": source_asset_ref,
    }
    for key in _SOURCE_META_URL_KEYS:
        cleaned = _clean_source_meta_url(source_meta.get(key))
        if cleaned:
            trace[key] = cleaned
    for key in _SOURCE_META_TEXT_KEYS:
        if key in _SOURCE_META_BOOL_KEYS and isinstance(source_meta.get(key), bool):
            trace[key] = source_meta[key]
            continue
        if key in _SOURCE_META_INT_KEYS:
            try:
                value = int(source_meta.get(key))
            except (TypeError, ValueError):
                continue
            if value > 0:
                trace[key] = value
            continue
        if key == "subject_profile_summary":
            cleaned = _clean_source_meta_text(source_meta.get(key), max_len=1200)
            if cleaned:
                trace[key] = cleaned
            continue
        cleaned = _clean_source_meta_text(source_meta.get(key))
        if cleaned:
            trace[key] = cleaned
    return {k: v for k, v in trace.items() if v is not None}


def _rate_limit(user_id: int) -> None:
    key = f"gen:rate:{user_id}"
    n = incr_window(key, 3600)
    if n > settings.user_gen_rate_per_hour:
        raise HTTPException(429, "生成过于频繁,请稍后再试")


def _normalize_client_request_id(value: str | None) -> str | None:
    if value is None:
        return None
    request_id = value.strip()
    if not request_id:
        return None
    if len(request_id) < 8 or len(request_id) > 128:
        raise HTTPException(400, "client_request_id 长度需在 8..128 之间")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", request_id):
        raise HTTPException(400, "client_request_id 只能包含字母、数字、点、下划线、冒号和短横线")
    return request_id


def _existing_client_request_task(
    db: Session,
    user_id: int,
    client_request_id: str | None,
) -> GenTask | None:
    if not client_request_id:
        return None
    return db.execute(
        select(GenTask)
        .where(
            GenTask.user_id == user_id,
            GenTask.client_request_id == client_request_id,
        )
        .limit(1)
    ).scalar_one_or_none()


def _existing_active_final(db: Session, user_id: int, parent_id: int) -> GenTask | None:
    active = db.execute(
        select(GenTask)
        .where(
            GenTask.user_id == user_id,
            GenTask.category == "video",
            GenTask.stage == "final",
            GenTask.parent_task_id == parent_id,
            GenTask.status.in_(("queued", "running", generation.NEEDS_REVIEW)),
        )
        .order_by(GenTask.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if active is not None:
        return active
    return db.execute(
        select(GenTask)
        .join(GenAsset, GenAsset.task_id == GenTask.id)
        .where(
            GenTask.user_id == user_id,
            GenTask.category == "video",
            GenTask.stage == "final",
            GenTask.parent_task_id == parent_id,
            GenTask.status == "succeeded",
        )
        .order_by(GenTask.id.desc())
        .limit(1)
    ).scalar_one_or_none()


@dataclass
class PreparedGeneration:
    client_request_id: str | None
    existing_client_task: GenTask | None
    parent: GenTask | None
    prompt: dict
    source_asset_url: str | None
    source_type: str | None
    task_params: dict
    model: object
    model_use: str
    snapshot: dict
    n_images: int
    request_fingerprint: str
    cost: int
    request_snapshot: dict
    project_id: int | None
    recipe_attribution: recipe_usage.RecipeAttribution | None
    reverse_operation_id: int | None
    reverse_revision_id: int | None


def _resolved_reverse_lineage(
    body: GenerationRequestBase,
    *,
    parent: GenTask | None,
    existing_client_task: GenTask | None,
) -> tuple[int | None, int | None]:
    operation_id = body.reverse_operation_id
    revision_id = body.reverse_revision_id

    inherited_task = parent or existing_client_task
    if inherited_task is None:
        return operation_id, revision_id
    inherited_operation_id = inherited_task.reverse_operation_id
    inherited_revision_id = inherited_task.source_revision_id
    inherited = inherited_operation_id is not None and inherited_revision_id is not None
    requested = operation_id is not None and revision_id is not None

    if parent is not None and requested and not inherited:
        raise HTTPException(409, "视频最终生成不能新增预览任务未绑定的反推血缘")
    if (
        inherited
        and requested
        and (
            int(operation_id) != int(inherited_operation_id)
            or int(revision_id) != int(inherited_revision_id)
        )
    ):
        raise HTTPException(409, "生成请求的反推血缘与已有任务不一致")
    if inherited and not requested:
        return int(inherited_operation_id), int(inherited_revision_id)
    return operation_id, revision_id


def _bind_reverse_lineage_fingerprint(
    fingerprint: str,
    *,
    reverse_operation_id: int | None,
    reverse_revision_id: int | None,
) -> str:
    if reverse_operation_id is None or reverse_revision_id is None:
        return fingerprint
    raw = f"{fingerprint}:reverse:{int(reverse_operation_id)}:{int(reverse_revision_id)}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _matches_reverse_video_source(operation, source_asset_url: str | None) -> bool:
    """Bind an analysis-only source video to the reverse task that consumed it."""
    if operation is None or operation.target != "video" or not source_asset_url:
        return False
    candidates = {str(operation.asset_url or "").strip()}
    context = operation.request_context if isinstance(operation.request_context, dict) else {}
    candidates.add(str(context.get("asset_url") or "").strip())
    sources = context.get("sources")
    if isinstance(sources, list):
        candidates.update(
            str(item.get("asset_url") or "").strip()
            for item in sources
            if isinstance(item, dict) and item.get("source_type") == "video"
        )
    return source_asset_url.strip() in candidates


def _task_recipe_attribution(task: GenTask | None) -> recipe_usage.RecipeAttribution | None:
    if task is None:
        return None
    params = task.params if isinstance(task.params, dict) else {}
    if "_recipe_attribution" not in params:
        return None
    attribution = recipe_usage.attribution_from_snapshot(params.get("_recipe_attribution"))
    if attribution is None:
        raise HTTPException(409, "已有生成任务的创作配方归因不完整")
    return attribution


def _resolved_recipe_attribution(
    body: GenerationRequestBase,
    *,
    db: Session,
    user: User,
    parent: GenTask | None,
    existing_client_task: GenTask | None,
    trusted_attribution: recipe_usage.RecipeAttribution | None = None,
) -> recipe_usage.RecipeAttribution | None:
    inherited = trusted_attribution or _task_recipe_attribution(parent or existing_client_task)
    requested = body.creation_recipe_id is not None
    resolved: recipe_usage.RecipeAttribution | None = None
    if requested:
        resolved, _ = recipe_usage.resolve_recipe_attribution(
            db,
            user_id=int(user.id),
            recipe_id=int(body.creation_recipe_id),
            version=int(body.creation_recipe_version),
            share_slug=body.creation_recipe_share_slug,
            expected_category=body.category,
        )
    if parent is not None and requested and inherited is None:
        raise HTTPException(409, "视频最终生成不能新增预览任务未绑定的创作配方归因")
    if inherited is not None and resolved is not None and inherited != resolved:
        raise HTTPException(409, "生成请求的创作配方归因与已有任务不一致")
    return inherited or resolved


def _bind_recipe_attribution_fingerprint(
    fingerprint: str,
    attribution: recipe_usage.RecipeAttribution | None,
) -> str:
    if attribution is None:
        return fingerprint
    raw = (
        f"{fingerprint}:recipe:{attribution.recipe_id}:{attribution.recipe_version}:"
        f"{attribution.source}:{attribution.share_id or 0}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _bind_reproduction_remediation_fingerprint(
    fingerprint: str,
    context: dict | None,
) -> str:
    if not context:
        return fingerprint
    raw = (
        f"{fingerprint}:reproduction-remediation:"
        f"{int(context['remediation_id'])}:{context['plan_item_id']}:"
        f"{context['plan_hash']}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _prepare_generation(
    body: GenerationRequestBase,
    *,
    db: Session,
    user: User,
    quote: GenerationQuote | None = None,
    fresh_route: bool = False,
    avoid_route_ids: set[int] | None = None,
    trusted_recipe_attribution: recipe_usage.RecipeAttribution | None = None,
) -> PreparedGeneration:
    if (
        (body.project_id is not None and not feature_enabled("projects_enabled"))
        or (
            body.creation_recipe_id is not None
            and not feature_enabled("recipes_enabled")
        )
        or (
            body.reproduction_remediation_id is not None
            and not feature_enabled("reproduction_assessment_enabled")
        )
    ):
        raise HTTPException(404, "not found")
    validate_dispatch_request(body.category)
    client_request_id = _normalize_client_request_id(body.client_request_id)
    existing_client_task = _existing_client_request_task(db, user.id, client_request_id)

    params = validate_generation_params(body.category, body.params)
    reproduction_context: dict | None = None
    if body.reproduction_remediation_id is not None:
        from ..services import reproduction_remediation

        request_payload = body.model_dump(
            mode="json",
            exclude_none=True,
            exclude={"quote_id"},
        )
        try:
            reproduction_context = reproduction_remediation.validate_generation_request(
                db,
                user_id=int(user.id),
                remediation_id=int(body.reproduction_remediation_id),
                plan_item_id=str(body.reproduction_plan_item_id),
                request_payload=request_payload,
            )
        except reproduction_remediation.ReproductionRemediationError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
    shot_context = body.shot_context.model_dump(mode="json") if body.shot_context else None
    request_prompt = dict(body.prompt or {})
    if body.instruction:
        request_prompt["instruction"] = body.instruction
    request_instruction = request_prompt.get("instruction")
    validation_prompt = (
        compact_image_prompt_payload(request_prompt, params)
        if body.category == "image"
        else request_prompt
    )
    validation_instruction = validation_prompt.get("instruction")
    validate_prompt_payload(
        validation_prompt,
        str(validation_instruction)
        if validation_instruction is not None
        else (str(request_instruction) if request_instruction is not None else None),
    )

    # Video now submits full renders directly. Legacy final-from-preview is kept
    # for old history entries and idempotent clients that still pass a parent.
    parent = None
    if body.stage == "final":
        if body.category != "video":
            raise HTTPException(400, "final 阶段仅支持视频高清渲染")
        if body.parent_task_id:
            parent = db.get(GenTask, body.parent_task_id)
            if not parent or parent.user_id != user.id:
                raise HTTPException(404, "预览任务不存在")
            if (
                parent.category != "video"
                or parent.stage != "preview"
                or parent.status != "succeeded"
            ):
                raise HTTPException(400, "final 必须基于已成功的视频预览任务")
            has_preview_asset = db.execute(
                select(GenAsset.id).where(GenAsset.task_id == parent.id).limit(1)
            ).scalar_one_or_none()
            if has_preview_asset is None:
                raise HTTPException(400, "预览任务缺少可用素材,无法高清渲染")

    reverse_operation_id, reverse_revision_id = _resolved_reverse_lineage(
        body,
        parent=parent,
        existing_client_task=existing_client_task,
    )
    lineage_operation, lineage_revision = validate_generation_lineage(
        db,
        user_id=user.id,
        reverse_operation_id=reverse_operation_id,
        reverse_revision_id=reverse_revision_id,
    )

    project_id = body.project_id
    inherited_project_task = parent or existing_client_task
    if project_id is None and inherited_project_task is not None:
        project_id = project_collection.primary_project_id_for_task(
            db,
            user_id=user.id,
            task_kind="generation",
            task_id=int(inherited_project_task.id),
        )
    if project_id is not None:
        try:
            project_collection.require_owned_project(db, user.id, project_id)
        except project_collection.ProjectNotFound as exc:
            raise HTTPException(404, str(exc)) from exc

    recipe_attribution = _resolved_recipe_attribution(
        body,
        db=db,
        user=user,
        parent=parent,
        existing_client_task=existing_client_task,
        trusted_attribution=trusted_recipe_attribution,
    )

    # A final render is FULLY derived from its own preview — never trust the
    # client's prompt/source/params for final, or a user could pass preview A's
    # id while rendering entirely different content. Otherwise assemble from the
    # request: structured(final_text) OR plain instruction (reverse off).
    if body.stage == "final" and parent:
        prompt = dict(parent.prompt or {})
        source_asset_url = parent.source_asset_url
        source_type = parent.source_type
        task_params = _final_params_from_parent(parent.params)
    else:
        prompt = dict(request_prompt)
        if not prompt:
            raise HTTPException(400, "请提供 prompt 或 instruction")
        source_asset_url = body.source_asset_url
        source_type = body.source_type
        task_params = params
    analysis_only_source_video = bool(
        body.category == "video"
        and source_type == "video"
        and source_asset_url
        and lineage_operation is not None
    )
    if analysis_only_source_video and not _matches_reverse_video_source(
        lineage_operation,
        source_asset_url,
    ):
        raise HTTPException(409, "视频生成素材与已应用的反推源视频不一致")
    if reproduction_context is not None:
        task_params["_reproduction_context"] = dict(reproduction_context)
    if shot_context is not None:
        task_params["_shot_context"] = shot_context
    if body.category == "video":
        # Final renders inherit the preview task's params. Re-validate the
        # ordered detail-image contract instead of trusting historical rows.
        validate_product_detail_images(task_params)
    model_use = body.category  # image/video
    selected_model_config_id = body.model_config_id
    require_enabled = True
    if quote is not None:
        if selected_model_config_id is not None and int(selected_model_config_id) != int(
            quote.model_config_id
        ):
            raise HTTPException(
                409,
                detail={"code": "QUOTE_MISMATCH", "message": "生成模型已变化，请重新报价"},
            )
        selected_model_config_id = int(quote.model_config_id)
        require_enabled = False
    elif parent is not None and body.stage == "final":
        parent_model_config_id = getattr(parent, "model_config_id", None)
        if (
            selected_model_config_id is not None
            and parent_model_config_id is not None
            and int(selected_model_config_id) != int(parent_model_config_id)
        ):
            raise HTTPException(409, "视频最终生成必须使用预览任务绑定的同一模型")
        selected_model_config_id = parent_model_config_id or selected_model_config_id
        require_enabled = fresh_route
    elif existing_client_task is not None:
        existing_model_config_id = getattr(existing_client_task, "model_config_id", None)
        if (
            selected_model_config_id is not None
            and existing_model_config_id is not None
            and int(selected_model_config_id) != int(existing_model_config_id)
        ):
            raise HTTPException(409, "client_request_id 已用于不同模型配置")
        selected_model_config_id = existing_model_config_id or selected_model_config_id
        require_enabled = False
    try:
        model = resolve_model_config(
            db,
            model_use,
            selected_model_config_id,
            require_enabled=require_enabled,
        )
    except ModelConfigResolutionError as exc:
        raise HTTPException(400, str(exc)) from exc
    if quote is not None and not model.enabled:
        raise HTTPException(
            409,
            detail={
                "code": "QUOTE_GATEWAY_UNAVAILABLE",
                "message": "报价绑定的模型已停用,请重新报价",
            },
        )
    policy_model = model
    if quote is not None:
        snapshot = validate_quote_snapshot_integrity(db, quote)
        try:
            policy_model = model_from_persisted_snapshot(
                db,
                snapshot,
                model,
                model_use,
            )
        except generation.ModelSnapshotMismatchError as exc:
            raise_gateway_config_http(exc)
    elif body.stage == "final" and parent and not fresh_route:
        inherited_snapshot = dict(parent.params or {}).get("_model_snapshot") or {}
        if inherited_snapshot:
            try:
                policy_model = model_from_persisted_snapshot(
                    db,
                    inherited_snapshot,
                    model,
                    model_use,
                )
            except generation.ModelSnapshotMismatchError as e:
                raise_gateway_config_http(e)
        snapshot = inherited_snapshot or model_snapshot(model)
    elif existing_client_task is not None and not fresh_route:
        inherited_snapshot = dict(existing_client_task.params or {}).get("_model_snapshot") or {}
        if inherited_snapshot:
            try:
                policy_model = model_from_persisted_snapshot(
                    db,
                    inherited_snapshot,
                    model,
                    model_use,
                )
            except generation.ModelSnapshotMismatchError as exc:
                raise_gateway_config_http(exc)
        snapshot = inherited_snapshot or model_snapshot(model)
    else:
        try:
            route_selection = select_model_route(
                db,
                model,
                avoid_route_ids=avoid_route_ids,
            )
        except ModelRouteUnavailable as exc:
            raise HTTPException(
                503,
                detail={
                    "code": "MODEL_ROUTE_UNAVAILABLE",
                    "message": str(exc),
                },
            ) from exc
        policy_model = route_selection.runtime_model
        snapshot = attach_route_snapshot(
            model_snapshot(route_selection.runtime_model),
            route_selection,
        )

    assert_generation_request_policy(
        db,
        user_id=int(user.id),
        model=policy_model,
        category=body.category,
        source_asset_url=source_asset_url,
        source_type=source_type,
        prompt=prompt,
        params=task_params,
        require_actionable_video_reference=not (body.category == "video" and parent is not None),
        analysis_only_source_video=analysis_only_source_video,
    )
    if body.category == "image":
        edit_source_url = (
            task_params.get("reference_image_url")
            or task_params.get("character_reference_image")
            or source_asset_url
        )
        try:
            reviewed_evidence_plan = resolve_reviewed_evidence_plan_for_lineage(
                db,
                user_id=int(user.id),
                operation_id=reverse_operation_id,
                revision_id=reverse_revision_id,
                edit_source_url=edit_source_url,
            )
        except ReviewedEvidenceMaskError as exc:
            raise HTTPException(409, str(exc)) from exc
        if reviewed_evidence_plan is not None:
            snapshot_extra = (
                snapshot.get("extra") if isinstance(snapshot.get("extra"), dict) else {}
            )
            edit_path = snapshot_extra.get("edit_path", settings.image_edit_path)
            if not edit_source_url or not edit_path:
                raise HTTPException(400, "所选图片模型不支持已确认证据蒙版编辑")
            capabilities = (
                snapshot_extra.get("capabilities")
                if isinstance(snapshot_extra.get("capabilities"), dict)
                else {}
            )
            mask_capability_keys = ("image_mask", "mask_edit", "inpainting")
            declared_mask_capabilities = [
                key for key in mask_capability_keys if key in capabilities
            ]
            if declared_mask_capabilities and not any(
                capabilities.get(key) is True for key in declared_mask_capabilities
            ):
                raise HTTPException(400, "所选图片模型已明确声明不支持蒙版编辑")
            task_params["_reviewed_evidence_mask"] = {
                "schema_version": "image-mask-plan.v1",
                "operation_id": reviewed_evidence_plan.operation_id,
                "revision_id": reviewed_evidence_plan.revision_id,
                "revision_version": reviewed_evidence_plan.revision_version,
                "source_index": reviewed_evidence_plan.source_index,
                "evidence_ids": reviewed_evidence_plan.evidence_ids,
            }
        if reproduction_context is not None:
            if reproduction_context.get("mode") != "image_inpaint":
                raise HTTPException(409, "纠偏计划的媒体类型与图片生成请求不一致")
            snapshot_extra = (
                snapshot.get("extra") if isinstance(snapshot.get("extra"), dict) else {}
            )
            edit_path = snapshot_extra.get("edit_path", settings.image_edit_path)
            if not edit_source_url or not edit_path:
                raise HTTPException(400, "所选图片模型不支持复刻纠偏局部重绘")
            capabilities = (
                snapshot_extra.get("capabilities")
                if isinstance(snapshot_extra.get("capabilities"), dict)
                else {}
            )
            declared = [
                key for key in ("image_mask", "mask_edit", "inpainting")
                if key in capabilities
            ]
            if declared and not any(capabilities.get(key) is True for key in declared):
                raise HTTPException(400, "所选图片模型已明确声明不支持局部重绘")
    if body.category == "video":
        references = build_video_prompt_references(
            source_asset_url=source_asset_url,
            source_type=source_type,
            params=task_params,
            source_video_analysis_only=analysis_only_source_video,
        )
        snapshot_extra = snapshot.get("extra") if isinstance(snapshot.get("extra"), dict) else {}
        model_profiles = snapshot_extra.get("video_prompt_profiles") or snapshot_extra.get(
            "prompt_profiles"
        )
        if not isinstance(model_profiles, dict):
            model_profiles = None
        # 反推链路的证据门分镜（verified/vlm_only 三态运动信息）存放在已应用
        # 的反推结果版本里，而不是客户端提交的 prompt 里。这里只在编译输入上
        # 合并：存库的 task.prompt 与请求指纹保持不变，没有血缘或没有证据门
        # 的任务行为与旧版完全一致。
        compiled = compile_video_prompt(
            merge_lineage_video_analysis(
                prompt,
                lineage_video_analysis_for_compile(
                    getattr(lineage_revision, "payload", None)
                ),
            ),
            duration=video_render_duration(task_params, body.stage),
            model_id=str(snapshot.get("model_id") or ""),
            provider=str(snapshot.get("provider") or ""),
            extra=snapshot_extra,
            references=references,
            product_reference=any(item.get("role") == "product" for item in references),
            portrait_reference=any(item.get("role") == "character" for item in references),
            product_lock_mode=str(task_params.get("product_lock_mode") or "locked"),
            product_video_template=str(
                task_params.get("product_video_template") or "prompt_driven"
            ),
            model_profiles=model_profiles,
            fit_mode="single_clip",
        )
        if compiled.get("sequence_required"):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "video_prompt_too_long",
                    "error_type": "user_input",
                    "error_message": (
                        "核心单视频提示词在自动精简后仍超过模型承载能力，"
                        "请减少单个动作描述或技术约束后重试。"
                    ),
                    "sequence_required": False,
                    "warnings": list((compiled.get("plan") or {}).get("warnings") or []),
                },
            )
        store_video_prompt_compile(task_params, compiled, references)

    n_images = int(task_params.get("n") or default_image_n(db)) if body.category == "image" else 1
    if reproduction_context is not None and body.category == "image":
        n_images = 1
    # Persist the resolved n so freeze (here), the worker, and settlement all
    # agree even when the client omitted n.
    if body.category == "image":
        task_params["n"] = n_images
    request_fingerprint = build_request_fingerprint(
        category=body.category,
        stage=body.stage,
        source_asset_url=source_asset_url,
        source_type=source_type,
        prompt=prompt,
        params=task_params,
        parent_task_id=body.parent_task_id,
        model_config_id=(
            None
            if existing_client_task is not None
            and getattr(existing_client_task, "model_config_id", None) is None
            and body.model_config_id is None
            else int(model.id)
        ),
        project_id=project_id,
        shot_context=shot_context,
    )
    request_fingerprint = _bind_reverse_lineage_fingerprint(
        request_fingerprint,
        reverse_operation_id=reverse_operation_id,
        reverse_revision_id=reverse_revision_id,
    )
    request_fingerprint = _bind_recipe_attribution_fingerprint(
        request_fingerprint,
        recipe_attribution,
    )
    request_fingerprint = _bind_reproduction_remediation_fingerprint(
        request_fingerprint,
        reproduction_context,
    )
    if existing_client_task is not None:
        assert_client_request_replay(existing_client_task, request_fingerprint)
    if body.stage != "final" or parent is None:
        source_asset_ref = project_collection.asset_ref_for_url(
            db,
            user.id,
            source_asset_url,
        )
        task_params["_source_trace"] = _source_trace(
            source_asset_url=source_asset_url,
            source_type=source_type,
            source_asset_ref=source_asset_ref,
            meta=body.source_asset_meta,
        )
    elif "_source_trace" not in task_params and parent is not None:
        parent_trace = (parent.params or {}).get("_source_trace")
        if isinstance(parent_trace, dict):
            task_params["_source_trace"] = parent_trace
    if recipe_attribution is not None:
        task_params["_recipe_attribution"] = recipe_attribution.snapshot()
    task_params["_client_request_fingerprint"] = request_fingerprint
    cost = estimate_generation_cost_from_snapshot(
        snapshot,
        body.category,
        body.stage,
        n_images,
        params=task_params,
        source_type=source_type,
    )

    request_snapshot = {
        "client_request_id": client_request_id,
        "source_asset_url": source_asset_url,
        "source_type": source_type,
        "category": body.category,
        "stage": body.stage,
        "parent_task_id": body.parent_task_id,
        "prompt": prompt,
        "params": {
            str(key): value for key, value in task_params.items() if not str(key).startswith("_")
        },
        "model_config_id": int(model.id),
        "project_id": project_id,
        "creation_recipe": (
            recipe_attribution.snapshot() if recipe_attribution is not None else None
        ),
        "reverse_operation_id": reverse_operation_id,
        "reverse_revision_id": reverse_revision_id,
        "reproduction_remediation_id": body.reproduction_remediation_id,
        "reproduction_plan_item_id": body.reproduction_plan_item_id,
        "shot_context": shot_context,
    }
    return PreparedGeneration(
        client_request_id=client_request_id,
        existing_client_task=existing_client_task,
        parent=parent,
        prompt=prompt,
        source_asset_url=source_asset_url,
        source_type=source_type,
        task_params=task_params,
        model=model,
        model_use=model_use,
        snapshot=snapshot,
        n_images=n_images,
        request_fingerprint=request_fingerprint,
        cost=cost,
        request_snapshot=request_snapshot,
        project_id=project_id,
        recipe_attribution=recipe_attribution,
        reverse_operation_id=reverse_operation_id,
        reverse_revision_id=reverse_revision_id,
    )


def _quote_rate_limit(user_id: int) -> None:
    key = f"gen:quote:rate:{user_id}"
    if incr_window(key, 3600) > max(100, int(settings.user_gen_rate_per_hour) * 5):
        raise HTTPException(429, "报价请求过于频繁,请稍后再试")


def _validate_execution_quote_request(
    schema: type[BaseModel],
    request_payload: dict,
) -> BaseModel:
    try:
        return schema.model_validate(request_payload)
    except ValidationError as exc:
        raise HTTPException(
            422,
            detail={
                "code": "QUOTE_REQUEST_INVALID",
                "message": "报价请求参数无效",
                "errors": exc.errors(
                    include_url=False,
                    include_context=False,
                    include_input=False,
                ),
            },
        ) from exc


def _quote_generation_intent(
    body: GenerationQuoteIn,
    *,
    db: Session,
    user: User,
):
    prepared = _prepare_generation(body, db=db, user=user)
    if prepared.existing_client_task is not None:
        existing_quote_id = prepared.existing_client_task.quote_id
        if existing_quote_id is not None:
            existing_quote = db.get(GenerationQuote, existing_quote_id)
            if existing_quote is not None:
                return existing_quote
        raise HTTPException(
            409,
            detail={"code": "TASK_ALREADY_EXISTS", "message": "该请求已创建生成任务"},
        )
    if prepared.parent is not None:
        existing_final = _existing_active_final(db, user.id, prepared.parent.id)
        if existing_final is not None:
            if existing_final.quote_id is not None:
                existing_quote = db.get(GenerationQuote, existing_final.quote_id)
                if existing_quote is not None:
                    return existing_quote
            raise HTTPException(
                409,
                detail={"code": "TASK_ALREADY_EXISTS", "message": "该预览已创建最终生成任务"},
            )
    replay = find_idempotent_quote(
        db,
        user_id=user.id,
        kind="generation",
        client_request_id=body.client_request_id,
        request_fingerprint=prepared.request_fingerprint,
    )
    if replay is not None:
        return replay
    _quote_rate_limit(user.id)
    return create_generation_quote(
        db,
        user_id=user.id,
        model=prepared.model,
        request_fingerprint=prepared.request_fingerprint,
        category=body.category,
        stage=body.stage,
        request_snapshot=prepared.request_snapshot,
        model_snapshot=prepared.snapshot,
        params=prepared.task_params,
        source_type=prepared.source_type,
        estimated_credits=prepared.cost,
    )


@router.post("/quotes", response_model=GenerationQuoteOut, status_code=201)
def quote_generation(
    body: GenerationQuoteIn | ExecutionQuoteIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if isinstance(body, ExecutionQuoteIn) and (
        (body.kind == "reverse_batch" and not feature_enabled("reverse_batch_enabled"))
        or (body.kind == "workflow" and not feature_enabled("tool_workflows_enabled"))
    ):
        raise HTTPException(404, "not found")
    if isinstance(body, GenerationQuoteIn):
        quote = _quote_generation_intent(body, db=db, user=user)
    else:
        request_payload = dict(body.request or {})
        nested_request_id = request_payload.get("client_request_id")
        if (
            nested_request_id is not None
            and str(nested_request_id).strip() != body.client_request_id
        ):
            raise HTTPException(
                409,
                detail={
                    "code": "QUOTE_IDEMPOTENCY_CONFLICT",
                    "message": "报价 envelope 与 request 的 client_request_id 不一致",
                },
            )
        request_payload.pop("quote_id", None)
        if body.kind == "prompt_optimization":
            nested_idempotency_key = request_payload.get("idempotency_key")
            if (
                nested_idempotency_key is not None
                and str(nested_idempotency_key).strip() != body.client_request_id
            ):
                raise HTTPException(
                    409,
                    detail={
                        "code": "QUOTE_IDEMPOTENCY_CONFLICT",
                        "message": "报价 envelope 与 request 的 idempotency_key 不一致",
                    },
                )
            request_payload.pop("client_request_id", None)
            request_payload["idempotency_key"] = body.client_request_id
        else:
            request_payload["client_request_id"] = body.client_request_id
        _quote_rate_limit(user.id)
        if body.kind == "generation":
            quote = _quote_generation_intent(
                _validate_execution_quote_request(GenerationQuoteIn, request_payload),
                db=db,
                user=user,
            )
        elif body.kind == "reverse":
            retry_operation_id = request_payload.get("reverse_operation_id")
            if retry_operation_id is None:
                try:
                    quote = create_reverse_quote(
                        db,
                        user_id=user.id,
                        body=_validate_execution_quote_request(
                            ReverseOperationCreate,
                            request_payload,
                        ),
                    )
                except reverse_operations.ReverseOperationConflict as exc:
                    raise HTTPException(409, str(exc)) from exc
                except reverse_operations.ReverseOperationInvalid as exc:
                    raise HTTPException(400, str(exc)) from exc
                except project_collection.ProjectNotFound as exc:
                    raise HTTPException(404, str(exc)) from exc
            else:
                unexpected = set(request_payload).difference(
                    {"reverse_operation_id", "client_request_id", "model_config_id"}
                )
                if (
                    unexpected
                    or isinstance(retry_operation_id, bool)
                    or not isinstance(retry_operation_id, int)
                    or retry_operation_id <= 0
                ):
                    raise HTTPException(
                        422,
                        detail={
                            "code": "QUOTE_REQUEST_INVALID",
                            "message": "反推重试报价只接受任务、请求标识和可选模型",
                        },
                    )
                try:
                    previous, retry_body = reverse_operations.build_retry_operation_body(
                        db,
                        operation_id=retry_operation_id,
                        user_id=user.id,
                        client_request_id=body.client_request_id,
                        model_config_id=request_payload.get("model_config_id"),
                    )
                except reverse_operations.ReverseOperationNotFound as exc:
                    raise HTTPException(404, str(exc)) from exc
                except reverse_operations.ReverseOperationConflict as exc:
                    raise HTTPException(409, str(exc)) from exc
                except reverse_operations.ReverseOperationInvalid as exc:
                    raise HTTPException(400, str(exc)) from exc
                except project_collection.ProjectNotFound as exc:
                    raise HTTPException(404, str(exc)) from exc
                try:
                    quote = create_reverse_quote(
                        db,
                        user_id=user.id,
                        body=retry_body,
                        retry_of_operation_id=int(previous.id),
                    )
                except reverse_operations.ReverseOperationConflict as exc:
                    raise HTTPException(409, str(exc)) from exc
                except reverse_operations.ReverseOperationInvalid as exc:
                    raise HTTPException(400, str(exc)) from exc
                except project_collection.ProjectNotFound as exc:
                    raise HTTPException(404, str(exc)) from exc
        elif body.kind == "reverse_batch":
            try:
                quote = create_reverse_batch_quote(
                    db,
                    user_id=user.id,
                    body=_validate_execution_quote_request(
                        ReverseBatchCreate,
                        request_payload,
                    ),
                )
            except reverse_operations.ReverseOperationConflict as exc:
                raise HTTPException(409, str(exc)) from exc
            except reverse_operations.ReverseOperationInvalid as exc:
                raise HTTPException(400, str(exc)) from exc
            except project_collection.ProjectNotFound as exc:
                raise HTTPException(404, str(exc)) from exc
        elif body.kind == "workflow":
            quote = create_workflow_quote(
                db,
                user_id=user.id,
                body=_validate_execution_quote_request(ToolRunCreateIn, request_payload),
            )
        elif body.kind == "prompt_optimization":
            quote = create_prompt_optimization_quote(
                db,
                user_id=user.id,
                body=_validate_execution_quote_request(
                    StudioPromptOptimizationIn,
                    request_payload,
                ),
            )
        else:
            unexpected = set(request_payload).difference({"asset_id", "client_request_id"})
            asset_id = request_payload.get("asset_id")
            if (
                unexpected
                or isinstance(asset_id, bool)
                or not isinstance(asset_id, int)
                or asset_id <= 0
            ):
                raise HTTPException(
                    422,
                    detail={
                        "code": "QUOTE_REQUEST_INVALID",
                        "message": "素材解锁报价只接受有效的 asset_id",
                    },
                )
            quote = create_asset_unlock_quote(
                db,
                user_id=user.id,
                asset_id=asset_id,
                client_request_id=body.client_request_id,
            )
    if quote.kind == "generation":
        snapshot = quote.request_snapshot if isinstance(quote.request_snapshot, dict) else {}
        recipe_usage.record_generation_prepare(
            db,
            attribution=recipe_usage.attribution_from_snapshot(snapshot.get("creation_recipe")),
            user_id=int(user.id),
            quote_id=int(quote.id),
            category=quote.category,
        )
    db.commit()
    db.refresh(quote)
    return generation_quote_out(db, quote)


@router.post("/generate", response_model=TaskOut)
def generate(
    body: GenerateIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    quote_lock_key = f"gen:quote:consume:{user.id}:{body.quote_id}"
    quote_lock_token = locks.acquire(quote_lock_key, ttl=60)
    if not quote_lock_token:
        raise HTTPException(
            409, detail={"code": "QUOTE_IN_USE", "message": "报价正在使用，请稍后刷新"}
        )

    try:
        quote_task = db.scalar(
            select(GenTask).where(
                GenTask.user_id == user.id,
                GenTask.quote_id == body.quote_id,
            )
        )
        if quote_task is not None:
            consumed_quote = db.scalar(
                select(GenerationQuote)
                .where(
                    GenerationQuote.id == body.quote_id,
                    GenerationQuote.user_id == user.id,
                )
                .with_for_update()
            )
            if consumed_quote is None:
                raise HTTPException(
                    409,
                    detail={"code": "QUOTE_STATE_INVALID", "message": "生成任务的报价记录缺失"},
                )
            prepared = _prepare_generation(body, db=db, user=user, quote=consumed_quote)
            if (
                prepared.existing_client_task is None
                or int(prepared.existing_client_task.id) != int(quote_task.id)
                or consumed_quote.status != "consumed"
                or int(consumed_quote.task_id or 0) != int(quote_task.id)
            ):
                raise HTTPException(
                    409,
                    detail={
                        "code": "IDEMPOTENCY_CONFLICT",
                        "message": "报价已用于不同的生成请求",
                    },
                )
            validate_generation_quote(
                db,
                consumed_quote,
                request_fingerprint=prepared.request_fingerprint,
                model=prepared.model,
            )
            return build_task_out(db, quote_task)

        locked_quote = lock_generation_quote(db, quote_id=body.quote_id, user_id=user.id)
        prepared = _prepare_generation(body, db=db, user=user, quote=locked_quote)
        if prepared.existing_client_task is not None:
            return build_task_out(db, prepared.existing_client_task)
        if prepared.parent is not None:
            existing_final = _existing_active_final(db, user.id, prepared.parent.id)
            if existing_final is not None:
                return build_task_out(db, existing_final)
        _rate_limit(user.id)

        quote = locked_quote
        validate_generation_quote(
            db,
            quote,
            request_fingerprint=prepared.request_fingerprint,
            model=prepared.model,
        )

        task_snapshot = quoted_model_snapshot(quote, prepared.snapshot)
        prepared.task_params["_model_snapshot"] = task_snapshot
        prepared.task_params["_quote"] = {
            "quote_id": int(quote.id),
            "capability_version_id": int(quote.capability_version_id),
            "price_version_id": int(quote.price_version_id),
            "estimated_credits": int(quote.estimated_credits),
            "expires_at": quote.expires_at.isoformat(),
        }
        cost = int(quote.estimated_credits)

        def replay_conflicting_task() -> TaskOut | None:
            existing = _existing_client_request_task(db, user.id, prepared.client_request_id)
            if existing is not None:
                assert_client_request_replay(existing, prepared.request_fingerprint)
                return build_task_out(db, existing)
            quote_task = db.scalar(
                select(GenTask).where(
                    GenTask.user_id == user.id,
                    GenTask.quote_id == quote.id,
                )
            )
            if quote_task is not None:
                return build_task_out(db, quote_task)
            if body.category == "video" and body.stage == "final" and prepared.parent is not None:
                active = _existing_active_final(db, user.id, prepared.parent.id)
                if active is not None:
                    return build_task_out(db, active)
            return None

        def create_task() -> TaskOut:
            return submit_generation_task(
                db,
                user_id=user.id,
                source_asset_url=prepared.source_asset_url,
                source_type=prepared.source_type,
                category=body.category,
                stage=body.stage,
                prompt=prepared.prompt,
                model_use=prepared.model_use,
                model_config_id=int(prepared.model.id),
                quote=quote,
                params=prepared.task_params,
                client_request_id=prepared.client_request_id,
                cost=cost,
                parent_task_id=body.parent_task_id,
                project_id=prepared.project_id,
                reverse_operation_id=prepared.reverse_operation_id,
                reverse_revision_id=prepared.reverse_revision_id,
                recipe_attribution=prepared.recipe_attribution,
                ip=get_client_ip(request),
                replay_conflicting_task=replay_conflicting_task,
            )

        if body.category == "video" and body.stage == "final" and prepared.parent:
            lock_key = f"gen:final:create:{user.id}:{prepared.parent.id}"
            lock_token = locks.acquire(lock_key, ttl=60)
            if not lock_token:
                active = _existing_active_final(db, user.id, prepared.parent.id)
                if active:
                    return build_task_out(db, active)
                raise HTTPException(409, "高清视频任务正在创建,请稍后刷新")
            try:
                active = _existing_active_final(db, user.id, prepared.parent.id)
                if active:
                    return build_task_out(db, active)
                result = create_task()
                if result.dispatch_reconciliation_required:
                    response.status_code = 202
                return result
            finally:
                locks.release(lock_key, lock_token)

        result = create_task()
        if result.dispatch_reconciliation_required:
            response.status_code = 202
        return result
    finally:
        locks.release(quote_lock_key, quote_lock_token)


@router.post("/tasks/{task_id}/retry/requote", response_model=TaskOut)
def requote_retry_generation(
    task_id: int,
    body: RetryRequoteIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create a new task using the failed task's public request and current quote."""
    source = db.scalar(
        select(GenTask).where(GenTask.id == task_id, GenTask.user_id == user.id).with_for_update()
    )
    if source is None:
        raise HTTPException(404, "任务不存在")
    if source.status != "failed":
        raise HTTPException(400, "仅失败任务可重新报价重试")

    client_request_id = _normalize_client_request_id(body.client_request_id)
    selected_model_config_id = generation_retry.requote_model_config_id(
        source,
        body.model_config_id,
    )
    existing = _existing_client_request_task(db, user.id, client_request_id)
    if existing is not None:
        generation_retry.assert_requote_replay(
            existing,
            source_task_id=int(source.id),
            selected_model_config_id=selected_model_config_id,
        )
        return build_task_out(db, existing)

    # All reconciliation and immutable-lineage checks must run before route
    # selection, because a half-open route claim itself is persisted state.
    assert_retry_submission_state_known(source.params)
    generation_retry.assert_retry_dispatch_reconciled(db, source)
    generation_retry.assert_retry_generation_lineage(db, source)

    retry_body = generation_retry.build_requote_input(
        db,
        task=source,
        user_id=int(user.id),
        client_request_id=str(client_request_id),
        model_config_id=selected_model_config_id,
    )
    source_recipe_attribution = _task_recipe_attribution(source)
    old_route_id = generation_retry.source_route_id(source)
    prepared = _prepare_generation(
        retry_body,
        db=db,
        user=user,
        fresh_route=True,
        avoid_route_ids={old_route_id} if old_route_id is not None else None,
        trusted_recipe_attribution=source_recipe_attribution,
    )
    if prepared.existing_client_task is not None:
        existing = prepared.existing_client_task
        generation_retry.assert_requote_replay(
            existing,
            source_task_id=int(source.id),
            selected_model_config_id=selected_model_config_id,
        )
        return build_task_out(db, existing)

    _rate_limit(user.id)
    route_snapshot = (
        prepared.snapshot.get("route_snapshot")
        if isinstance(prepared.snapshot.get("route_snapshot"), dict)
        else {}
    )
    new_route_id = route_snapshot.get("route_id")
    source_quote = db.get(GenerationQuote, source.quote_id) if source.quote_id is not None else None
    source_model_config_id = (
        int(source.model_config_id) if source.model_config_id is not None else None
    )
    retry_meta = {
        "mode": "requote",
        "source_task_id": int(source.id),
        "source_model_config_id": source_model_config_id,
        "selected_model_config_id": int(prepared.model.id),
        "model_switched": source_model_config_id != int(prepared.model.id),
        "source_quote_id": int(source.quote_id) if source.quote_id is not None else None,
        "source_estimated_credits": (
            int(source_quote.estimated_credits)
            if source_quote is not None
            else int(source.cost_frozen or 0)
        ),
        "source_route_id": old_route_id,
        "selected_route_id": int(new_route_id) if new_route_id is not None else None,
    }
    prepared.request_snapshot["retry"] = dict(retry_meta)
    prepared.task_params["_retry"] = dict(retry_meta)

    quote = create_generation_quote(
        db,
        user_id=user.id,
        model=prepared.model,
        request_fingerprint=prepared.request_fingerprint,
        category=retry_body.category,
        stage=retry_body.stage,
        request_snapshot=prepared.request_snapshot,
        model_snapshot=prepared.snapshot,
        params=prepared.task_params,
        source_type=prepared.source_type,
        estimated_credits=prepared.cost,
    )
    recipe_usage.record_generation_prepare(
        db,
        attribution=prepared.recipe_attribution,
        user_id=int(user.id),
        quote_id=int(quote.id),
        category=retry_body.category,
    )
    retry_meta["selected_quote_id"] = int(quote.id)
    retry_meta["selected_estimated_credits"] = int(quote.estimated_credits)
    quote.request_snapshot = {
        **dict(quote.request_snapshot or {}),
        "retry": dict(retry_meta),
    }
    prepared.task_params["_retry"] = dict(retry_meta)
    prepared.task_params["_model_snapshot"] = quoted_model_snapshot(
        quote,
        prepared.snapshot,
    )
    prepared.task_params["_quote"] = {
        "quote_id": int(quote.id),
        "capability_version_id": int(quote.capability_version_id),
        "price_version_id": int(quote.price_version_id),
        "estimated_credits": int(quote.estimated_credits),
        "expires_at": quote.expires_at.isoformat(),
    }

    source_task_id = int(source.id)

    def replay_conflicting_task() -> TaskOut | None:
        replay = _existing_client_request_task(db, user.id, client_request_id)
        if replay is None:
            return None
        generation_retry.assert_requote_replay(
            replay,
            source_task_id=source_task_id,
            selected_model_config_id=selected_model_config_id,
        )
        return build_task_out(db, replay)

    result = submit_generation_task(
        db,
        user_id=user.id,
        source_asset_url=prepared.source_asset_url,
        source_type=prepared.source_type,
        category=retry_body.category,
        stage=retry_body.stage,
        prompt=prepared.prompt,
        model_use=prepared.model_use,
        model_config_id=int(prepared.model.id),
        quote=quote,
        params=prepared.task_params,
        client_request_id=prepared.client_request_id,
        cost=int(quote.estimated_credits),
        parent_task_id=retry_body.parent_task_id,
        project_id=prepared.project_id,
        reverse_operation_id=prepared.reverse_operation_id,
        reverse_revision_id=prepared.reverse_revision_id,
        recipe_attribution=prepared.recipe_attribution,
        ip=get_client_ip(request),
        replay_conflicting_task=replay_conflicting_task,
        retry_of_task_id=source_task_id,
    )
    if result.dispatch_reconciliation_required:
        response.status_code = 202
    return result
