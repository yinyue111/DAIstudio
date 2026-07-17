"""Submit generation tasks. Freezes the estimated cost, enqueues the worker,
and refunds immediately if enqueue fails."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import get_client_ip, get_current_user
from ..models import GenAsset, GenTask, UploadedAsset, User
from ..schemas import GenerateIn, TaskOut
from ..services import asset_refs, generation, locks
from ..services.config_store import ModelConfigResolutionError, resolve_model_config
from ..services.content_safety import assert_text_allowed
from ..services.generation import (
    assert_model_snapshot_compatible,
    model_snapshot,
)
from ..services.generation_media import video_render_duration
from ..services.generation_prompts import compact_image_prompt_payload
from ..services.generation_request import (
    assert_client_request_replay,
    assert_product_detail_image_access,
    assert_product_reference_image_access,
    assert_reference_access,
    default_image_n,
    estimate_generation_cost_from_snapshot,
    validate_generation_params,
    validate_product_detail_images,
)
from ..services.generation_request import (
    request_fingerprint as build_request_fingerprint,
)
from ..services.generation_submit import submit_generation_task
from ..services.model_capabilities import ModelCapabilityError, assert_generation_capability
from ..services.rate_limit import incr_window
from ..services.ssrf import (
    SsrfError,
    assert_safe_user_asset_url,
    local_storage_key_from_user_asset_url,
)
from ..services.task_output import build_task_out
from ..services.video_prompt_compiler import (
    build_video_prompt_references,
    compile_video_prompt,
    store_video_prompt_compile,
)

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


def _validate_prompt_payload(prompt: dict, instruction: str | None) -> None:
    if instruction is not None and len(instruction) > int(settings.max_prompt_chars):
        raise HTTPException(400, "instruction 过长")
    if prompt:
        try:
            raw = json.dumps(prompt, ensure_ascii=False)
        except (TypeError, ValueError):
            raise HTTPException(400, "prompt 必须是可序列化 JSON")
        if len(raw) > int(settings.max_prompt_chars):
            raise HTTPException(400, "prompt 过长")
        for key in ("final_text", "instruction", "negative", "negative_prompt"):
            value = prompt.get(key)
            if isinstance(value, str) and len(value) > int(settings.max_prompt_chars):
                raise HTTPException(400, f"{key} 过长")


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


def _is_local_user_asset(db: Session, user_id: int, url: str | None) -> bool:
    key = local_storage_key_from_user_asset_url(url)
    if not key:
        return False
    row = db.get(UploadedAsset, key)
    if row and row.user_id == user_id:
        return True
    try:
        asset_refs.generated_asset_reference_path(db, user_id, key)
        return True
    except asset_refs.AssetRefError:
        return False


def _is_local_user_video_asset(db: Session, user_id: int, url: str | None) -> bool:
    key = local_storage_key_from_user_asset_url(url)
    if not key:
        return False
    try:
        asset_refs.generated_video_reference_path(db, user_id, key)
        return True
    except asset_refs.AssetRefError:
        return False


def _video_reference_is_actionable(
    db: Session,
    user_id: int,
    source_asset_url: str | None,
    source_type: str,
    prompt: dict,
    params: dict,
) -> bool:
    if source_type != "video":
        return True
    has_reverse_prompt = bool(prompt and (prompt.get("final_text") or len(prompt.keys()) > 1))
    has_local_first_frame = False
    for key in ("first_frame_image", "reference_image_url", "last_frame_image"):
        value = params.get(key)
        if not value:
            continue
        if _is_local_user_asset(db, user_id, value):
            has_local_first_frame = True
            return True
        try:
            assert_safe_user_asset_url(value)
            return True
        except SsrfError:
            continue
    if has_local_first_frame:
        return True
    # A local generated video can be sampled by the reverse flow, but generation
    # gateways currently need an image first frame; avoid pretending the raw video
    # URL itself will be used as model context.
    if _is_local_user_video_asset(db, user_id, source_asset_url) and has_reverse_prompt:
        return True
    return False


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


@router.post("/generate", response_model=TaskOut)
def generate(body: GenerateIn, request: Request,
             db: Session = Depends(get_db),
             user: User = Depends(get_current_user)):
    client_request_id = _normalize_client_request_id(body.client_request_id)
    existing_client_task = _existing_client_request_task(db, user.id, client_request_id)

    params = validate_generation_params(body.category, body.params)
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
    _validate_prompt_payload(
        validation_prompt,
        str(validation_instruction) if validation_instruction is not None else (
            str(request_instruction) if request_instruction is not None else None
        ),
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
    if body.category == "video":
        # Final renders inherit the preview task's params. Re-validate the
        # ordered detail-image contract instead of trusting historical rows.
        validate_product_detail_images(task_params)
    assert_text_allowed(
        db,
        prompt,
        task_params.get("negative_prompt"),
        task_params.get("negative"),
    )

    if body.category == "video" and body.stage == "final" and parent:
        existing_final = _existing_active_final(db, user.id, parent.id)
        if existing_final is not None:
            return build_task_out(db, existing_final)

    model_use = body.category  # image/video
    selected_model_config_id = body.model_config_id
    require_enabled = True
    if parent is not None and body.stage == "final":
        parent_model_config_id = getattr(parent, "model_config_id", None)
        if (
            selected_model_config_id is not None
            and parent_model_config_id is not None
            and int(selected_model_config_id) != int(parent_model_config_id)
        ):
            raise HTTPException(409, "视频最终生成必须使用预览任务绑定的同一模型")
        selected_model_config_id = parent_model_config_id or selected_model_config_id
        require_enabled = False
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
    try:
        assert_generation_capability(
            model,
            category=body.category,
            source_asset_url=source_asset_url,
            source_type=source_type,
            params=task_params,
        )
    except ModelCapabilityError as exc:
        raise HTTPException(400, str(exc)) from exc

    if body.stage == "final" and parent:
        inherited_snapshot = (dict(parent.params or {}).get("_model_snapshot") or {})
        if inherited_snapshot:
            try:
                assert_model_snapshot_compatible(model, inherited_snapshot)
            except generation.ModelSnapshotMismatchError as e:
                raise HTTPException(409, str(e)) from e
        snapshot = inherited_snapshot or model_snapshot(model)
    else:
        snapshot = model_snapshot(model)

    # SSRF: validate every URL that may be forwarded to us / the gateway. This
    # runs for ALL stages — including `final`, which inherits its preview's
    # params and could carry an unsafe URL written before this guard existed.
    try:
        assert_safe_user_asset_url(source_asset_url)
        for _url_key in (
            "reference_image_url",
            "product_reference_image",
            "first_frame_image",
            "last_frame_image",
            "style_reference_image",
            "character_reference_image",
            "mask_image_url",
        ):
            assert_safe_user_asset_url(task_params.get(_url_key))
        for _detail_url in task_params.get("product_detail_images") or []:
            assert_safe_user_asset_url(_detail_url)
    except SsrfError as e:
        raise HTTPException(400, f"素材链接被安全策略拦截:{e}")
    assert_reference_access(
        db,
        user.id,
        source_asset_url,
        task_params.get("reference_image_url"),
        task_params.get("product_reference_image"),
        task_params.get("first_frame_image"),
        task_params.get("last_frame_image"),
        task_params.get("style_reference_image"),
        task_params.get("character_reference_image"),
        task_params.get("mask_image_url"),
    )
    assert_product_detail_image_access(
        db,
        user.id,
        task_params.get("product_detail_images") or [],
    )
    assert_product_reference_image_access(
        db,
        user.id,
        task_params.get("product_reference_image"),
    )
    if body.category == "video" and parent is None and not _video_reference_is_actionable(
        db,
        user.id,
        source_asset_url,
        source_type,
        prompt,
        task_params,
    ):
        raise HTTPException(400, "视频参考缺少可用封面或反推提示词,请先反推视频或选择带封面的素材")

    if body.category == "video":
        references = build_video_prompt_references(
            source_asset_url=source_asset_url,
            source_type=source_type,
            params=task_params,
        )
        snapshot_extra = snapshot.get("extra") if isinstance(snapshot.get("extra"), dict) else {}
        model_profiles = (
            snapshot_extra.get("video_prompt_profiles")
            or snapshot_extra.get("prompt_profiles")
        )
        if not isinstance(model_profiles, dict):
            model_profiles = None
        compiled = compile_video_prompt(
            prompt,
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

    n_images = int(task_params.get("n") or default_image_n(db)) \
        if body.category == "image" else 1
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
    )
    if existing_client_task is not None:
        assert_client_request_replay(existing_client_task, request_fingerprint)
        return build_task_out(db, existing_client_task)
    _rate_limit(user.id)
    if body.stage != "final" or parent is None:
        task_params["_source_trace"] = _source_trace(
            source_asset_url=source_asset_url,
            source_type=source_type,
            meta=body.source_asset_meta,
        )
    elif "_source_trace" not in task_params and parent is not None:
        parent_trace = (parent.params or {}).get("_source_trace")
        if isinstance(parent_trace, dict):
            task_params["_source_trace"] = parent_trace
    task_params["_model_snapshot"] = snapshot
    task_params["_client_request_fingerprint"] = request_fingerprint
    cost = estimate_generation_cost_from_snapshot(
        snapshot,
        body.category,
        body.stage,
        n_images,
        params=task_params,
        source_type=source_type,
    )

    def replay_conflicting_task() -> TaskOut | None:
        existing = _existing_client_request_task(db, user.id, client_request_id)
        if existing is not None:
            assert_client_request_replay(existing, request_fingerprint)
            return build_task_out(db, existing)
        if body.category == "video" and body.stage == "final" and parent is not None:
            active = _existing_active_final(db, user.id, parent.id)
            if active is not None:
                return build_task_out(db, active)
        return None

    def create_task() -> TaskOut:
        return submit_generation_task(
            db,
            user_id=user.id,
            source_asset_url=source_asset_url,
            source_type=source_type,
            category=body.category,
            stage=body.stage,
            prompt=prompt,
            model_use=model_use,
            model_config_id=int(model.id),
            params=task_params,
            client_request_id=client_request_id,
            cost=cost,
            parent_task_id=body.parent_task_id,
            ip=get_client_ip(request),
            replay_conflicting_task=replay_conflicting_task,
        )

    if body.category == "video" and body.stage == "final" and parent:
        lock_key = f"gen:final:create:{user.id}:{parent.id}"
        lock_token = locks.acquire(lock_key, ttl=60)
        if not lock_token:
            active = _existing_active_final(db, user.id, parent.id)
            if active:
                return build_task_out(db, active)
            raise HTTPException(409, "高清视频任务正在创建,请稍后刷新")
        try:
            active = _existing_active_final(db, user.id, parent.id)
            if active:
                return build_task_out(db, active)
            return create_task()
        finally:
            locks.release(lock_key, lock_token)

    return create_task()
