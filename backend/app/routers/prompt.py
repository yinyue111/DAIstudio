"""Reverse prompt (optional). Vision model: image(s) -> structured words.

For an image asset we send the image straight to the vision model. For a video
asset (target=video) we sample a few keyframes from the source clip and send
them in temporal order so the model can reason about motion — falling back to a
supplied cover image when keyframe sampling isn't available.
"""
from __future__ import annotations

import base64
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..redis_client import redis_client
from ..schemas import ReverseIn, ReverseOut
from ..services import asset_refs, credits, gateway, usage, video_frames
from ..services.config_store import get_model_config, get_setting
from ..services.ssrf import SsrfError, assert_safe_user_asset_url

router = APIRouter(prefix="/api/prompt", tags=["prompt"])

_VIDEO_EXTS = (".mp4", ".webm", ".mov")
_UNSUPPORTED_VIDEO_EXTS = (".m3u8",)
_REVERSE_RATE_LIMIT = 30
_REVERSE_RATE_WINDOW = 3600


def _looks_like_video_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(_VIDEO_EXTS + _UNSUPPORTED_VIDEO_EXTS)


def _is_unsupported_video_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(_UNSUPPORTED_VIDEO_EXTS)


def _gateway_ref(db: Session, user: User, url: str | None) -> str | None:
    try:
        return asset_refs.gateway_ref_for_user_asset(db, user.id, url)
    except asset_refs.AssetRefError as e:
        raise HTTPException(404, str(e))


def _collect_refs(body: ReverseIn, db: Session, user: User) -> list[str]:
    """Resolve the asset into one or more image refs (URLs or base64 data-URIs)
    to feed the vision model. The image-target/video-url mismatch is rejected by
    the caller before this runs."""
    if not _looks_like_video_url(body.asset_url):
        ref = _gateway_ref(db, user, body.asset_url)
        return [ref] if ref else []

    # video + target=video: sample keyframes for motion-aware understanding
    if not settings.effective_mock_mode and video_frames.available():
        frames = video_frames.sample_keyframes(
            body.asset_url, n=settings.reverse_video_frames
        )
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
    n = redis_client.incr(key)
    if n == 1:
        redis_client.expire(key, _REVERSE_RATE_WINDOW)
    if n > _REVERSE_RATE_LIMIT:
        raise HTTPException(429, "反推过于频繁,请稍后再试")


@router.post("/reverse", response_model=ReverseOut)
def reverse(body: ReverseIn, db: Session = Depends(get_db),
            user: User = Depends(get_current_user)):
    _rate_limit(user.id)
    # SSRF: the asset/cover URL is sent to the vision gateway (and the video is
    # downloaded for keyframes) — reject internal/metadata targets up front.
    try:
        assert_safe_user_asset_url(body.asset_url)
        assert_safe_user_asset_url(body.fallback_image)
    except SsrfError as e:
        raise HTTPException(400, f"素材链接被安全策略拦截:{e}")
    # a video source only makes sense for video reverse; keep the clear hint
    if _looks_like_video_url(body.asset_url) and body.target != "video":
        raise HTTPException(400, "反推只支持图片素材;视频素材请使用封面图进行反推")
    if _is_unsupported_video_url(body.asset_url):
        raise HTTPException(400, "暂不支持 HLS/m3u8 视频反推,请使用 mp4/webm/mov 或封面图")
    # Honour the admin 反推开关 server-side, not just in the UI.
    if not get_setting(db, "reverse_prompt_enabled", True):
        raise HTTPException(403, "反推功能已被管理员关闭")
    model = get_model_config(db, "vision")
    if not model or not model.enabled:
        raise HTTPException(400, "未配置可用的视觉模型")

    unit_cost = max(0, int(model.cost_credits or 0))
    precharged = 0
    if _looks_like_video_url(body.asset_url) and body.target == "video":
        precharged = unit_cost * max(1, int(settings.reverse_video_frames or 1))
        if precharged:
            try:
                credits.consume(db, user.id, precharged, biz_type="reverse",
                                note=f"preauth target={body.target},refs={settings.reverse_video_frames}")
            except credits.InsufficientCredits as e:
                raise HTTPException(400, str(e))
    try:
        refs = _collect_refs(body, db, user)
    except Exception:
        if precharged:
            credits.refund_consumed(db, user.id, precharged, biz_type="reverse",
                                    note=f"preauth_refund target={body.target}")
        raise
    cost = unit_cost * max(1, len(refs))
    if precharged > cost:
        credits.refund_consumed(db, user.id, precharged - cost, biz_type="reverse",
                                note=f"preauth_adjust target={body.target},refs={len(refs)}")
    elif cost > precharged:
        try:
            credits.consume(db, user.id, cost - precharged, biz_type="reverse",
                            note=f"target={body.target},refs={len(refs)}")
        except credits.InsufficientCredits as e:
            if precharged:
                credits.refund_consumed(db, user.id, precharged, biz_type="reverse",
                                        note=f"preauth_refund target={body.target}")
            raise HTTPException(400, str(e))
    try:
        result = gateway.reverse_prompt(refs, model.model_id, target=body.target)
    except gateway.GatewayError as e:
        if cost:
            credits.refund_consumed(db, user.id, cost, biz_type="reverse",
                                    note=f"failed target={body.target},refs={len(refs)}")
        usage.record_call(db, kind="reverse", model_id=model.model_id,
                          user_id=user.id, status="failed",
                          detail={"target": body.target, "cost": cost,
                                  "error": str(e)[:200]})
        raise HTTPException(502, f"反推失败:{e}")
    except Exception as e:  # noqa: BLE001
        if cost:
            credits.refund_consumed(db, user.id, cost, biz_type="reverse",
                                    note=f"failed target={body.target},refs={len(refs)}")
        usage.record_call(db, kind="reverse", model_id=model.model_id,
                          user_id=user.id, status="failed",
                          detail={"target": body.target, "cost": cost,
                                  "error": str(e)[:200]})
        raise HTTPException(502, f"反推失败:{e}")

    # real-cost accounting: persist the provider's token usage for this call
    usage.record_call(db, kind="reverse", model_id=model.model_id, user_id=user.id,
                      status="ok", latency_ms=result.get("latency_ms"),
                      usage=result.get("usage"),
                      detail={"target": body.target, "frames": len(refs), "cost": cost})
    return ReverseOut(structured=result["structured"], final_text=result["final_text"])
