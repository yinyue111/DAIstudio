"""Shared validation and compatibility helpers for prompt reverse routes."""
from __future__ import annotations

import hashlib
import inspect
import json
import logging
from functools import wraps

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ReverseOperation, User
from ..schemas import ReverseIn, ReverseOperationCreate, ReverseOut
from ..services import (
    asset_refs,
    locks,
    reverse_source_resolution,
    storage,
    video_frames,
)
from ..services.content_safety import assert_text_allowed
from ..services.rate_limit import incr_window
from ..services.ssrf import SsrfError, assert_safe_user_asset_url

log = logging.getLogger("prompt")

_VIDEO_EXTS = (".mp4", ".webm", ".mov")
_UNSUPPORTED_VIDEO_EXTS = (".m3u8",)
_REVERSE_RATE_LIMIT = 30
_REVERSE_RATE_WINDOW = 3600
_REVERSE_WS_TICKET_TTL_SECONDS = 60
_CLIENT_REVISION_SOURCES = frozenset({"user_edit", "applied"})
_REVERSE_STATUSES = {
    "queued",
    "running",
    "needs_confirmation",
    "succeeded",
    "failed",
    "canceled",
}
REVERSE_IMAGE_REFERENCE_MAX_SIDE = 1024
REVERSE_IMAGE_REFERENCE_QUALITY = 92
_assert_text_allowed = assert_text_allowed
_LEGACY_REVERSE_HEADERS = {
    "Deprecation": "true",
    "Sunset": "Wed, 16 Sep 2026 00:00:00 GMT",
    "Link": '</api/prompt/reverse-operations>; rel="successor-version"',
}


def _acquire_reverse_quote_lock(user_id: int, quote_id: int | None) -> tuple[str, str]:
    if quote_id is None:
        raise HTTPException(
            400,
            detail={"code": "QUOTE_REQUIRED", "message": "反推执行前必须确认有效报价"},
        )
    key = f"reverse:quote:consume:{int(user_id)}:{int(quote_id)}"
    token = locks.acquire(key, ttl=60)
    if not token:
        raise HTTPException(
            409,
            detail={"code": "QUOTE_IN_USE", "message": "报价正在使用，请稍后刷新"},
        )
    return key, token


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


def _looks_like_video_url(url: str) -> bool:
    return reverse_source_resolution.looks_like_video_url(url)


def _is_unsupported_video_url(url: str) -> bool:
    return reverse_source_resolution.is_unsupported_video_url(url)


def _is_video_source(body: ReverseIn) -> bool:
    return reverse_source_resolution.is_video_source(body)


def _reverse_sources(body: ReverseIn | ReverseOperationCreate) -> list[dict]:
    return reverse_source_resolution.reverse_sources(body)


def _gateway_ref(db: Session, user: User, url: str | None) -> str | None:
    return reverse_source_resolution.gateway_ref(db, user, url)


def _gateway_ref_with_content_hash(
    db: Session,
    user: User,
    url: str | None,
) -> tuple[str | None, str | None]:
    return reverse_source_resolution.gateway_ref_with_content_hash(db, user, url)


def _collect_refs(
    body: ReverseIn,
    db: Session,
    user: User,
    *,
    frame_budget: int | None = None,
    video_preset: str | None = None,
    gateway_mock: bool | None = None,
    include_source_fingerprints: bool = False,
) -> tuple[list[str], dict | None] | tuple[list[str], dict | None, list[dict]]:
    """Compatibility shim for callers and tests that patch router resolvers."""
    return reverse_source_resolution.collect_refs(
        body,
        db,
        user,
        frame_budget=frame_budget,
        video_preset=video_preset,
        gateway_mock=gateway_mock,
        include_source_fingerprints=include_source_fingerprints,
        gateway_ref_resolver=_gateway_ref,
        gateway_ref_with_hash_resolver=_gateway_ref_with_content_hash,
    )


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
    if body.project_id is not None:
        payload["project_id"] = int(body.project_id)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _reverse_operation_response(op: ReverseOperation) -> ReverseOut:
    result = op.result if isinstance(op.result, dict) else {}
    structured = result.get("structured") if isinstance(result.get("structured"), dict) else {}
    final_text = str(result.get("final_text") or "")
    if not final_text:
        raise HTTPException(409, "该反推请求结果不完整,请重新发起")
    evidence_fields = {
        "evidence_type",
        "bbox",
        "field_key",
        "evidence_text",
        "confidence",
        "source_index",
        "fact_status",
        "protected",
        "editable",
    }
    legacy_image_evidence = [
        {key: row[key] for key in evidence_fields if key in row}
        for row in result.get("image_evidence", [])
        if isinstance(row, dict)
    ]
    return ReverseOut(
        structured=structured,
        final_text=final_text,
        image_evidence=legacy_image_evidence,
        charged_credits=int(op.cost_settled or op.charged_credits or 0),
        reference_count=max(1, int(op.reference_count or 1)),
        video_analysis=(
            result.get("video_analysis")
            if isinstance(result.get("video_analysis"), dict)
            else None
        ),
        model_config_id=getattr(op, "model_config_id", None),
        model_name=(
            str((op.model_snapshot or {}).get("model_name") or "").strip() or None
            if isinstance(op.model_snapshot, dict)
            else None
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
    if existing.error_code == "RESULT_EXPIRED":
        raise HTTPException(
            409,
            "该反推结果已超过保留期，请使用新的 client_request_id 重新发起",
        )
    if existing.status == "succeeded":
        return _reverse_operation_response(existing)
    if existing.status == "needs_confirmation":
        raise _legacy_cover_confirmation_required(existing)
    if existing.status in {"queued", "running"}:
        raise HTTPException(409, "反推请求仍在处理中,请稍后重试")
    raise HTTPException(409, "该反推请求已失败,请重新发起")


def _video_duration_for_reverse(
    body: ReverseIn,
    db: Session,
    user: User,
) -> float | None:
    return reverse_source_resolution.video_duration_for_reverse(body, db, user)


def _video_selection_payload(body):
    return reverse_source_resolution.video_selection_payload(body)


def _validate_local_video_selection(
    body: ReverseIn | ReverseOperationCreate,
    db: Session,
    user: User,
) -> None:
    """Reject invalid local-video selections before any credits are frozen."""
    if not _is_video_source(body):
        return
    key = storage.key_from_url(str(body.asset_url or ""))
    if not key:
        return
    try:
        path = asset_refs.generated_video_reference_path(db, user.id, key)
    except asset_refs.AssetRefError as exc:
        raise HTTPException(404, str(exc)) from exc
    metadata = video_frames.probe_media(str(path))
    raw_duration = metadata.get("duration_seconds") or metadata.get("duration")
    if not raw_duration:
        return
    source_ranges, range_start, range_end, custom_timestamps = _video_selection_payload(body)
    try:
        video_frames.validate_video_selection(
            source_ranges=source_ranges,
            start_seconds=range_start,
            end_seconds=range_end,
            custom_timestamps=custom_timestamps,
            total_duration=float(raw_duration),
        )
    except video_frames.VideoSelectionError as exc:
        raise HTTPException(400, str(exc)) from exc


def _accepts_keyword(fn, name: str) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    return name in sig.parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in sig.parameters.values()
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
        raise HTTPException(
            400,
            "该视频是 HLS/m3u8 流媒体,抽帧组件暂不支持拉取分段流,"
            "请改用 mp4/webm/mov 视频文件,或选择封面图做单帧反推",
        )


def _legacy_cover_confirmation_required(operation: ReverseOperation) -> HTTPException:
    return HTTPException(
        409,
        detail={
            "message": "无法抽取视频关键帧,请确认是否改用封面单帧分析",
            "operation_id": int(operation.id),
            "status": "needs_confirmation",
            "confirmation_expires_at": (
                operation.confirmation_expires_at.isoformat()
                if operation.confirmation_expires_at
                else None
            ),
        },
        headers=dict(_LEGACY_REVERSE_HEADERS),
    )
