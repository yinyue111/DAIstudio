"""Reverse prompt (optional). Vision model: image(s) -> structured words.

For an image asset we send the image straight to the vision model. For a video
asset (target=video) we sample a few keyframes from the source clip and send
them in temporal order so the model can reason about motion — falling back to a
supplied cover image when keyframe sampling isn't available.
"""
from __future__ import annotations

import base64
import inspect
import secrets
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..schemas import ReverseIn, ReverseOut
from ..services import asset_refs, credits, gateway, storage, usage, video_frames
from ..services.config_store import get_model_config, get_setting
from ..services.content_safety import assert_text_allowed
from ..services.generation_pricing import reverse_cost
from ..services.model_gateway_config import runtime_config_for_model
from ..services.rate_limit import incr_window
from ..services.ssrf import SsrfError, assert_safe_user_asset_url
from ..services.video_analysis import (
    frame_count_for_duration,
    max_frame_count,
    normalize_video_analysis_preset,
)

router = APIRouter(prefix="/api/prompt", tags=["prompt"])

_VIDEO_EXTS = (".mp4", ".webm", ".mov")
_UNSUPPORTED_VIDEO_EXTS = (".m3u8",)
_REVERSE_RATE_LIMIT = 30
_REVERSE_RATE_WINDOW = 3600
REVERSE_IMAGE_REFERENCE_MAX_SIDE = 1024
REVERSE_IMAGE_REFERENCE_QUALITY = 92
_assert_text_allowed = assert_text_allowed


def _looks_like_video_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(_VIDEO_EXTS + _UNSUPPORTED_VIDEO_EXTS)


def _is_unsupported_video_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(_UNSUPPORTED_VIDEO_EXTS)


def _is_video_source(body: ReverseIn) -> bool:
    return body.source_type == "video" or _looks_like_video_url(body.asset_url)


def _gateway_ref(db: Session, user: User, url: str | None) -> str | None:
    try:
        return asset_refs.gateway_ref_for_user_asset(
            db,
            user.id,
            url,
            max_side=REVERSE_IMAGE_REFERENCE_MAX_SIDE,
            prefer_original_upload=True,
            quality=REVERSE_IMAGE_REFERENCE_QUALITY,
            subsampling=0,
        )
    except asset_refs.AssetRefError as e:
        raise HTTPException(404, str(e))


def _collect_refs(
    body: ReverseIn,
    db: Session,
    user: User,
    *,
    frame_budget: int | None = None,
    video_preset: str | None = None,
) -> list[str]:
    """Resolve the asset into one or more image refs (URLs or base64 data-URIs)
    to feed the vision model. The image-target/video-url mismatch is rejected by
    the caller before this runs."""
    if not _is_video_source(body):
        ref = _gateway_ref(db, user, body.asset_url)
        return [ref] if ref else []

    key = storage.key_from_url(body.asset_url)
    local_video_path = None
    if key:
        try:
            local_video_path = asset_refs.generated_video_reference_path(db, user.id, key)
        except asset_refs.AssetRefError as e:
            raise HTTPException(404, str(e))

    # video + target=video: sample keyframes for motion-aware understanding
    if not settings.effective_mock_mode and video_frames.available():
        n_frames = max(1, int(frame_budget or settings.reverse_video_frames or 1))
        try:
            if local_video_path is not None:
                frames = video_frames.sample_keyframes_from_path(
                    str(local_video_path),
                    n=n_frames,
                    preset=video_preset,
                )
            else:
                frames = video_frames.sample_keyframes(
                    body.asset_url,
                    n=n_frames,
                    preset=video_preset,
                )
        except asset_refs.AssetRefError as e:
            raise HTTPException(404, str(e))
        if frames:
            return ["data:image/jpeg;base64," + base64.b64encode(f).decode()
                    for f in frames]

    # fall back to a provided cover/keyframe image
    cover = body.fallback_image
    if cover and not _looks_like_video_url(cover):
        ref = _gateway_ref(db, user, cover)
        return [ref] if ref else []
    if settings.effective_mock_mode:
        return [cover or body.asset_url]
    raise HTTPException(400, "无法从该视频抽取关键帧,请改用封面图进行反推")


def _rate_limit(user_id: int) -> None:
    key = f"reverse:rate:{user_id}"
    n = incr_window(key, _REVERSE_RATE_WINDOW)
    if n > _REVERSE_RATE_LIMIT:
        raise HTTPException(429, "反推过于频繁,请稍后再试")


def _reverse_biz_ref() -> int:
    return secrets.randbits(63)


def _video_duration_for_reverse(body: ReverseIn, db: Session, user: User) -> float | None:
    if not _is_video_source(body):
        return None
    key = storage.key_from_url(body.asset_url)
    if not key:
        return None
    try:
        path = asset_refs.generated_video_reference_path(db, user.id, key)
    except asset_refs.AssetRefError:
        return None
    meta = video_frames.probe_media(str(path))
    duration = meta.get("duration")
    return float(duration) if duration else None


def _accepts_gateway_config(fn) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    return "gateway_config" in sig.parameters or any(
        p.kind == inspect.Parameter.VAR_KEYWORD
        for p in sig.parameters.values()
    )


@router.post("/reverse", response_model=ReverseOut)
def reverse(body: ReverseIn, db: Session = Depends(get_db),
            user: User = Depends(get_current_user)):
    _rate_limit(user.id)
    _assert_text_allowed(db, body.asset_url, body.fallback_image)
    # SSRF: the asset/cover URL is sent to the vision gateway (and the video is
    # downloaded for keyframes) — reject internal/metadata targets up front.
    try:
        assert_safe_user_asset_url(body.asset_url)
        assert_safe_user_asset_url(body.fallback_image)
    except SsrfError as e:
        raise HTTPException(400, f"素材链接被安全策略拦截:{e}")
    # a video source only makes sense for video reverse; keep the clear hint
    is_video_source = _is_video_source(body)
    if is_video_source and body.target != "video":
        raise HTTPException(400, "反推只支持图片素材;视频素材请使用封面图进行反推")
    if _is_unsupported_video_url(body.asset_url):
        raise HTTPException(400, "暂不支持 HLS/m3u8 视频反推,请使用 mp4/webm/mov 或封面图")
    # Honour the admin 反推开关 server-side, not just in the UI.
    if not get_setting(db, "reverse_prompt_enabled", True):
        raise HTTPException(403, "反推功能已被管理员关闭")
    model = get_model_config(db, "vision")
    if not model or not model.enabled:
        raise HTTPException(400, "未配置可用的视觉模型")

    precharged = 0
    biz_ref = _reverse_biz_ref()
    video_preset = normalize_video_analysis_preset(body.video_analysis_preset)
    expected_cost = reverse_cost(body.target, preset=video_preset)
    requested_frame_budget = 1
    if is_video_source and body.target == "video":
        duration = _video_duration_for_reverse(body, db, user)
        requested_frame_budget = frame_count_for_duration(duration, video_preset)
        precharge_frames = max(max_frame_count(video_preset), requested_frame_budget)
        precharged = expected_cost
        if precharged:
            try:
                credits.consume(db, user.id, precharged, biz_type="reverse",
                                biz_ref=biz_ref,
                                note=f"preauth target={body.target},preset={video_preset},max_refs={precharge_frames}")
            except credits.InsufficientCredits as e:
                raise HTTPException(400, str(e))
    try:
        refs = _collect_refs(
            body,
            db,
            user,
            frame_budget=requested_frame_budget,
            video_preset=video_preset,
        )
    except Exception:
        if precharged:
            credits.refund_consumed(db, user.id, precharged, biz_type="reverse",
                                    biz_ref=biz_ref,
                                    note=f"preauth_refund target={body.target}")
        raise
    cost = expected_cost
    if precharged > cost:
        credits.refund_consumed(db, user.id, precharged - cost, biz_type="reverse",
                                biz_ref=biz_ref,
                                note=f"preauth_adjust target={body.target},refs={len(refs)}")
    elif cost > precharged:
        try:
            credits.consume(db, user.id, cost - precharged, biz_type="reverse",
                            biz_ref=biz_ref,
                            note=f"target={body.target},preset={video_preset},refs={len(refs)}")
        except credits.InsufficientCredits as e:
            if precharged:
                credits.refund_consumed(db, user.id, precharged, biz_type="reverse",
                                        biz_ref=biz_ref,
                                        note=f"preauth_refund target={body.target}")
            raise HTTPException(400, str(e))
    try:
        kwargs = {"target": body.target}
        if _accepts_gateway_config(gateway.reverse_prompt):
            kwargs["gateway_config"] = runtime_config_for_model(model, "vision")
        result = gateway.reverse_prompt(refs, model.model_id, **kwargs)
    except HTTPException:
        raise
    except gateway.GatewayError as e:
        if cost:
            credits.refund_consumed(db, user.id, cost, biz_type="reverse",
                                    biz_ref=biz_ref,
                                    note=f"failed target={body.target},refs={len(refs)}")
        usage.record_call(db, kind="reverse", model_id=model.model_id,
                          user_id=user.id, status="failed",
                          detail={"target": body.target, "cost": cost,
                                  "preset": video_preset,
                                  "error": str(e)[:200]})
        message = "反推失败:上游视觉模型调用失败,请稍后重试"
        if "timed out" in str(e).lower() or "timeout" in str(e).lower():
            message = "反推失败:视觉模型响应超时,请先切换到快速/标准分析后重试"
        raise HTTPException(502, message)
    except Exception as e:  # noqa: BLE001
        if cost:
            credits.refund_consumed(db, user.id, cost, biz_type="reverse",
                                    biz_ref=biz_ref,
                                    note=f"failed target={body.target},refs={len(refs)}")
        usage.record_call(db, kind="reverse", model_id=model.model_id,
                          user_id=user.id, status="failed",
                          detail={"target": body.target, "cost": cost,
                                  "preset": video_preset,
                                  "error": str(e)[:200]})
        raise HTTPException(502, "反推失败:服务暂时不可用,请稍后重试")

    # real-cost accounting: persist the provider's token usage for this call
    usage.record_call(db, kind="reverse", model_id=model.model_id, user_id=user.id,
                      status="ok", latency_ms=result.get("latency_ms"),
                      usage=result.get("usage"),
                      detail={"target": body.target, "preset": video_preset,
                              "frames": len(refs), "cost": cost})
    return ReverseOut(
        structured=result["structured"],
        final_text=result["final_text"],
        charged_credits=cost,
        reference_count=max(1, len(refs)),
    )
