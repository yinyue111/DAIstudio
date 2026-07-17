"""Reverse prompt (optional). Vision model: image(s) -> structured words.

For an image asset we send the image straight to the vision model. For a video
asset (target=video) we sample keyframes in temporal order. Sampling failures
pause for explicit cover confirmation instead of silently changing contracts.
"""
from __future__ import annotations

import base64
import hashlib
import inspect
import json
import logging
import secrets
from functools import wraps
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import get_current_user
from ..models import ReverseOperation, User
from ..redis_client import redis_client
from ..schemas import (
    PromptOptimizeIn,
    PromptOptimizeOut,
    ReverseIn,
    ReverseOperationConfirm,
    ReverseOperationCreate,
    ReverseOperationOut,
    ReverseOut,
)
from ..services import (
    asset_refs,
    credits,
    gateway,
    reverse_operations,
    storage,
    usage,
    video_frames,
)
from ..services.config_store import (
    ModelConfigResolutionError,
    get_model_config,
    get_setting,
    resolve_model_config,
)
from ..services.content_safety import assert_text_allowed
from ..services.model_gateway_config import runtime_config_for_model
from ..services.rate_limit import incr_window
from ..services.ssrf import SsrfError, assert_safe_user_asset_url

router = APIRouter(prefix="/api/prompt", tags=["prompt"])
log = logging.getLogger("prompt")

_VIDEO_EXTS = (".mp4", ".webm", ".mov")
_UNSUPPORTED_VIDEO_EXTS = (".m3u8",)
_REVERSE_RATE_LIMIT = 30
_REVERSE_RATE_WINDOW = 3600
_OPTIMIZE_RATE_LIMIT = 60
_REVERSE_WS_TICKET_TTL_SECONDS = 60
_REVERSE_STATUSES = {
    "queued", "running", "needs_confirmation", "succeeded", "failed", "canceled",
}
REVERSE_IMAGE_REFERENCE_MAX_SIDE = 1024
REVERSE_IMAGE_REFERENCE_QUALITY = 92
_assert_text_allowed = assert_text_allowed
_LEGACY_REVERSE_HEADERS = {
    "Deprecation": "true",
    "Sunset": "Wed, 16 Sep 2026 00:00:00 GMT",
    "Link": '</api/prompt/reverse-operations>; rel="successor-version"',
}


def _deprecated_reverse_endpoint(fn):
    """Attach deprecation metadata to every legacy response, including errors."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        response = kwargs.get("response")
        if response is not None:
            for name, value in _LEGACY_REVERSE_HEADERS.items():
                response.headers[name] = value
        body = kwargs.get("body")
        user = kwargs.get("user")
        log.warning(
            "deprecated reverse endpoint called user_id=%s client_request_id=%s target=%s",
            getattr(user, "id", None),
            getattr(body, "client_request_id", None),
            getattr(body, "target", None),
        )
        try:
            return fn(*args, **kwargs)
        except HTTPException as exc:
            exc.headers = {
                **_LEGACY_REVERSE_HEADERS,
                **dict(exc.headers or {}),
            }
            raise

    return wrapped


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
    optimizer_config_id = getattr(body, "optimizer_model_config_id", None)
    try:
        model = (
            resolve_model_config(db, "prompt", optimizer_config_id)
            if optimizer_config_id is not None
            else get_model_config(db, "prompt")
        )
    except ModelConfigResolutionError as exc:
        raise HTTPException(400, str(exc)) from exc
    if model is None or not model.enabled:
        raise HTTPException(503, "提示词优化模型未启用")

    target_use = body.category
    target_config_id = getattr(body, "target_model_config_id", None)
    try:
        target_model = (
            resolve_model_config(db, target_use, target_config_id)
            if target_config_id is not None
            else get_model_config(db, target_use)
        )
    except ModelConfigResolutionError as exc:
        raise HTTPException(400, str(exc)) from exc
    if target_model is None or not target_model.enabled:
        raise HTTPException(503, f"{target_use} 生成模型未启用")
    target_model_id = target_model.model_id
    target_model_provider = (
        target_model.provider
        or runtime_config_for_model(target_model, target_use).provider
    )
    # Deprecated raw target identifiers are accepted only when they match a
    # catalog row selected by id/default. They can no longer route a request.
    if body.target_model_id and body.target_model_id != target_model_id:
        raise HTTPException(400, "target_model_id 与后台模型目录不匹配")
    if body.target_model_provider and body.target_model_provider != target_model_provider:
        raise HTTPException(400, "target_model_provider 与后台模型目录不匹配")
    target_model_extra = dict(target_model.extra) if isinstance(target_model.extra, dict) else None
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
            model_config_id=getattr(model, "id", None),
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
        model_config_id=getattr(model, "id", None),
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
        optimizer_model_config_id=getattr(model, "id", None),
        optimizer_model_name=(getattr(model, "display_name", None) or model.model_id),
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
    # Mock mode must still exercise FFmpeg for user-owned local uploads so the
    # offline E2E path can validate real keyframe evidence. Keep remote video
    # downloads disabled in mock mode to avoid unexpected network access.
    if video_frames.available() and (local_video_path is not None or not effective_gateway_mock):
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
        "video_analysis_preset": body.video_analysis_preset or "standard",
        "fallback_image": body.fallback_image,
        "model_config_id": getattr(body, "model_config_id", None),
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
        charged_credits=int(op.cost_settled or op.charged_credits or 0),
        reference_count=max(1, int(op.reference_count or 1)),
        video_analysis=(
            result.get("video_analysis")
            if isinstance(result.get("video_analysis"), dict) else None
        ),
        model_config_id=getattr(op, "model_config_id", None),
        model_name=(
            str((op.model_snapshot or {}).get("model_name") or "").strip() or None
            if isinstance(op.model_snapshot, dict) else None
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
    if existing.status == "needs_confirmation":
        raise _legacy_cover_confirmation_required(existing)
    if existing.status in {"queued", "running"}:
        raise HTTPException(409, "反推请求仍在处理中,请稍后重试")
    raise HTTPException(409, "该反推请求已失败,请重新发起")


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


def _validate_reverse_asset_request(
    db: Session,
    *,
    asset_url: str,
    fallback_image: str | None,
    target: str,
    source_type: str | None,
) -> None:
    _assert_text_allowed(db, asset_url, fallback_image)
    try:
        assert_safe_user_asset_url(asset_url)
        assert_safe_user_asset_url(fallback_image)
    except SsrfError as exc:
        raise HTTPException(400, f"素材链接被安全策略拦截:{exc}") from exc
    pseudo = ReverseIn(
        asset_url=asset_url,
        target=target,
        source_type=source_type,
        fallback_image=fallback_image,
    )
    if _is_video_source(pseudo) and target != "video":
        raise HTTPException(400, "视频素材仅支持视频反推")
    if _is_unsupported_video_url(asset_url):
        raise HTTPException(400, "暂不支持 HLS/m3u8 视频反推,请使用 mp4/webm/mov 或封面图")


def _legacy_cover_confirmation_required(operation: ReverseOperation) -> HTTPException:
    return HTTPException(
        409,
        detail={
            "message": "无法抽取视频关键帧,请确认是否改用封面单帧分析",
            "operation_id": int(operation.id),
            "status": "needs_confirmation",
            "confirmation_expires_at": (
                operation.confirmation_expires_at.isoformat()
                if operation.confirmation_expires_at else None
            ),
        },
        headers=dict(_LEGACY_REVERSE_HEADERS),
    )


@router.post(
    "/reverse-operations",
    response_model=ReverseOperationOut,
    status_code=202,
)
def create_reverse_operation(
    body: ReverseOperationCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        replay = reverse_operations.find_idempotent_operation(
            db,
            user_id=user.id,
            body=body,
        )
    except reverse_operations.ReverseOperationConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    if replay is not None:
        return reverse_operations.serialize_operation(replay)
    _validate_reverse_asset_request(
        db,
        asset_url=body.asset_url,
        fallback_image=body.fallback_image,
        target=body.target,
        source_type=body.source_type,
    )
    if not get_setting(db, "reverse_prompt_enabled", True):
        raise HTTPException(403, "反推功能已被管理员关闭")
    _rate_limit(user.id)
    try:
        operation, created = reverse_operations.create_operation(
            db,
            user_id=user.id,
            body=body,
        )
    except reverse_operations.ReverseOperationConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except reverse_operations.ReverseOperationInvalid as exc:
        raise HTTPException(400, str(exc)) from exc
    except credits.InsufficientCredits as exc:
        raise HTTPException(400, str(exc)) from exc
    if created:
        try:
            reverse_operations.enqueue_operation(int(operation.id))
        except Exception as exc:  # noqa: BLE001
            log.exception("failed to enqueue reverse operation %s", operation.id)
            if reverse_operations.fail_queued_submission(
                int(operation.id),
                code="BROKER_UNAVAILABLE",
                error="反推任务队列暂时不可用,已退回冻结积分",
            ):
                raise HTTPException(503, "反推任务队列暂时不可用,请稍后重试") from exc
            # The broker may have accepted the message before the publish call
            # raised. If the worker already claimed it, its database state is
            # authoritative and the create request remains a successful 202.
            db.expire_all()
            advanced = db.get(ReverseOperation, int(operation.id))
            if advanced is None:
                raise HTTPException(503, "反推任务队列状态不确定,请使用原 client_request_id 重试") from exc
            operation = advanced
            log.warning(
                "reverse operation %s advanced to %s after enqueue raised; returning operation",
                operation.id,
                operation.status,
            )
    db.expire_all()
    operation = db.get(ReverseOperation, int(operation.id)) or operation
    return reverse_operations.serialize_operation(operation)


@router.get("/reverse-operations", response_model=list[ReverseOperationOut])
def list_reverse_operations(
    status: str | None = None,
    limit: int = 30,
    offset: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if status is not None and status not in _REVERSE_STATUSES:
        raise HTTPException(422, "反推任务状态非法")
    rows = reverse_operations.list_owned_operations(
        db,
        user_id=user.id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return [reverse_operations.serialize_operation(row) for row in rows]


@router.get("/reverse-operations/{operation_id}", response_model=ReverseOperationOut)
def get_reverse_operation(
    operation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        operation = reverse_operations.get_owned_operation(db, operation_id, user.id)
    except reverse_operations.ReverseOperationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    return reverse_operations.serialize_operation(operation)


@router.post(
    "/reverse-operations/{operation_id}/confirm-cover",
    response_model=ReverseOperationOut,
    status_code=202,
)
def confirm_reverse_operation_cover(
    operation_id: int,
    body: ReverseOperationConfirm,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if body.fallback_image:
        _assert_text_allowed(db, body.fallback_image)
        try:
            assert_safe_user_asset_url(body.fallback_image)
        except SsrfError as exc:
            raise HTTPException(400, f"封面链接被安全策略拦截:{exc}") from exc
    try:
        operation = reverse_operations.confirm_cover(
            db,
            operation_id=operation_id,
            user_id=user.id,
            fallback_image=body.fallback_image,
        )
    except reverse_operations.ReverseOperationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except reverse_operations.ReverseOperationConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except reverse_operations.ReverseOperationInvalid as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        reverse_operations.enqueue_operation(operation_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("failed to enqueue confirmed reverse operation %s", operation_id)
        if reverse_operations.fail_queued_submission(
            operation_id,
            code="BROKER_UNAVAILABLE",
            error="反推任务队列暂时不可用,已退回冻结积分",
        ):
            raise HTTPException(503, "反推任务队列暂时不可用,请稍后重试") from exc
        # A publish call may raise after the broker accepted the message. If
        # the worker already claimed it, keep that state authoritative instead
        # of refunding or terminating the running operation.
        db.expire_all()
        advanced = db.get(ReverseOperation, operation_id)
        if advanced is None:
            raise HTTPException(503, "反推任务队列状态不确定,请稍后查询任务状态") from exc
        operation = advanced
        log.warning(
            "confirmed reverse operation %s advanced to %s after enqueue raised; "
            "returning operation",
            operation.id,
            operation.status,
        )
    db.expire_all()
    operation = db.get(ReverseOperation, operation_id) or operation
    return reverse_operations.serialize_operation(operation)


@router.post("/reverse-operations/{operation_id}/cancel", response_model=ReverseOperationOut)
def cancel_reverse_operation(
    operation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        operation = reverse_operations.request_cancel(
            db,
            operation_id=operation_id,
            user_id=user.id,
        )
    except reverse_operations.ReverseOperationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    return reverse_operations.serialize_operation(operation)


@router.post("/reverse-operations/{operation_id}/ws-ticket")
def create_reverse_operation_ws_ticket(
    operation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    operation = db.get(ReverseOperation, operation_id)
    if operation is None or operation.user_id != user.id:
        raise HTTPException(404, "反推任务不存在")
    count = incr_window(f"ws:ticket-rate:reverse:{user.id}", 60)
    if count > max(1, int(settings.ws_ticket_rate_per_minute or 1)):
        raise HTTPException(429, "WebSocket 连接过于频繁,请稍后再试")
    ticket = secrets.token_urlsafe(32)
    redis_client.setex(
        f"ws:reverse-ticket:{ticket}",
        _REVERSE_WS_TICKET_TTL_SECONDS,
        f"{user.id}:{operation_id}:{user.token_version}",
    )
    return {"ticket": ticket, "expires_in": _REVERSE_WS_TICKET_TTL_SECONDS}


@router.post("/reverse", response_model=ReverseOut, deprecated=True)
@_deprecated_reverse_endpoint
def reverse(body: ReverseIn, db: Session = Depends(get_db),
            user: User = Depends(get_current_user), response: Response = None):
    replay = _existing_reverse_operation_replay(db, user_id=user.id, body=body)
    if replay is not None:
        return replay
    _validate_reverse_asset_request(
        db,
        asset_url=body.asset_url,
        fallback_image=body.fallback_image,
        target=body.target,
        source_type=body.source_type,
    )
    if not get_setting(db, "reverse_prompt_enabled", True):
        raise HTTPException(403, "反推功能已被管理员关闭")
    _rate_limit(user.id)
    try:
        operation, created = reverse_operations.create_legacy_operation(
            db,
            user_id=user.id,
            body=body,
            fingerprint=_reverse_request_fingerprint(body),
        )
    except reverse_operations.ReverseOperationConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except reverse_operations.ReverseOperationInvalid as exc:
        status_code = 403 if str(exc) == "反推功能已被管理员关闭" else 400
        raise HTTPException(status_code, str(exc)) from exc
    except credits.InsufficientCredits as exc:
        raise HTTPException(400, str(exc)) from exc

    if not created:
        if operation.status == "succeeded":
            return _reverse_operation_response(operation)
        if operation.status == "needs_confirmation":
            raise _legacy_cover_confirmation_required(operation)
        if operation.status == "running":
            raise HTTPException(409, "反推请求仍在处理中,请稍后重试")
        if operation.status in {"failed", "canceled"}:
            raise HTTPException(409, "该反推请求已关闭,请重新发起")

    reverse_operations.run_operation(int(operation.id))
    db.expire_all()
    operation = db.get(ReverseOperation, int(operation.id))
    if operation is None:
        raise HTTPException(502, "反推任务状态丢失,请稍后重试")
    if operation.status == "succeeded":
        return _reverse_operation_response(operation)
    if operation.status == "needs_confirmation":
        raise _legacy_cover_confirmation_required(operation)
    if operation.status == "canceled":
        raise HTTPException(409, "反推任务已取消,积分已退回")

    detail = operation.error or "反推失败,请稍后重试"
    if operation.error_code in {
        "CONTENT_SAFETY_BLOCKED",
        "INVALID_OPERATION",
        "REQUEST_REJECTED",
    }:
        raise HTTPException(400, detail)
    if operation.error_code == "ASSET_NOT_FOUND":
        raise HTTPException(404, detail)
    if operation.error_code == "MODEL_CONFIG_CHANGED":
        raise HTTPException(409, detail)
    raise HTTPException(502, detail)
