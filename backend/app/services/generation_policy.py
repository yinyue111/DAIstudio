"""Read-only generation policy enforced before quoting, submit, and retry."""
from __future__ import annotations

import json

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..config import settings
from ..models import UploadedAsset
from . import asset_refs
from .content_safety import assert_text_allowed
from .generation_prompts import compact_image_prompt_payload
from .generation_request import (
    assert_product_detail_image_access,
    assert_product_reference_image_access,
    assert_reference_access,
    validate_product_detail_images,
)
from .model_capabilities import ModelCapabilityError, assert_generation_capability
from .ssrf import (
    SsrfError,
    assert_safe_user_asset_url,
    local_storage_key_from_user_asset_url,
)

REFERENCE_URL_KEYS = (
    "reference_image_url",
    "product_reference_image",
    "first_frame_image",
    "last_frame_image",
    "style_reference_image",
    "character_reference_image",
    "mask_image_url",
)


def validate_prompt_payload(prompt: dict, instruction: str | None = None) -> None:
    """Apply the same bounded JSON contract to every executable prompt."""
    if instruction is not None and len(instruction) > int(settings.max_prompt_chars):
        raise HTTPException(400, "instruction 过长")
    if not prompt:
        return
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


def video_reference_is_actionable(
    db: Session,
    user_id: int,
    source_asset_url: str | None,
    source_type: str | None,
    prompt: dict,
    params: dict,
) -> bool:
    """Return whether a video source can actually reach a generation gateway."""
    if source_type != "video":
        return True
    has_reverse_prompt = bool(prompt and (prompt.get("final_text") or len(prompt) > 1))
    for key in ("first_frame_image", "reference_image_url", "last_frame_image"):
        value = params.get(key)
        if not value:
            continue
        if _is_local_user_asset(db, user_id, value):
            return True
        try:
            assert_safe_user_asset_url(value)
            return True
        except SsrfError:
            continue
    # Raw local video URLs are actionable only after reverse analysis supplied
    # a usable prompt; gateways otherwise require an image frame reference.
    return _is_local_user_video_asset(db, user_id, source_asset_url) and has_reverse_prompt


def assert_generation_request_policy(
    db: Session,
    *,
    user_id: int,
    model,
    category: str,
    source_asset_url: str | None,
    source_type: str | None,
    prompt: dict,
    params: dict,
    require_actionable_video_reference: bool = True,
    analysis_only_source_video: bool = False,
) -> None:
    """Enforce the current, side-effect-free policy for one generation request.

    Callers must invoke this before quote creation, task state changes, credit
    freezes, or queue publication. Keeping these gates here prevents retry and
    other submission paths from drifting away from the primary generate route.
    """
    if not isinstance(prompt, dict):
        raise HTTPException(400, "prompt 必须是 JSON 对象")
    if not prompt:
        raise HTTPException(400, "请提供 prompt 或 instruction")
    policy_params = dict(params or {})
    if category == "video":
        validate_product_detail_images(policy_params)

    validation_prompt = (
        compact_image_prompt_payload(prompt, policy_params)
        if category == "image"
        else prompt
    )
    validation_instruction = validation_prompt.get("instruction")
    validate_prompt_payload(
        validation_prompt,
        str(validation_instruction) if validation_instruction is not None else None,
    )
    assert_text_allowed(
        db,
        prompt,
        policy_params.get("negative_prompt"),
        policy_params.get("negative"),
    )

    reference_urls = [policy_params.get(key) for key in REFERENCE_URL_KEYS]
    product_detail_urls = policy_params.get("product_detail_images") or []
    try:
        assert_safe_user_asset_url(source_asset_url)
        for url in (*reference_urls, *product_detail_urls):
            assert_safe_user_asset_url(url)
    except SsrfError as exc:
        raise HTTPException(400, f"素材链接被安全策略拦截:{exc}") from exc

    assert_reference_access(db, user_id, source_asset_url, *reference_urls)
    assert_product_detail_image_access(db, user_id, product_detail_urls)
    assert_product_reference_image_access(
        db,
        user_id,
        policy_params.get("product_reference_image"),
    )

    try:
        assert_generation_capability(
            model,
            category=category,
            # A video already consumed by the reverse pipeline is provenance,
            # not a provider video-to-video input. Capability checks must
            # therefore evaluate the executable text/image request only.
            source_asset_url=None if analysis_only_source_video else source_asset_url,
            source_type=None if analysis_only_source_video else source_type,
            params=policy_params,
        )
    except ModelCapabilityError as exc:
        raise HTTPException(400, str(exc)) from exc

    if (
        category == "video"
        and require_actionable_video_reference
        and not analysis_only_source_video
        and not video_reference_is_actionable(
            db,
            user_id,
            source_asset_url,
            source_type,
            prompt,
            policy_params,
        )
    ):
        raise HTTPException(
            400,
            "视频参考缺少可用封面或反推提示词,请先反推视频或选择带封面的素材",
        )


def assert_retry_submission_state_known(params: dict | None) -> None:
    """Prevent a user retry from duplicating a possibly accepted upstream job."""
    values = params if isinstance(params, dict) else {}
    if values.get("_video_submit_state_unknown") or values.get("_image_submit_state_unknown"):
        raise HTTPException(
            409,
            "上游提交状态未知,不能自动重试;请先完成任务对账或人工确认未生成",
        )
