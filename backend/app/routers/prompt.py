"""Reverse prompt (optional). Vision model: image(s) -> structured words.

For an image asset we send the image straight to the vision model. For a video
asset (target=video) we sample a few keyframes from the source clip and send
them in temporal order so the model can reason about motion — falling back to a
supplied cover image when keyframe sampling isn't available.
"""
from __future__ import annotations

import base64
import hashlib
import inspect
import json
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import get_current_user
from ..models import ReverseOperation, User
from ..schemas import PromptOptimizeIn, PromptOptimizeOut, ReverseIn, ReverseOut
from ..services import asset_refs, credits, gateway, storage, usage, video_frames
from ..services.config_store import get_model_config, get_setting
from ..services.content_safety import assert_text_allowed
from ..services.generation_pricing import reverse_cost
from ..services.model_gateway_config import runtime_config_for_model
from ..services.prompt_history import remember_prompt
from ..services.rate_limit import incr_window
from ..services.ssrf import SsrfError, assert_safe_user_asset_url
from ..services.video_analysis import (
    frame_count_for_duration,
    max_frame_count,
    normalize_video_analysis_preset,
)

router = APIRouter(prefix="/api/prompt", tags=["prompt"])
log = logging.getLogger("prompt")

_VIDEO_EXTS = (".mp4", ".webm", ".mov")
_UNSUPPORTED_VIDEO_EXTS = (".m3u8",)
_REVERSE_RATE_LIMIT = 30
_REVERSE_RATE_WINDOW = 3600
_OPTIMIZE_RATE_LIMIT = 60
REVERSE_IMAGE_REFERENCE_MAX_SIDE = 1024
REVERSE_IMAGE_REFERENCE_QUALITY = 92
_assert_text_allowed = assert_text_allowed


class ReverseOperationClosed(Exception):
    """The request lost ownership because recovery already finalized it."""


@router.post("/optimize", response_model=PromptOptimizeOut)
def optimize_prompt_text(
    body: PromptOptimizeIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    source = body.prompt.strip()
    _assert_text_allowed(db, source)
    count = incr_window(f"prompt_optimize:{user.id}", _REVERSE_RATE_WINDOW)
    if count > _OPTIMIZE_RATE_LIMIT:
        raise HTTPException(429, "提示词优化过于频繁，请稍后再试")
    model = get_model_config(db, "prompt")
    if model is None or not model.enabled:
        raise HTTPException(503, "提示词优化模型未启用")
    target_model_id = body.target_model_id
    target_model_provider = body.target_model_provider
    target_model_extra = None
    if body.category == "video" and (not target_model_id or not target_model_provider):
        video_model = get_model_config(db, "video")
        if video_model is not None:
            target_model_id = target_model_id or video_model.model_id
            target_model_provider = (
                target_model_provider
                or video_model.provider
                or runtime_config_for_model(video_model, "video").provider
            )
    elif body.category == "video":
        video_model = get_model_config(db, "video")
    else:
        video_model = None
    if (
        video_model is not None
        and target_model_id == video_model.model_id
        and isinstance(video_model.extra, dict)
    ):
        target_model_extra = dict(video_model.extra)
    cost = max(0, int(model.cost_credits or 0))
    if cost:
        try:
            credits.consume(
                db,
                user.id,
                cost,
                biz_type="prompt_optimize",
                note=f"model={model.model_id}",
            )
        except credits.InsufficientCredits as e:
            raise HTTPException(400, str(e)) from e
    try:
        result = gateway.optimize_prompt(
            source,
            model.model_id,
            category=body.category,
            product_mode=body.product_mode,
            duration=body.duration,
            subject_mode=body.subject_mode,
            reference_type=body.reference_type,
            subject_profile=body.subject_profile,
            target_model_id=target_model_id,
            target_model_provider=target_model_provider,
            aspect_ratio=body.aspect_ratio,
            resolution=body.resolution,
            product_lock_mode=body.product_lock_mode,
            product_video_template=body.product_video_template,
            target_model_extra=target_model_extra,
            gateway_config=runtime_config_for_model(model, "prompt"),
        )
        optimized = str(result.get("prompt") or "").strip()
        if not optimized:
            raise gateway.GatewayError("提示词优化模型返回了空结果")
        _assert_text_allowed(db, optimized)
    except Exception as e:
        usage.record_call(
            db,
            kind="prompt_optimize",
            model_id=model.model_id,
            user_id=user.id,
            status="failed",
            detail={
                "category": body.category,
                "product_mode": body.product_mode,
                "error": str(e)[:300],
            },
        )
        if cost:
            credits.refund_consumed(
                db,
                user.id,
                cost,
                biz_type="prompt_optimize",
                note=f"failed model={model.model_id}",
            )
        if isinstance(e, gateway.GatewayError):
            raise HTTPException(502, f"提示词优化失败：{e}") from e
        raise
    usage.record_call(
        db,
        kind="prompt_optimize",
        model_id=model.model_id,
        user_id=user.id,
        status="ok",
        latency_ms=result.get("latency_ms"),
        usage=result.get("usage"),
        detail={"category": body.category, "product_mode": body.product_mode},
    )
    return PromptOptimizeOut(
        prompt=optimized,
        model_id=model.model_id,
        optimizer_model_id=model.model_id,
        compiler_metadata=result.get("compiler_metadata"),
        context_metadata=result.get("context_metadata"),
    )


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
    gateway_mock: bool | None = None,
) -> tuple[list[str], dict | None]:
    """Resolve the asset into one or more image refs (URLs or base64 data-URIs)
    to feed the vision model. The image-target/video-url mismatch is rejected by
    the caller before this runs."""
    if not _is_video_source(body):
        ref = _gateway_ref(db, user, body.asset_url)
        return ([ref] if ref else []), None

    key = storage.key_from_url(body.asset_url)
    local_video_path = None
    sample = None
    if key:
        try:
            local_video_path = asset_refs.generated_video_reference_path(db, user.id, key)
        except asset_refs.AssetRefError as e:
            raise HTTPException(404, str(e))

    # video + target=video: sample keyframes for motion-aware understanding
    effective_gateway_mock = settings.effective_mock_mode if gateway_mock is None else gateway_mock
    if not effective_gateway_mock and video_frames.available():
        n_frames = max(1, int(frame_budget or settings.reverse_video_frames or 1))
        try:
            if local_video_path is not None:
                sample = video_frames.sample_video_from_path(
                    str(local_video_path),
                    n=n_frames,
                    preset=video_preset,
                )
            else:
                sample = video_frames.sample_video(
                    body.asset_url,
                    n=n_frames,
                    preset=video_preset,
                )
        except asset_refs.AssetRefError as e:
            raise HTTPException(404, str(e))
        except Exception as e:  # noqa: BLE001
            log.warning("video keyframe sampling failed, using cover fallback: %s", e)
            sample = None
        if sample and sample.frames:
            analysis = sample.analysis()
            analysis["analysis_mode"] = "keyframes"
            return (
                [
                    "data:image/jpeg;base64," + base64.b64encode(frame.jpeg).decode()
                    for frame in sample.frames
                ],
                analysis,
            )

    # fall back to a provided cover/keyframe image
    cover = body.fallback_image
    if cover and not _looks_like_video_url(cover):
        ref = _gateway_ref(db, user, cover)
        if sample:
            analysis = sample.analysis()
        else:
            source = video_frames.VideoMetadata()
            if local_video_path is not None:
                probed = video_frames.probe_media(str(local_video_path))
                source = video_frames.VideoMetadata(
                    width=probed.get("width"),
                    height=probed.get("height"),
                    duration_seconds=(
                        probed.get("duration_seconds") or probed.get("duration")
                    ),
                    fps=probed.get("fps"),
                    has_audio=bool(probed.get("has_audio")),
                )
            analysis = {"source": source.to_dict(), "sampled_frames": []}
        analysis.update({
            "analysis_mode": "cover_fallback",
            "degraded_reason": "未能从源视频抽取可用关键帧，已降级为封面单帧分析",
        })
        return ([ref] if ref else []), analysis
    if effective_gateway_mock:
        return [cover or body.asset_url], None
    raise HTTPException(400, "无法从该视频抽取关键帧,请改用封面图进行反推")


def _rate_limit(user_id: int) -> None:
    key = f"reverse:rate:{user_id}"
    n = incr_window(key, _REVERSE_RATE_WINDOW)
    if n > _REVERSE_RATE_LIMIT:
        raise HTTPException(429, "反推过于频繁,请稍后再试")


def _reverse_request_fingerprint(body: ReverseIn) -> str:
    payload = {
        "asset_url": body.asset_url,
        "target": body.target,
        "source_type": body.source_type,
        "video_analysis_preset": body.video_analysis_preset,
        "fallback_image": body.fallback_image,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _reverse_operation_response(op: ReverseOperation) -> ReverseOut:
    result = op.result if isinstance(op.result, dict) else {}
    structured = result.get("structured") if isinstance(result.get("structured"), dict) else {}
    final_text = str(result.get("final_text") or "")
    if not final_text:
        raise HTTPException(409, "该反推请求结果不完整,请重新发起")
    return ReverseOut(
        structured=structured,
        final_text=final_text,
        charged_credits=int(op.charged_credits or 0),
        reference_count=max(1, int(op.reference_count or 1)),
        video_analysis=(
            result.get("video_analysis")
            if isinstance(result.get("video_analysis"), dict) else None
        ),
    )


def _existing_reverse_operation_replay(
    db: Session,
    *,
    user_id: int,
    body: ReverseIn,
) -> ReverseOut | None:
    raw_key = (body.client_request_id or "").strip()
    if not raw_key:
        return None
    fingerprint = _reverse_request_fingerprint(body)
    existing = db.execute(
        select(ReverseOperation).where(
            ReverseOperation.user_id == user_id,
            ReverseOperation.client_request_id == raw_key,
        )
    ).scalar_one_or_none()
    if existing is None:
        return None
    if existing.request_fingerprint != fingerprint:
        raise HTTPException(409, "client_request_id 已用于不同反推请求")
    if existing.status == "succeeded":
        return _reverse_operation_response(existing)
    if existing.status == "running":
        raise HTTPException(409, "反推请求仍在处理中,请稍后重试")
    raise HTTPException(409, "该反推请求已失败,请重新发起")


def _reserve_reverse_operation(
    db: Session,
    *,
    user_id: int,
    body: ReverseIn,
) -> tuple[ReverseOperation, ReverseOut | None]:
    raw_key = (body.client_request_id or "").strip() or None
    fingerprint = _reverse_request_fingerprint(body)
    op = ReverseOperation(
        user_id=user_id,
        client_request_id=raw_key,
        request_fingerprint=fingerprint,
        target=body.target,
        asset_url=body.asset_url,
        status="running",
    )
    try:
        db.add(op)
        db.commit()
        db.refresh(op)
        return op, None
    except IntegrityError as e:
        db.rollback()
        if raw_key is None:
            raise
        existing = db.execute(
            select(ReverseOperation).where(
                ReverseOperation.user_id == user_id,
                ReverseOperation.client_request_id == raw_key,
            )
        ).scalar_one_or_none()
        if not existing:
            raise HTTPException(409, "重复的反推请求已拦截,请更换请求 ID") from e
        if existing.request_fingerprint != fingerprint:
            raise HTTPException(409, "client_request_id 已用于不同反推请求") from e
        if existing.status == "succeeded":
            return existing, _reverse_operation_response(existing)
        if existing.status == "running":
            raise HTTPException(409, "反推请求仍在处理中,请稍后重试") from e
        raise HTTPException(409, "该反推请求已失败,请重新发起") from e


def _running_reverse_operation_for_update(
    db: Session,
    operation_id: int,
) -> ReverseOperation:
    operation = db.execute(
        select(ReverseOperation)
        .where(
            ReverseOperation.id == operation_id,
            ReverseOperation.status == "running",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if operation is None:
        db.rollback()
        raise ReverseOperationClosed
    return operation


def _charge_reverse_operation(
    db: Session,
    operation_id: int,
    amount: int,
    *,
    note: str,
) -> None:
    if amount <= 0:
        return
    operation = _running_reverse_operation_for_update(db, operation_id)
    operation.charged_credits = int(operation.charged_credits or 0) + amount
    operation.updated_at = datetime.now(timezone.utc)
    # Establish operation-row ownership before credits.consume locks the user.
    db.flush()
    credits.consume(
        db,
        operation.user_id,
        amount,
        biz_type="reverse",
        biz_ref=operation.id,
        note=note,
        commit=False,
    )
    db.commit()


def _refund_reverse_operation_charge(
    db: Session,
    operation_id: int,
    amount: int,
    *,
    note: str,
) -> None:
    if amount <= 0:
        return
    operation = _running_reverse_operation_for_update(db, operation_id)
    charged = int(operation.charged_credits or 0)
    if amount > charged:
        db.rollback()
        raise credits.InsufficientCredits(
            f"反推扣费记录不足:需要退回 {amount},当前已扣 {charged}"
        )
    operation.charged_credits = charged - amount
    operation.updated_at = datetime.now(timezone.utc)
    db.flush()
    credits.refund_consumed(
        db,
        operation.user_id,
        amount,
        biz_type="reverse",
        biz_ref=operation.id,
        note=note,
        commit=False,
    )
    db.commit()


def _touch_reverse_operation(
    db: Session,
    operation_id: int,
    *,
    charged_credits: int,
) -> bool:
    claimed = db.execute(
        update(ReverseOperation)
        .where(
            ReverseOperation.id == operation_id,
            ReverseOperation.status == "running",
            ReverseOperation.charged_credits == int(charged_credits or 0),
        )
        .values(updated_at=datetime.now(timezone.utc))
        .returning(ReverseOperation.id)
        .execution_options(synchronize_session=False)
    ).first()
    if claimed is None:
        db.rollback()
        return False
    db.commit()
    return True


def _finish_reverse_operation(
    db: Session,
    operation_id: int,
    *,
    result: dict,
    charged_credits: int,
    reference_count: int,
) -> bool:
    claimed = db.execute(
        update(ReverseOperation)
        .where(
            ReverseOperation.id == operation_id,
            ReverseOperation.status == "running",
            ReverseOperation.charged_credits == int(charged_credits or 0),
        )
        .values(
            status="succeeded",
            result={
                "structured": result.get("structured") or {},
                "final_text": result.get("final_text") or "",
                **(
                    {"video_analysis": result["video_analysis"]}
                    if isinstance(result.get("video_analysis"), dict) else {}
                ),
            },
            reference_count=max(1, int(reference_count or 1)),
            error=None,
            updated_at=datetime.now(timezone.utc),
        )
        .returning(ReverseOperation.id)
        .execution_options(synchronize_session=False)
    ).first()
    if claimed is None:
        db.rollback()
        return False
    db.commit()
    return True


def _fail_reverse_operation(
    db: Session,
    operation_id: int,
    error: str,
    *,
    refund_note: str,
) -> bool:
    try:
        claimed = db.execute(
            update(ReverseOperation)
            .where(
                ReverseOperation.id == operation_id,
                ReverseOperation.status == "running",
            )
            .values(
                status="failed",
                error=str(error or "反推失败")[:1000],
                updated_at=datetime.now(timezone.utc),
            )
            .returning(
                ReverseOperation.user_id,
                ReverseOperation.charged_credits,
            )
            .execution_options(synchronize_session=False)
        ).first()
        if claimed is None:
            db.rollback()
            return False
        user_id, charged_credits = claimed
        charged = int(charged_credits or 0)
        if charged:
            db.execute(
                update(ReverseOperation)
                .where(ReverseOperation.id == operation_id)
                .values(charged_credits=0)
                .execution_options(synchronize_session=False)
            )
            credits.refund_consumed(
                db,
                int(user_id),
                charged,
                biz_type="reverse",
                biz_ref=operation_id,
                note=refund_note,
                commit=False,
            )
        db.commit()
        return True
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("failed to fail and refund reverse operation %s", operation_id)
        return False


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


def _accepts_keyword(fn, name: str) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    return name in sig.parameters or any(
        p.kind == inspect.Parameter.VAR_KEYWORD
        for p in sig.parameters.values()
    )


def _accepts_gateway_config(fn) -> bool:
    return _accepts_keyword(fn, "gateway_config")


@router.post("/reverse", response_model=ReverseOut)
def reverse(body: ReverseIn, db: Session = Depends(get_db),
            user: User = Depends(get_current_user)):
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
    reverse_gateway_config = (
        runtime_config_for_model(model, "vision")
        if _accepts_gateway_config(gateway.reverse_prompt) else None
    )
    reverse_gateway_mock = settings.mock_mode or (
        (reverse_gateway_config is None or reverse_gateway_config.source == "env")
        and settings.effective_mock_mode
    )

    replay = _existing_reverse_operation_replay(db, user_id=user.id, body=body)
    if replay is not None:
        return replay
    _rate_limit(user.id)
    operation, replay = _reserve_reverse_operation(db, user_id=user.id, body=body)
    if replay is not None:
        return replay

    precharged = 0
    operation_id = int(operation.id)
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
                _charge_reverse_operation(
                    db,
                    operation_id,
                    precharged,
                    note=(
                        f"preauth target={body.target},preset={video_preset},"
                        f"max_refs={precharge_frames}"
                    ),
                )
            except credits.InsufficientCredits as e:
                _fail_reverse_operation(
                    db,
                    operation_id,
                    str(e),
                    refund_note=f"preauth_failed target={body.target}",
                )
                raise HTTPException(400, str(e))
            except ReverseOperationClosed:
                raise HTTPException(409, "反推请求已被恢复任务关闭,请重新发起") from None
    try:
        collected_refs = _collect_refs(
            body,
            db,
            user,
            frame_budget=requested_frame_budget,
            video_preset=video_preset,
            gateway_mock=reverse_gateway_mock,
        )
        if isinstance(collected_refs, tuple) and len(collected_refs) == 2:
            refs, video_analysis = collected_refs
        else:
            refs, video_analysis = collected_refs, None
    except Exception:
        _fail_reverse_operation(
            db,
            operation_id,
            "素材引用解析失败",
            refund_note=f"preauth_refund target={body.target}",
        )
        raise
    cost = expected_cost
    if precharged > cost:
        try:
            _refund_reverse_operation_charge(
                db,
                operation_id,
                precharged - cost,
                note=f"preauth_adjust target={body.target},refs={len(refs)}",
            )
        except ReverseOperationClosed:
            raise HTTPException(409, "反推请求已被恢复任务关闭,请重新发起") from None
    elif cost > precharged:
        try:
            _charge_reverse_operation(
                db,
                operation_id,
                cost - precharged,
                note=f"target={body.target},preset={video_preset},refs={len(refs)}",
            )
        except credits.InsufficientCredits as e:
            _fail_reverse_operation(
                db,
                operation_id,
                str(e),
                refund_note=f"preauth_refund target={body.target}",
            )
            raise HTTPException(400, str(e))
        except ReverseOperationClosed:
            raise HTTPException(409, "反推请求已被恢复任务关闭,请重新发起") from None
    if not _touch_reverse_operation(
        db,
        operation_id,
        charged_credits=cost,
    ):
        raise HTTPException(409, "反推请求已被恢复任务关闭,请重新发起")
    try:
        kwargs = {"target": body.target}
        if _accepts_gateway_config(gateway.reverse_prompt):
            kwargs["gateway_config"] = reverse_gateway_config
        if video_analysis is not None and _accepts_keyword(gateway.reverse_prompt, "video_analysis"):
            kwargs["video_analysis"] = video_analysis
        result = gateway.reverse_prompt(refs, model.model_id, **kwargs)
        if video_analysis is not None:
            result["video_analysis"] = {
                **video_analysis,
                "shots": result.pop("shots", []),
                "analysis_gaps": result.pop("analysis_gaps", []),
            }
    except HTTPException as e:
        _fail_reverse_operation(
            db,
            operation_id,
            str(e.detail),
            refund_note=f"failed target={body.target},refs={len(refs)}",
        )
        usage.record_call(db, kind="reverse", model_id=model.model_id,
                          user_id=user.id, status="failed",
                          detail={"target": body.target, "cost": cost,
                                  "preset": video_preset,
                                  "error": str(e.detail)[:200]})
        raise
    except gateway.GatewayError as e:
        _fail_reverse_operation(
            db,
            operation_id,
            str(e),
            refund_note=f"failed target={body.target},refs={len(refs)}",
        )
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
        _fail_reverse_operation(
            db,
            operation_id,
            str(e),
            refund_note=f"failed target={body.target},refs={len(refs)}",
        )
        usage.record_call(db, kind="reverse", model_id=model.model_id,
                          user_id=user.id, status="failed",
                          detail={"target": body.target, "cost": cost,
                                  "preset": video_preset,
                                  "error": str(e)[:200]})
        raise HTTPException(502, "反推失败:服务暂时不可用,请稍后重试")

    try:
        _assert_text_allowed(db, result.get("structured"), result.get("final_text"))
    except HTTPException:
        _fail_reverse_operation(
            db,
            operation_id,
            "反推结果被内容安全策略拦截",
            refund_note=f"blocked_output target={body.target},refs={len(refs)}",
        )
        usage.record_call(
            db,
            kind="reverse",
            model_id=model.model_id,
            user_id=user.id,
            status="failed",
            usage=result.get("usage"),
            detail={
                "target": body.target,
                "preset": video_preset,
                "frames": len(refs),
                "cost": cost,
                "blocked_output": True,
            },
        )
        raise

    # real-cost accounting: persist the provider's token usage for this call
    usage.record_call(db, kind="reverse", model_id=model.model_id, user_id=user.id,
                      status="ok", latency_ms=result.get("latency_ms"),
                      usage=result.get("usage"),
                      detail={"target": body.target, "preset": video_preset,
                              "frames": len(refs), "cost": cost})
    finished = _finish_reverse_operation(
        db,
        operation_id,
        result=result,
        charged_credits=cost,
        reference_count=max(1, len(refs)),
    )
    if not finished:
        raise HTTPException(409, "反推请求已被恢复任务关闭,请重新发起")
    try:
        if body.target == "video":
            history_title = "视频反推提示词"
            history_category = "video"
        elif body.target == "product_profile":
            history_title = "主体身份档案"
            history_category = "image"
        else:
            history_title = "图片反推提示词"
            history_category = "image"
        remember_prompt(
            db,
            user_id=user.id,
            prompt=result.get("final_text"),
            title=history_title,
            category=history_category,
            source="reverse",
            params={"asset_url": body.asset_url, "reference_count": max(1, len(refs))},
            commit=True,
        )
    except Exception:
        rollback = getattr(db, "rollback", None)
        if callable(rollback):
            rollback()
        log.exception("failed to remember reverse prompt history for user %s", user.id)
    return ReverseOut(
        structured=result["structured"],
        final_text=result["final_text"],
        charged_credits=cost,
        reference_count=max(1, len(refs)),
        video_analysis=(
            result.get("video_analysis")
            if isinstance(result.get("video_analysis"), dict) else None
        ),
    )
