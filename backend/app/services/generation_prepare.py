"""Prepare generation requests before quote or task submission."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import GenAsset, GenerationQuote, GenTask, User
from ..schemas import GenerationRequestBase
from . import generation, project_collection, recipe_usage
from .config_store import ModelConfigResolutionError, resolve_model_config
from .gateway_config_errors import raise_gateway_config_http
from .generation import model_snapshot
from .generation_dispatch import validate_dispatch_request
from .generation_image_evidence import (
    ReviewedEvidenceMaskError,
    resolve_reviewed_evidence_plan_for_lineage,
)
from .generation_media import video_render_duration
from .generation_model_runtime import model_from_persisted_snapshot
from .generation_policy import (
    assert_generation_request_policy,
    validate_prompt_payload,
)
from .generation_prompts import compact_image_prompt_payload
from .generation_quotes import validate_quote_snapshot_integrity
from .generation_request import (
    assert_client_request_replay,
    default_image_n,
    estimate_generation_cost_from_snapshot,
    validate_generation_params,
    validate_product_detail_images,
)
from .generation_request import request_fingerprint as build_request_fingerprint
from .generation_submit import validate_generation_lineage
from .generation_video_submit import (
    lineage_video_analysis_for_compile,
    merge_lineage_video_analysis,
)
from .model_routes import (
    ModelRouteUnavailable,
    attach_route_snapshot,
    select_model_route,
)
from .product_edition import feature_enabled
from .video_prompt_compiler import (
    build_video_prompt_references,
    compile_video_prompt,
    store_video_prompt_compile,
)

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


@dataclass
class _GenerationRequestContext:
    client_request_id: str | None
    existing_client_task: GenTask | None
    params: dict
    reproduction_context: dict | None
    shot_context: dict | None
    request_prompt: dict


@dataclass
class _GenerationLineageContext:
    parent: GenTask | None
    operation_id: int | None
    revision_id: int | None
    operation: object | None
    revision: object | None
    project_id: int | None
    recipe_attribution: recipe_usage.RecipeAttribution | None


@dataclass
class _GenerationPayload:
    prompt: dict
    source_asset_url: str | None
    source_type: str | None
    task_params: dict
    analysis_only_source_video: bool


@dataclass
class _GenerationModelContext:
    model: object
    model_use: str
    policy_model: object
    snapshot: dict


@dataclass(frozen=True)
class _GenerationPrepareDependencies:
    snapshot_builder: Callable[[object], dict]
    snapshot_loader: Callable[..., object]


_DEFAULT_PREPARE_DEPENDENCIES = _GenerationPrepareDependencies(
    snapshot_builder=model_snapshot,
    snapshot_loader=model_from_persisted_snapshot,
)


def _validate_generation_features(body: GenerationRequestBase) -> None:
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


def _validate_reproduction_request(
    body: GenerationRequestBase,
    *,
    db: Session,
    user: User,
) -> dict | None:
    if body.reproduction_remediation_id is None:
        return None

    from ..services import reproduction_remediation

    request_payload = body.model_dump(
        mode="json",
        exclude_none=True,
        exclude={"quote_id"},
    )
    try:
        return reproduction_remediation.validate_generation_request(
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


def _validated_request_prompt(body: GenerationRequestBase, params: dict) -> dict:
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
    return request_prompt


def _prepare_request_context(
    body: GenerationRequestBase,
    *,
    db: Session,
    user: User,
) -> _GenerationRequestContext:
    _validate_generation_features(body)
    validate_dispatch_request(body.category)
    client_request_id = _normalize_client_request_id(body.client_request_id)
    existing_client_task = _existing_client_request_task(db, user.id, client_request_id)
    params = validate_generation_params(body.category, body.params)
    return _GenerationRequestContext(
        client_request_id=client_request_id,
        existing_client_task=existing_client_task,
        params=params,
        reproduction_context=_validate_reproduction_request(body, db=db, user=user),
        shot_context=(
            body.shot_context.model_dump(mode="json") if body.shot_context else None
        ),
        request_prompt=_validated_request_prompt(body, params),
    )


def _resolve_final_parent(
    body: GenerationRequestBase,
    *,
    db: Session,
    user: User,
) -> GenTask | None:
    if body.stage != "final":
        return None
    if body.category != "video":
        raise HTTPException(400, "final 阶段仅支持视频高清渲染")
    if not body.parent_task_id:
        return None

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
    return parent


def _resolve_project_id(
    body: GenerationRequestBase,
    *,
    db: Session,
    user: User,
    inherited_task: GenTask | None,
) -> int | None:
    project_id = body.project_id
    if project_id is None and inherited_task is not None:
        project_id = project_collection.primary_project_id_for_task(
            db,
            user_id=user.id,
            task_kind="generation",
            task_id=int(inherited_task.id),
        )
    if project_id is not None:
        try:
            project_collection.require_owned_project(db, user.id, project_id)
        except project_collection.ProjectNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
    return project_id


def _resolve_lineage_context(
    body: GenerationRequestBase,
    *,
    db: Session,
    user: User,
    request: _GenerationRequestContext,
    trusted_recipe_attribution: recipe_usage.RecipeAttribution | None,
) -> _GenerationLineageContext:
    parent = _resolve_final_parent(body, db=db, user=user)
    operation_id, revision_id = _resolved_reverse_lineage(
        body,
        parent=parent,
        existing_client_task=request.existing_client_task,
    )
    operation, revision = validate_generation_lineage(
        db,
        user_id=user.id,
        reverse_operation_id=operation_id,
        reverse_revision_id=revision_id,
    )
    inherited_task = parent or request.existing_client_task
    return _GenerationLineageContext(
        parent=parent,
        operation_id=operation_id,
        revision_id=revision_id,
        operation=operation,
        revision=revision,
        project_id=_resolve_project_id(
            body,
            db=db,
            user=user,
            inherited_task=inherited_task,
        ),
        recipe_attribution=_resolved_recipe_attribution(
            body,
            db=db,
            user=user,
            parent=parent,
            existing_client_task=request.existing_client_task,
            trusted_attribution=trusted_recipe_attribution,
        ),
    )


def _assemble_generation_payload(
    body: GenerationRequestBase,
    *,
    request: _GenerationRequestContext,
    lineage: _GenerationLineageContext,
) -> _GenerationPayload:
    if body.stage == "final" and lineage.parent:
        prompt = dict(lineage.parent.prompt or {})
        source_asset_url = lineage.parent.source_asset_url
        source_type = lineage.parent.source_type
        task_params = _final_params_from_parent(lineage.parent.params)
    else:
        prompt = dict(request.request_prompt)
        if not prompt:
            raise HTTPException(400, "请提供 prompt 或 instruction")
        source_asset_url = body.source_asset_url
        source_type = body.source_type
        task_params = request.params

    analysis_only_source_video = bool(
        body.category == "video"
        and source_type == "video"
        and source_asset_url
        and lineage.operation is not None
    )
    if analysis_only_source_video and not _matches_reverse_video_source(
        lineage.operation,
        source_asset_url,
    ):
        raise HTTPException(409, "视频生成素材与已应用的反推源视频不一致")
    if request.reproduction_context is not None:
        task_params["_reproduction_context"] = dict(request.reproduction_context)
    if request.shot_context is not None:
        task_params["_shot_context"] = request.shot_context
    if body.category == "video":
        validate_product_detail_images(task_params)
    return _GenerationPayload(
        prompt=prompt,
        source_asset_url=source_asset_url,
        source_type=source_type,
        task_params=task_params,
        analysis_only_source_video=analysis_only_source_video,
    )


def _selected_model_config(
    body: GenerationRequestBase,
    *,
    quote: GenerationQuote | None,
    fresh_route: bool,
    lineage: _GenerationLineageContext,
    existing_client_task: GenTask | None,
) -> tuple[int | None, bool]:
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
        return int(quote.model_config_id), False
    if lineage.parent is not None and body.stage == "final":
        parent_model_config_id = getattr(lineage.parent, "model_config_id", None)
        if (
            selected_model_config_id is not None
            and parent_model_config_id is not None
            and int(selected_model_config_id) != int(parent_model_config_id)
        ):
            raise HTTPException(409, "视频最终生成必须使用预览任务绑定的同一模型")
        return parent_model_config_id or selected_model_config_id, fresh_route
    if existing_client_task is not None:
        existing_model_config_id = getattr(existing_client_task, "model_config_id", None)
        if (
            selected_model_config_id is not None
            and existing_model_config_id is not None
            and int(selected_model_config_id) != int(existing_model_config_id)
        ):
            raise HTTPException(409, "client_request_id 已用于不同模型配置")
        return existing_model_config_id or selected_model_config_id, False
    return selected_model_config_id, require_enabled


def _runtime_model_for_snapshot(
    db: Session,
    *,
    snapshot: dict,
    model: object,
    model_use: str,
    dependencies: _GenerationPrepareDependencies,
) -> object:
    try:
        return dependencies.snapshot_loader(db, snapshot, model, model_use)
    except generation.ModelSnapshotMismatchError as exc:
        raise_gateway_config_http(exc)


def _resolve_generation_model(
    body: GenerationRequestBase,
    *,
    db: Session,
    request: _GenerationRequestContext,
    lineage: _GenerationLineageContext,
    quote: GenerationQuote | None,
    fresh_route: bool,
    avoid_route_ids: set[int] | None,
    dependencies: _GenerationPrepareDependencies,
) -> _GenerationModelContext:
    model_use = body.category
    selected_model_config_id, require_enabled = _selected_model_config(
        body,
        quote=quote,
        fresh_route=fresh_route,
        lineage=lineage,
        existing_client_task=request.existing_client_task,
    )
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
        policy_model = _runtime_model_for_snapshot(
            db,
            snapshot=snapshot,
            model=model,
            model_use=model_use,
            dependencies=dependencies,
        )
    elif lineage.parent is not None and body.stage == "final" and not fresh_route:
        snapshot = dict(lineage.parent.params or {}).get("_model_snapshot") or {}
        if snapshot:
            policy_model = _runtime_model_for_snapshot(
                db,
                snapshot=snapshot,
                model=model,
                model_use=model_use,
                dependencies=dependencies,
            )
        snapshot = snapshot or dependencies.snapshot_builder(model)
    elif request.existing_client_task is not None and not fresh_route:
        snapshot = (
            dict(request.existing_client_task.params or {}).get("_model_snapshot") or {}
        )
        if snapshot:
            policy_model = _runtime_model_for_snapshot(
                db,
                snapshot=snapshot,
                model=model,
                model_use=model_use,
                dependencies=dependencies,
            )
        snapshot = snapshot or dependencies.snapshot_builder(model)
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
            dependencies.snapshot_builder(route_selection.runtime_model),
            route_selection,
        )
    return _GenerationModelContext(
        model=model,
        model_use=model_use,
        policy_model=policy_model,
        snapshot=snapshot,
    )


def _snapshot_image_edit_capabilities(
    snapshot: dict,
    *,
    edit_source_url: str | None,
    unsupported_message: str,
    capability_message: str,
) -> None:
    snapshot_extra = snapshot.get("extra") if isinstance(snapshot.get("extra"), dict) else {}
    edit_path = snapshot_extra.get("edit_path", settings.image_edit_path)
    if not edit_source_url or not edit_path:
        raise HTTPException(400, unsupported_message)
    capabilities = (
        snapshot_extra.get("capabilities")
        if isinstance(snapshot_extra.get("capabilities"), dict)
        else {}
    )
    declared = [
        key for key in ("image_mask", "mask_edit", "inpainting") if key in capabilities
    ]
    if declared and not any(capabilities.get(key) is True for key in declared):
        raise HTTPException(400, capability_message)


def _apply_image_evidence_requirements(
    *,
    db: Session,
    user: User,
    request: _GenerationRequestContext,
    lineage: _GenerationLineageContext,
    payload: _GenerationPayload,
    snapshot: dict,
) -> None:
    edit_source_url = (
        payload.task_params.get("reference_image_url")
        or payload.task_params.get("character_reference_image")
        or payload.source_asset_url
    )
    try:
        reviewed_evidence_plan = resolve_reviewed_evidence_plan_for_lineage(
            db,
            user_id=int(user.id),
            operation_id=lineage.operation_id,
            revision_id=lineage.revision_id,
            edit_source_url=edit_source_url,
        )
    except ReviewedEvidenceMaskError as exc:
        raise HTTPException(409, str(exc)) from exc
    if reviewed_evidence_plan is not None:
        _snapshot_image_edit_capabilities(
            snapshot,
            edit_source_url=edit_source_url,
            unsupported_message="所选图片模型不支持已确认证据蒙版编辑",
            capability_message="所选图片模型已明确声明不支持蒙版编辑",
        )
        payload.task_params["_reviewed_evidence_mask"] = {
            "schema_version": "image-mask-plan.v1",
            "operation_id": reviewed_evidence_plan.operation_id,
            "revision_id": reviewed_evidence_plan.revision_id,
            "revision_version": reviewed_evidence_plan.revision_version,
            "source_index": reviewed_evidence_plan.source_index,
            "evidence_ids": reviewed_evidence_plan.evidence_ids,
        }
    if request.reproduction_context is not None:
        if request.reproduction_context.get("mode") != "image_inpaint":
            raise HTTPException(409, "纠偏计划的媒体类型与图片生成请求不一致")
        _snapshot_image_edit_capabilities(
            snapshot,
            edit_source_url=edit_source_url,
            unsupported_message="所选图片模型不支持复刻纠偏局部重绘",
            capability_message="所选图片模型已明确声明不支持局部重绘",
        )


def _compile_video_generation_prompt(
    body: GenerationRequestBase,
    *,
    lineage: _GenerationLineageContext,
    payload: _GenerationPayload,
    snapshot: dict,
) -> None:
    references = build_video_prompt_references(
        source_asset_url=payload.source_asset_url,
        source_type=payload.source_type,
        params=payload.task_params,
        source_video_analysis_only=payload.analysis_only_source_video,
    )
    snapshot_extra = snapshot.get("extra") if isinstance(snapshot.get("extra"), dict) else {}
    model_profiles = snapshot_extra.get("video_prompt_profiles") or snapshot_extra.get(
        "prompt_profiles"
    )
    if not isinstance(model_profiles, dict):
        model_profiles = None
    compiled = compile_video_prompt(
        merge_lineage_video_analysis(
            payload.prompt,
            lineage_video_analysis_for_compile(
                getattr(lineage.revision, "payload", None)
            ),
        ),
        duration=video_render_duration(payload.task_params, body.stage),
        model_id=str(snapshot.get("model_id") or ""),
        provider=str(snapshot.get("provider") or ""),
        extra=snapshot_extra,
        references=references,
        product_reference=any(item.get("role") == "product" for item in references),
        portrait_reference=any(item.get("role") == "character" for item in references),
        product_lock_mode=str(payload.task_params.get("product_lock_mode") or "locked"),
        product_video_template=str(
            payload.task_params.get("product_video_template") or "prompt_driven"
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
    store_video_prompt_compile(payload.task_params, compiled, references)


def _apply_generation_policy(
    body: GenerationRequestBase,
    *,
    db: Session,
    user: User,
    request: _GenerationRequestContext,
    lineage: _GenerationLineageContext,
    payload: _GenerationPayload,
    model: _GenerationModelContext,
) -> None:
    assert_generation_request_policy(
        db,
        user_id=int(user.id),
        model=model.policy_model,
        category=body.category,
        source_asset_url=payload.source_asset_url,
        source_type=payload.source_type,
        prompt=payload.prompt,
        params=payload.task_params,
        require_actionable_video_reference=not (
            body.category == "video" and lineage.parent is not None
        ),
        analysis_only_source_video=payload.analysis_only_source_video,
    )
    if body.category == "image":
        _apply_image_evidence_requirements(
            db=db,
            user=user,
            request=request,
            lineage=lineage,
            payload=payload,
            snapshot=model.snapshot,
        )
    elif body.category == "video":
        _compile_video_generation_prompt(
            body,
            lineage=lineage,
            payload=payload,
            snapshot=model.snapshot,
        )


def _resolve_generation_count(
    body: GenerationRequestBase,
    *,
    db: Session,
    reproduction_context: dict | None,
    task_params: dict,
) -> int:
    n_images = int(task_params.get("n") or default_image_n(db)) if body.category == "image" else 1
    if reproduction_context is not None and body.category == "image":
        n_images = 1
    if body.category == "image":
        task_params["n"] = n_images
    return n_images


def _build_bound_request_fingerprint(
    body: GenerationRequestBase,
    *,
    request: _GenerationRequestContext,
    lineage: _GenerationLineageContext,
    payload: _GenerationPayload,
    model: object,
) -> str:
    fingerprint = build_request_fingerprint(
        category=body.category,
        stage=body.stage,
        source_asset_url=payload.source_asset_url,
        source_type=payload.source_type,
        prompt=payload.prompt,
        params=payload.task_params,
        parent_task_id=body.parent_task_id,
        model_config_id=(
            None
            if request.existing_client_task is not None
            and getattr(request.existing_client_task, "model_config_id", None) is None
            and body.model_config_id is None
            else int(model.id)
        ),
        project_id=lineage.project_id,
        shot_context=request.shot_context,
    )
    fingerprint = _bind_reverse_lineage_fingerprint(
        fingerprint,
        reverse_operation_id=lineage.operation_id,
        reverse_revision_id=lineage.revision_id,
    )
    fingerprint = _bind_recipe_attribution_fingerprint(
        fingerprint,
        lineage.recipe_attribution,
    )
    return _bind_reproduction_remediation_fingerprint(
        fingerprint,
        request.reproduction_context,
    )


def _attach_generation_request_metadata(
    body: GenerationRequestBase,
    *,
    db: Session,
    user: User,
    request: _GenerationRequestContext,
    lineage: _GenerationLineageContext,
    payload: _GenerationPayload,
    request_fingerprint: str,
) -> None:
    if request.existing_client_task is not None:
        assert_client_request_replay(request.existing_client_task, request_fingerprint)
    if body.stage != "final" or lineage.parent is None:
        source_asset_ref = project_collection.asset_ref_for_url(
            db,
            user.id,
            payload.source_asset_url,
        )
        payload.task_params["_source_trace"] = _source_trace(
            source_asset_url=payload.source_asset_url,
            source_type=payload.source_type,
            source_asset_ref=source_asset_ref,
            meta=body.source_asset_meta,
        )
    elif "_source_trace" not in payload.task_params:
        parent_trace = (lineage.parent.params or {}).get("_source_trace")
        if isinstance(parent_trace, dict):
            payload.task_params["_source_trace"] = parent_trace
    if lineage.recipe_attribution is not None:
        payload.task_params["_recipe_attribution"] = lineage.recipe_attribution.snapshot()
    payload.task_params["_client_request_fingerprint"] = request_fingerprint


def _generation_request_snapshot(
    body: GenerationRequestBase,
    *,
    request: _GenerationRequestContext,
    lineage: _GenerationLineageContext,
    payload: _GenerationPayload,
    model: object,
) -> dict:
    return {
        "client_request_id": request.client_request_id,
        "source_asset_url": payload.source_asset_url,
        "source_type": payload.source_type,
        "category": body.category,
        "stage": body.stage,
        "parent_task_id": body.parent_task_id,
        "prompt": payload.prompt,
        "params": {
            str(key): value
            for key, value in payload.task_params.items()
            if not str(key).startswith("_")
        },
        "model_config_id": int(model.id),
        "project_id": lineage.project_id,
        "creation_recipe": (
            lineage.recipe_attribution.snapshot()
            if lineage.recipe_attribution is not None
            else None
        ),
        "reverse_operation_id": lineage.operation_id,
        "reverse_revision_id": lineage.revision_id,
        "reproduction_remediation_id": body.reproduction_remediation_id,
        "reproduction_plan_item_id": body.reproduction_plan_item_id,
        "shot_context": request.shot_context,
    }


def _prepare_generation_with_dependencies(
    body: GenerationRequestBase,
    *,
    db: Session,
    user: User,
    quote: GenerationQuote | None = None,
    fresh_route: bool = False,
    avoid_route_ids: set[int] | None = None,
    trusted_recipe_attribution: recipe_usage.RecipeAttribution | None = None,
    dependencies: _GenerationPrepareDependencies,
) -> PreparedGeneration:
    request = _prepare_request_context(body, db=db, user=user)
    lineage = _resolve_lineage_context(
        body,
        db=db,
        user=user,
        request=request,
        trusted_recipe_attribution=trusted_recipe_attribution,
    )
    payload = _assemble_generation_payload(body, request=request, lineage=lineage)
    model = _resolve_generation_model(
        body,
        db=db,
        request=request,
        lineage=lineage,
        quote=quote,
        fresh_route=fresh_route,
        avoid_route_ids=avoid_route_ids,
        dependencies=dependencies,
    )
    _apply_generation_policy(
        body,
        db=db,
        user=user,
        request=request,
        lineage=lineage,
        payload=payload,
        model=model,
    )
    n_images = _resolve_generation_count(
        body,
        db=db,
        reproduction_context=request.reproduction_context,
        task_params=payload.task_params,
    )
    request_fingerprint = _build_bound_request_fingerprint(
        body,
        request=request,
        lineage=lineage,
        payload=payload,
        model=model.model,
    )
    _attach_generation_request_metadata(
        body,
        db=db,
        user=user,
        request=request,
        lineage=lineage,
        payload=payload,
        request_fingerprint=request_fingerprint,
    )
    cost = estimate_generation_cost_from_snapshot(
        model.snapshot,
        body.category,
        body.stage,
        n_images,
        params=payload.task_params,
        source_type=payload.source_type,
    )
    return PreparedGeneration(
        client_request_id=request.client_request_id,
        existing_client_task=request.existing_client_task,
        parent=lineage.parent,
        prompt=payload.prompt,
        source_asset_url=payload.source_asset_url,
        source_type=payload.source_type,
        task_params=payload.task_params,
        model=model.model,
        model_use=model.model_use,
        snapshot=model.snapshot,
        n_images=n_images,
        request_fingerprint=request_fingerprint,
        cost=cost,
        request_snapshot=_generation_request_snapshot(
            body,
            request=request,
            lineage=lineage,
            payload=payload,
            model=model.model,
        ),
        project_id=lineage.project_id,
        recipe_attribution=lineage.recipe_attribution,
        reverse_operation_id=lineage.operation_id,
        reverse_revision_id=lineage.revision_id,
    )


def prepare_generation(
    body: GenerationRequestBase,
    *,
    db: Session,
    user: User,
    quote: GenerationQuote | None = None,
    fresh_route: bool = False,
    avoid_route_ids: set[int] | None = None,
    trusted_recipe_attribution: recipe_usage.RecipeAttribution | None = None,
) -> PreparedGeneration:
    return _prepare_generation_with_dependencies(
        body,
        db=db,
        user=user,
        quote=quote,
        fresh_route=fresh_route,
        avoid_route_ids=avoid_route_ids,
        trusted_recipe_attribution=trusted_recipe_attribution,
        dependencies=_DEFAULT_PREPARE_DEPENDENCIES,
    )
