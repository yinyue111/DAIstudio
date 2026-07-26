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
    ReverseBatchCreate,
    ReverseBatchOut,
    ReverseIn,
    ReverseOperationConfirm,
    ReverseOperationCreate,
    ReverseOperationFeedbackIn,
    ReverseOperationFeedbackOut,
    ReverseOperationOut,
    ReverseOperationRetryIn,
    ReverseOut,
    ReverseResultApplyIn,
    ReverseResultApplyOut,
    ReverseResultRevisionIn,
    ReverseResultRevisionOut,
    ReverseShotGenerationPrepareIn,
    ReverseShotGenerationPrepareOut,
    ReverseShotReanalyzeIn,
    ReverseShotTimelineEditIn,
)
from ..services import (
    asset_refs,
    credits,
    image_evidence_analysis,
    locks,
    project_collection,
    reverse_lineage,
    reverse_operations,
    storage,
    video_audio,
    video_evidence_analysis,
    video_frames,
)
from ..services.config_store import get_setting
from ..services.content_safety import assert_text_allowed
from ..services.generation_image_evidence import (
    ReviewedEvidenceMaskError,
    validate_saved_reviewed_image_evidence,
)
from ..services.product_edition import feature_enabled
from ..services.rate_limit import incr_window
from ..services.ssrf import SsrfError, assert_safe_user_asset_url

router = APIRouter(prefix="/api/prompt", tags=["prompt"])
log = logging.getLogger("prompt")

_VIDEO_EXTS = (".mp4", ".webm", ".mov")
_UNSUPPORTED_VIDEO_EXTS = (".m3u8",)
_REVERSE_RATE_LIMIT = 30
_REVERSE_RATE_WINDOW = 3600
_REVERSE_WS_TICKET_TTL_SECONDS = 60
_CLIENT_REVISION_SOURCES = frozenset({"user_edit", "applied"})
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


@router.get("/reverse-analyzers/status")
def reverse_analyzer_status(
    _user: User = Depends(get_current_user),
):
    return {
        "image": image_evidence_analysis.analyzer_health(),
        "video": video_evidence_analysis.analyzer_health(),
        "audio": video_audio.analyzer_health(),
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


@router.post("/optimize", status_code=410)
def optimize_prompt_text(
    _body: PromptOptimizeIn,
    _user: User = Depends(get_current_user),
):
    raise HTTPException(
        status_code=410,
        detail={
            "code": "PROMPT_OPTIMIZATION_MOVED",
            "message": (
                "旧提示词优化端点已退役，请先创建统一报价，"
                "再通过 Studio 提示词优化建议流程执行。"
            ),
            "quote_endpoint": "/api/quotes",
            "quote_kind": "prompt_optimization",
            "proposal_endpoint": "/api/studio/prompt-optimizations",
        },
    )


def _looks_like_video_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(_VIDEO_EXTS + _UNSUPPORTED_VIDEO_EXTS)


def _is_unsupported_video_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(_UNSUPPORTED_VIDEO_EXTS)


def _is_video_source(body: ReverseIn) -> bool:
    return body.source_type == "video" or _looks_like_video_url(str(body.asset_url or ""))


def _reverse_sources(body: ReverseIn | ReverseOperationCreate) -> list[dict]:
    sources = getattr(body, "sources", None) or []
    if sources:
        return [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
            for item in sources
        ]
    return [{
        "asset_url": str(body.asset_url or ""),
        "source_type": body.source_type or ("video" if _is_video_source(body) else "image"),
        "role": "primary",
    }]


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


def _gateway_ref_with_content_hash(
    db: Session,
    user: User,
    url: str | None,
) -> tuple[str | None, str | None]:
    try:
        resolved = asset_refs.gateway_ref_for_user_asset(
            db,
            user.id,
            url,
            max_side=REVERSE_IMAGE_REFERENCE_MAX_SIDE,
            prefer_original_upload=True,
            quality=REVERSE_IMAGE_REFERENCE_QUALITY,
            subsampling=0,
            return_content_hash=True,
        )
    except asset_refs.AssetRefError as e:
        raise HTTPException(404, str(e))
    if resolved is None:
        return None, None
    if isinstance(resolved, tuple):
        return resolved
    return resolved, reverse_lineage.data_uri_content_hash(resolved)


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
    """Resolve the asset into one or more image refs (URLs or base64 data-URIs)
    to feed the vision model. The image-target/video-url mismatch is rejected by
    the caller before this runs."""
    if not _is_video_source(body):
        refs = []
        reference_context = []
        source_fingerprints = []
        for source in _reverse_sources(body):
            if include_source_fingerprints:
                ref, content_hash = _gateway_ref_with_content_hash(
                    db, user, source.get("asset_url")
                )
            else:
                ref = _gateway_ref(db, user, source.get("asset_url"))
                content_hash = None
            if ref:
                refs.append(ref)
                if content_hash:
                    source_fingerprints.append(reverse_lineage.build_source_fingerprint(
                        source_index=len(refs),
                        content_hash=content_hash,
                        locator=source.get("asset_url"),
                    ))
                reference_context.append({
                    "role": source.get("role") or "primary",
                    "label": source.get("label"),
                    "source_type": "image",
                })
        analysis = {"reference_context": reference_context} if len(refs) > 1 else None
        if include_source_fingerprints:
            return refs, analysis, source_fingerprints
        return refs, analysis

    key = storage.key_from_url(str(body.asset_url or ""))
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
    source_ranges, range_start, range_end, custom_timestamps = _video_selection_payload(body)
    if video_frames.available() and (local_video_path is not None or not effective_gateway_mock):
        n_frames = max(1, int(frame_budget or settings.reverse_video_frames or 1))
        sample_kwargs = {"n": n_frames, "preset": video_preset}
        if source_ranges or custom_timestamps:
            sample_kwargs.update({
                "start_seconds": range_start,
                "end_seconds": range_end,
                "source_ranges": source_ranges,
                "custom_timestamps": custom_timestamps,
            })
        try:
            if local_video_path is not None:
                sample = video_frames.sample_video_from_path(
                    str(local_video_path),
                    **sample_kwargs,
                )
            else:
                sample = video_frames.sample_video(
                    str(body.asset_url or ""),
                    **sample_kwargs,
                )
        except asset_refs.AssetRefError as e:
            raise HTTPException(404, str(e))
        except video_frames.VideoSelectionError as e:
            # A user-selected time outside the real source is not a decoder
            # failure and must never be converted into cover-mode analysis.
            raise HTTPException(422, str(e)) from e
        except Exception as e:  # noqa: BLE001
            log.warning("video keyframe sampling failed, using cover fallback: %s", e)
            sample = None
        if sample and sample.frames:
            analysis = sample.analysis()
            analysis["analysis_mode"] = (
                "keyframes_multi_segment" if len(source_ranges) > 1 else "keyframes"
            )
            refs = [
                    "data:image/jpeg;base64," + base64.b64encode(frame.jpeg).decode()
                    for frame in sample.frames
                ]
            source_fingerprints = [
                reverse_lineage.build_source_fingerprint(
                    source_index=index,
                    content_hash=reverse_lineage.bytes_content_hash(frame.jpeg),
                    locator=f"{body.asset_url}#sampled-frame:{index}",
                    method="sampled_frame_sha256",
                )
                for index, frame in enumerate(sample.frames, start=1)
            ]
            reference_context = [
                {
                    "role": "frame",
                    "source_type": "video",
                    **row,
                }
                for row in analysis.get("sampled_frames") or []
            ]
            for source in _reverse_sources(body)[1:]:
                if include_source_fingerprints:
                    ref, content_hash = _gateway_ref_with_content_hash(
                        db, user, source.get("asset_url")
                    )
                else:
                    ref = _gateway_ref(db, user, source.get("asset_url"))
                    content_hash = None
                if ref:
                    refs.append(ref)
                    if content_hash:
                        source_fingerprints.append(reverse_lineage.build_source_fingerprint(
                            source_index=len(refs),
                            content_hash=content_hash,
                            locator=source.get("asset_url"),
                        ))
                    reference_context.append({
                        "role": source.get("role") or "style",
                        "label": source.get("label"),
                        "source_type": "image",
                    })
            analysis["reference_context"] = reference_context
            if include_source_fingerprints:
                return refs, analysis, source_fingerprints
            return refs, analysis

    # fall back to a provided cover/keyframe image
    cover = body.fallback_image
    if cover and not _looks_like_video_url(cover):
        if include_source_fingerprints:
            ref, content_hash = _gateway_ref_with_content_hash(db, user, cover)
        else:
            ref = _gateway_ref(db, user, cover)
            content_hash = None
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
        refs = [ref] if ref else []
        if include_source_fingerprints:
            fingerprints = (
                [reverse_lineage.build_source_fingerprint(
                    source_index=1,
                    content_hash=content_hash,
                    locator=cover,
                )]
                if ref and content_hash
                else []
            )
            return refs, analysis, fingerprints
        return refs, analysis
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
        "evidence_type", "bbox", "field_key", "evidence_text", "confidence",
        "source_index", "fact_status", "protected", "editable",
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
    if existing.error_code == "RESULT_EXPIRED":
        raise HTTPException(409, "该反推结果已超过保留期，请使用新的 client_request_id 重新发起")
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
    source_ranges = list(getattr(body, "source_ranges", None) or [])
    if source_ranges:
        return sum(
            max(0.0, float(item.end_seconds) - float(item.start_seconds))
            for item in source_ranges
        )
    source_range = getattr(body, "source_range", None)
    if source_range is not None:
        return max(0.0, float(source_range.end_seconds) - float(source_range.start_seconds))
    key = storage.key_from_url(str(body.asset_url or ""))
    if not key:
        return None
    try:
        path = asset_refs.generated_video_reference_path(db, user.id, key)
    except asset_refs.AssetRefError:
        return None
    meta = video_frames.probe_media(str(path))
    duration = meta.get("duration")
    return float(duration) if duration else None


def _video_selection_payload(body):
    source_ranges = [
        item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
        for item in (getattr(body, "source_ranges", None) or [])
    ]
    source_range = getattr(body, "source_range", None)
    if not source_ranges and source_range is not None:
        source_ranges = [
            source_range.model_dump(mode="json")
            if hasattr(source_range, "model_dump")
            else dict(source_range)
        ]
    range_start = float(getattr(source_range, "start_seconds", 0) or 0)
    range_end = getattr(source_range, "end_seconds", None)
    custom_timestamps = list(getattr(body, "custom_keyframes", None) or [])
    return source_ranges, range_start, range_end, custom_timestamps


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
        # 抽帧组件仅支持整文件下载(且 ffmpeg 以 file,pipe 协议白名单运行),
        # 无法拉取 HLS 分段流,这里明确拒绝而不是等运行时抽帧失败。
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
                if operation.confirmation_expires_at else None
            ),
        },
        headers=dict(_LEGACY_REVERSE_HEADERS),
    )


@router.post(
    "/reverse-batches",
    response_model=ReverseBatchOut,
    status_code=202,
)
def create_reverse_batch(
    body: ReverseBatchCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    lock_key, lock_token = _acquire_reverse_quote_lock(user.id, body.quote_id)
    try:
        try:
            replay = reverse_operations.find_quoted_idempotent_batch(
                db,
                user_id=user.id,
                body=body,
            )
        except reverse_operations.ReverseOperationConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except reverse_operations.ReverseOperationInvalid as exc:
            raise HTTPException(400, str(exc)) from exc
        if replay is not None:
            return reverse_operations.serialize_batch(db, replay, include_items=True)

        if body.project_id is not None:
            try:
                project_collection.require_owned_project(db, user.id, body.project_id)
            except project_collection.ProjectNotFound as exc:
                raise HTTPException(404, str(exc)) from exc
        operation_bodies = reverse_operations.batch_operation_bodies(body)
        for operation_body in operation_bodies:
            for source in operation_body.sources:
                _validate_reverse_asset_request(
                    db,
                    asset_url=source.asset_url,
                    fallback_image=(
                        operation_body.fallback_image
                        if source.role == "primary"
                        else None
                    ),
                    target=operation_body.target,
                    source_type=source.source_type,
                )
            _validate_local_video_selection(operation_body, db, user)
        if not get_setting(db, "reverse_prompt_enabled", True):
            raise HTTPException(403, "反推功能已被管理员关闭")
        for _ in operation_bodies:
            _rate_limit(user.id)
        try:
            batch, created, operation_ids = reverse_operations.create_quoted_batch(
                db,
                user_id=user.id,
                body=body,
            )
        except reverse_operations.ReverseOperationConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except reverse_operations.ReverseOperationInvalid as exc:
            raise HTTPException(400, str(exc)) from exc
        except project_collection.ProjectNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except credits.InsufficientCredits as exc:
            raise HTTPException(400, str(exc)) from exc
    finally:
        locks.release(lock_key, lock_token)

    if created:
        for operation_id in operation_ids:
            try:
                reverse_operations.enqueue_operation(operation_id)
            except Exception:  # noqa: BLE001
                log.exception("failed to enqueue reverse batch item %s", operation_id)
                reverse_operations.fail_queued_submission(
                    operation_id,
                    code="BROKER_UNAVAILABLE",
                    error="反推任务队列暂时不可用,该批次单项已退回冻结积分",
                )
    db.expire_all()
    batch = reverse_operations.get_owned_batch(
        db,
        batch_id=int(batch.id),
        user_id=user.id,
    )
    return reverse_operations.serialize_batch(db, batch, include_items=True)


@router.get("/reverse-batches", response_model=list[ReverseBatchOut])
def list_reverse_batches(
    limit: int = 30,
    offset: int = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    batches = reverse_operations.list_owned_batches(
        db,
        user_id=user.id,
        limit=limit,
        offset=offset,
    )
    return [
        reverse_operations.serialize_batch(db, batch, include_items=False)
        for batch in batches
    ]


@router.get("/reverse-batches/{batch_id}", response_model=ReverseBatchOut)
def get_reverse_batch(
    batch_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        batch = reverse_operations.get_owned_batch(
            db,
            batch_id=batch_id,
            user_id=user.id,
        )
    except reverse_operations.ReverseOperationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    return reverse_operations.serialize_batch(db, batch, include_items=True)


@router.post("/reverse-batches/{batch_id}/cancel", response_model=ReverseBatchOut)
def cancel_reverse_batch(
    batch_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        batch = reverse_operations.request_batch_cancel(
            db,
            batch_id=batch_id,
            user_id=user.id,
        )
    except reverse_operations.ReverseOperationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    return reverse_operations.serialize_batch(db, batch, include_items=True)


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
    if body.project_id is not None and not feature_enabled("projects_enabled"):
        raise HTTPException(404, "not found")
    lock_key, lock_token = _acquire_reverse_quote_lock(user.id, body.quote_id)
    try:
        try:
            replay = reverse_operations.find_quoted_idempotent_operation(
                db,
                user_id=user.id,
                body=body,
            )
        except reverse_operations.ReverseOperationConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except reverse_operations.ReverseOperationInvalid as exc:
            raise HTTPException(400, str(exc)) from exc
        if replay is not None:
            return reverse_operations.serialize_operation(replay)

        if body.project_id is not None:
            try:
                project_collection.require_owned_project(db, user.id, body.project_id)
            except project_collection.ProjectNotFound as exc:
                raise HTTPException(404, str(exc)) from exc
        for source in body.sources:
            _validate_reverse_asset_request(
                db,
                asset_url=source.asset_url,
                fallback_image=body.fallback_image if source.role == "primary" else None,
                target=body.target,
                source_type=source.source_type,
            )
        _validate_local_video_selection(body, db, user)
        if not get_setting(db, "reverse_prompt_enabled", True):
            raise HTTPException(403, "反推功能已被管理员关闭")
        _rate_limit(user.id)
        try:
            operation, created = reverse_operations.create_quoted_operation(
                db,
                user_id=user.id,
                body=body,
            )
        except reverse_operations.ReverseOperationConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except reverse_operations.ReverseOperationInvalid as exc:
            raise HTTPException(400, str(exc)) from exc
        except project_collection.ProjectNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except credits.InsufficientCredits as exc:
            raise HTTPException(400, str(exc)) from exc
    finally:
        locks.release(lock_key, lock_token)
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
    target: str | None = None,
    source_type: str | None = None,
    analysis_focus: str | None = None,
    include_audio: bool | None = None,
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
        target=target,
        source_type=source_type,
        analysis_focus=analysis_focus,
        include_audio=include_audio,
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


@router.get(
    "/reverse-operations/{operation_id}/revisions",
    response_model=list[ReverseResultRevisionOut],
)
def list_reverse_operation_revisions(
    operation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return reverse_operations.list_result_revisions(
            db, operation_id=operation_id, user_id=user.id
        )
    except reverse_operations.ReverseOperationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post(
    "/reverse-operations/{operation_id}/revisions",
    response_model=ReverseResultRevisionOut,
)
def create_reverse_operation_revision(
    operation_id: int,
    body: ReverseResultRevisionIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if body.source not in _CLIENT_REVISION_SOURCES:
        raise HTTPException(422, "客户端只能创建 user_edit 或 applied 反推结果版本")
    try:
        operation = reverse_operations.get_owned_operation(db, operation_id, user.id)
        validate_saved_reviewed_image_evidence(
            operation,
            body.payload,
            require_all_confirmed=body.source == "applied",
        )
    except reverse_operations.ReverseOperationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except ReviewedEvidenceMaskError as exc:
        raise HTTPException(422, f"图片证据审阅数据无效：{exc}") from exc
    _assert_text_allowed(db, body.payload)
    try:
        return reverse_operations.create_result_revision(
            db,
            operation_id=operation_id,
            user_id=user.id,
            source=body.source,
            payload=body.payload,
            parent_revision_id=body.parent_revision_id,
            clear_image_evidence=body.clear_image_evidence,
        )
    except reverse_operations.ReverseOperationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except reverse_operations.ReverseOperationConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except reverse_operations.ReverseOperationInvalid as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post(
    "/reverse-operations/{operation_id}/apply",
    response_model=ReverseResultApplyOut,
)
def apply_reverse_operation_result(
    operation_id: int,
    body: ReverseResultApplyIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _assert_text_allowed(db, body.payload)
    try:
        edited, applied = reverse_operations.apply_result_revision(
            db,
            operation_id=operation_id,
            user_id=user.id,
            payload=body.payload,
            parent_revision_id=body.parent_revision_id,
            clear_image_evidence=body.clear_image_evidence,
        )
        return {"user_edit": edited, "applied": applied}
    except reverse_operations.ReverseOperationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except reverse_operations.ReverseOperationConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except reverse_operations.ReverseOperationInvalid as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post(
    "/reverse-operations/{operation_id}/shots/edit",
    response_model=ReverseResultRevisionOut,
)
def edit_reverse_operation_shots(
    operation_id: int,
    body: ReverseShotTimelineEditIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return reverse_operations.edit_shot_timeline(
            db, operation_id=operation_id, user_id=user.id, body=body,
        )
    except reverse_operations.ReverseOperationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except reverse_operations.ReverseOperationConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except reverse_operations.ReverseOperationInvalid as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post(
    "/reverse-operations/{operation_id}/shots/reanalyze",
    response_model=ReverseOperationOut,
    status_code=202,
)
def reanalyze_reverse_operation_shot(
    operation_id: int,
    body: ReverseShotReanalyzeIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        _rate_limit(user.id)
        operation_body, reanalysis_context = reverse_operations.shot_reanalysis_body(
            db,
            operation_id=operation_id,
            user_id=user.id,
            client_request_id=body.client_request_id,
            shot_id=body.shot_id,
            analysis_precision=body.analysis_precision,
            include_audio=body.include_audio,
        )
        operation, created = reverse_operations.create_operation(
            db, user_id=user.id, body=operation_body,
        )
        operation = reverse_operations.attach_shot_reanalysis_context(
            db,
            operation=operation,
            user_id=user.id,
            context=reanalysis_context,
        )
        if created:
            try:
                reverse_operations.enqueue_operation(int(operation.id))
            except Exception as exc:  # noqa: BLE001
                if reverse_operations.fail_queued_submission(
                    int(operation.id),
                    code="BROKER_UNAVAILABLE",
                    error="单镜头重分析任务队列暂时不可用,已退回冻结积分",
                ):
                    raise HTTPException(503, "单镜头重分析任务队列暂时不可用") from exc
                db.expire_all()
                operation = db.get(ReverseOperation, int(operation.id)) or operation
        return reverse_operations.serialize_operation(operation)
    except reverse_operations.ReverseOperationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except reverse_operations.ReverseOperationConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except reverse_operations.ReverseOperationInvalid as exc:
        raise HTTPException(422, str(exc)) from exc
    except credits.InsufficientCredits as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post(
    "/reverse-operations/{operation_id}/shots/prepare-generation",
    response_model=ReverseShotGenerationPrepareOut,
)
def prepare_reverse_operation_shot_generation(
    operation_id: int,
    body: ReverseShotGenerationPrepareIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return reverse_operations.prepare_shot_generation(
            db,
            operation_id=operation_id,
            user_id=user.id,
            shot_id=body.shot_id,
            revision_id=body.revision_id,
            client_request_id=body.client_request_id,
            model_config_id=body.model_config_id,
            params=body.params,
        )
    except reverse_operations.ReverseOperationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except reverse_operations.ReverseOperationConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except reverse_operations.ReverseOperationInvalid as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get(
    "/reverse-operations/{operation_id}/feedback",
    response_model=ReverseOperationFeedbackOut | None,
)
def get_reverse_operation_feedback(
    operation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return reverse_operations.get_feedback(
            db,
            operation_id=operation_id,
            user_id=user.id,
        )
    except reverse_operations.ReverseOperationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc


@router.put(
    "/reverse-operations/{operation_id}/feedback",
    response_model=ReverseOperationFeedbackOut,
)
def save_reverse_operation_feedback(
    operation_id: int,
    body: ReverseOperationFeedbackIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _assert_text_allowed(db, body.note)
    try:
        return reverse_operations.upsert_feedback(
            db,
            operation_id=operation_id,
            user_id=user.id,
            rating=body.rating,
            issue_types=body.issue_types,
            note=body.note,
        )
    except reverse_operations.ReverseOperationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except reverse_operations.ReverseOperationConflict as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post(
    "/reverse-operations/{operation_id}/retry",
    response_model=ReverseOperationOut,
    status_code=202,
)
def retry_reverse_operation(
    operation_id: int,
    body: ReverseOperationRetryIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    lock_key, lock_token = _acquire_reverse_quote_lock(user.id, body.quote_id)
    try:
        previous, retry_body = reverse_operations.build_retry_operation_body(
            db,
            operation_id=operation_id,
            user_id=user.id,
            client_request_id=body.client_request_id,
            model_config_id=body.model_config_id,
            quote_id=body.quote_id,
        )
        replay = reverse_operations.find_quoted_idempotent_operation(
            db,
            user_id=user.id,
            body=retry_body,
            retry_of_operation_id=int(previous.id),
        )
        if replay is not None:
            return reverse_operations.serialize_operation(replay)
        context = dict(previous.request_context or {})
        sources = context.get("sources") if isinstance(context.get("sources"), list) else []
        if sources:
            for source in sources:
                _validate_reverse_asset_request(
                    db,
                    asset_url=str(source.get("asset_url") or ""),
                    fallback_image=(
                        context.get("fallback_image") if source.get("role") == "primary" else None
                    ),
                    target=previous.target,
                    source_type=source.get("source_type"),
                )
        else:
            _validate_reverse_asset_request(
                db,
                asset_url=previous.asset_url,
                fallback_image=context.get("fallback_image"),
                target=previous.target,
                source_type=context.get("source_type"),
            )
        operation, created = reverse_operations.retry_operation(
            db,
            operation_id=operation_id,
            user_id=user.id,
            client_request_id=body.client_request_id,
            quote_id=body.quote_id,
            model_config_id=body.model_config_id,
        )
    except reverse_operations.ReverseOperationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except reverse_operations.ReverseOperationConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except reverse_operations.ReverseOperationInvalid as exc:
        raise HTTPException(400, str(exc)) from exc
    except project_collection.ProjectNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except credits.InsufficientCredits as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        locks.release(lock_key, lock_token)
    if created:
        try:
            reverse_operations.enqueue_operation(int(operation.id))
        except Exception as exc:  # noqa: BLE001
            log.exception("failed to enqueue reverse retry %s", operation.id)
            if reverse_operations.fail_queued_submission(
                int(operation.id),
                code="BROKER_UNAVAILABLE",
                error="反推任务队列暂时不可用,已退回冻结积分",
            ):
                raise HTTPException(503, "反推任务队列暂时不可用,请稍后重试") from exc
    db.expire_all()
    return reverse_operations.serialize_operation(
        db.get(ReverseOperation, int(operation.id)) or operation
    )


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
    if body.project_id is not None and not feature_enabled("projects_enabled"):
        raise HTTPException(404, "not found")
    if body.project_id is not None:
        try:
            project_collection.require_owned_project(db, user.id, body.project_id)
        except project_collection.ProjectNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
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
    _validate_local_video_selection(body, db, user)
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
    except project_collection.ProjectNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
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
