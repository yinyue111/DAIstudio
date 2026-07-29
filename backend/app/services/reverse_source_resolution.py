"""Resolve reverse-analysis inputs without depending on the HTTP router layer."""
from __future__ import annotations

import base64
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..config import settings
from ..models import User
from ..schemas import ReverseIn, ReverseOperationCreate
from . import asset_refs, reverse_lineage, storage, video_frames

log = logging.getLogger("reverse_source_resolution")

VIDEO_EXTS = (".mp4", ".webm", ".mov")
UNSUPPORTED_VIDEO_EXTS = (".m3u8",)
IMAGE_REFERENCE_MAX_SIDE = 1024
IMAGE_REFERENCE_QUALITY = 92

GatewayRefResolver = Callable[[Session, User, str | None], str | None]
GatewayRefWithHashResolver = Callable[
    [Session, User, str | None], tuple[str | None, str | None]
]


def looks_like_video_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(VIDEO_EXTS + UNSUPPORTED_VIDEO_EXTS)


def is_unsupported_video_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(UNSUPPORTED_VIDEO_EXTS)


def is_video_source(body: ReverseIn | ReverseOperationCreate) -> bool:
    return body.source_type == "video" or looks_like_video_url(str(body.asset_url or ""))


def reverse_sources(body: ReverseIn | ReverseOperationCreate) -> list[dict]:
    sources = getattr(body, "sources", None) or []
    if sources:
        return [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
            for item in sources
        ]
    return [
        {
            "asset_url": str(body.asset_url or ""),
            "source_type": body.source_type or (
                "video" if is_video_source(body) else "image"
            ),
            "role": "primary",
        }
    ]


def gateway_ref(db: Session, user: User, url: str | None) -> str | None:
    try:
        return asset_refs.gateway_ref_for_user_asset(
            db,
            user.id,
            url,
            max_side=IMAGE_REFERENCE_MAX_SIDE,
            prefer_original_upload=True,
            quality=IMAGE_REFERENCE_QUALITY,
            subsampling=0,
        )
    except asset_refs.AssetRefError as exc:
        raise HTTPException(404, str(exc)) from exc


def gateway_ref_with_content_hash(
    db: Session,
    user: User,
    url: str | None,
) -> tuple[str | None, str | None]:
    try:
        resolved = asset_refs.gateway_ref_for_user_asset(
            db,
            user.id,
            url,
            max_side=IMAGE_REFERENCE_MAX_SIDE,
            prefer_original_upload=True,
            quality=IMAGE_REFERENCE_QUALITY,
            subsampling=0,
            return_content_hash=True,
        )
    except asset_refs.AssetRefError as exc:
        raise HTTPException(404, str(exc)) from exc
    if resolved is None:
        return None, None
    if isinstance(resolved, tuple):
        return resolved
    return resolved, reverse_lineage.data_uri_content_hash(resolved)


def video_selection_payload(body: Any) -> tuple[list[dict], float, Any, list[Any]]:
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


def video_duration_for_reverse(
    body: ReverseIn | ReverseOperationCreate,
    db: Session,
    user: User,
) -> float | None:
    if not is_video_source(body):
        return None
    source_ranges = list(getattr(body, "source_ranges", None) or [])
    if source_ranges:
        return sum(
            max(0.0, float(item.end_seconds) - float(item.start_seconds))
            for item in source_ranges
        )
    source_range = getattr(body, "source_range", None)
    if source_range is not None:
        return max(
            0.0,
            float(source_range.end_seconds) - float(source_range.start_seconds),
        )
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


def _append_image_sources(
    body: ReverseIn | ReverseOperationCreate,
    db: Session,
    user: User,
    *,
    sources: list[dict] | None = None,
    include_source_fingerprints: bool,
    gateway_ref_resolver: GatewayRefResolver,
    gateway_ref_with_hash_resolver: GatewayRefWithHashResolver,
    default_role: str = "primary",
) -> tuple[list[str], list[dict], list[dict]]:
    refs: list[str] = []
    reference_context: list[dict] = []
    source_fingerprints: list[dict] = []
    for source in sources if sources is not None else reverse_sources(body):
        if include_source_fingerprints:
            ref, content_hash = gateway_ref_with_hash_resolver(
                db,
                user,
                source.get("asset_url"),
            )
        else:
            ref = gateway_ref_resolver(db, user, source.get("asset_url"))
            content_hash = None
        if not ref:
            continue
        refs.append(ref)
        if content_hash:
            source_fingerprints.append(
                reverse_lineage.build_source_fingerprint(
                    source_index=len(refs),
                    content_hash=content_hash,
                    locator=source.get("asset_url"),
                )
            )
        reference_context.append(
            {
                "role": source.get("role") or default_role,
                "label": source.get("label"),
                "source_type": "image",
            }
        )
    return refs, reference_context, source_fingerprints


def _sample_video(
    body: ReverseIn | ReverseOperationCreate,
    *,
    local_video_path: Any,
    frame_budget: int | None,
    video_preset: str | None,
    gateway_mock: bool | None,
) -> tuple[Any, list[dict]]:
    effective_gateway_mock = (
        settings.effective_mock_mode if gateway_mock is None else gateway_mock
    )
    source_ranges, range_start, range_end, custom_timestamps = video_selection_payload(
        body
    )
    if not video_frames.available() or (
        local_video_path is None and effective_gateway_mock
    ):
        return None, source_ranges
    sample_kwargs: dict[str, Any] = {
        "n": max(1, int(frame_budget or settings.reverse_video_frames or 1)),
        "preset": video_preset,
    }
    if source_ranges or custom_timestamps:
        sample_kwargs.update(
            {
                "start_seconds": range_start,
                "end_seconds": range_end,
                "source_ranges": source_ranges,
                "custom_timestamps": custom_timestamps,
            }
        )
    try:
        if local_video_path is not None:
            return (
                video_frames.sample_video_from_path(
                    str(local_video_path),
                    **sample_kwargs,
                ),
                source_ranges,
            )
        return (
            video_frames.sample_video(str(body.asset_url or ""), **sample_kwargs),
            source_ranges,
        )
    except asset_refs.AssetRefError as exc:
        raise HTTPException(404, str(exc)) from exc
    except video_frames.VideoSelectionError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.warning("video keyframe sampling failed, using cover fallback: %s", exc)
        return None, source_ranges


def _sampled_video_result(
    body: ReverseIn | ReverseOperationCreate,
    db: Session,
    user: User,
    *,
    sample: Any,
    source_ranges: list[dict],
    include_source_fingerprints: bool,
    gateway_ref_resolver: GatewayRefResolver,
    gateway_ref_with_hash_resolver: GatewayRefWithHashResolver,
) -> tuple[list[str], dict, list[dict]]:
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
        {"role": "frame", "source_type": "video", **row}
        for row in analysis.get("sampled_frames") or []
    ]
    extra_refs, extra_context, extra_fingerprints = _append_image_sources(
        body,
        db,
        user,
        sources=reverse_sources(body)[1:],
        include_source_fingerprints=include_source_fingerprints,
        gateway_ref_resolver=gateway_ref_resolver,
        gateway_ref_with_hash_resolver=gateway_ref_with_hash_resolver,
        default_role="style",
    )
    for fingerprint in extra_fingerprints:
        fingerprint["source_index"] = len(refs) + int(
            fingerprint.get("source_index") or 0
        )
    refs.extend(extra_refs)
    reference_context.extend(extra_context)
    source_fingerprints.extend(extra_fingerprints)
    analysis["reference_context"] = reference_context
    return refs, analysis, source_fingerprints


def _cover_fallback_result(
    body: ReverseIn | ReverseOperationCreate,
    db: Session,
    user: User,
    *,
    sample: Any,
    local_video_path: Any,
    include_source_fingerprints: bool,
    gateway_ref_resolver: GatewayRefResolver,
    gateway_ref_with_hash_resolver: GatewayRefWithHashResolver,
) -> tuple[list[str], dict, list[dict]]:
    cover = body.fallback_image
    if not cover or looks_like_video_url(cover):
        raise HTTPException(400, "无法从该视频抽取关键帧,请改用封面图进行反推")
    if include_source_fingerprints:
        ref, content_hash = gateway_ref_with_hash_resolver(db, user, cover)
    else:
        ref = gateway_ref_resolver(db, user, cover)
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
    analysis.update(
        {
            "analysis_mode": "cover_fallback",
            "degraded_reason": "未能从源视频抽取可用关键帧，已降级为封面单帧分析",
        }
    )
    refs = [ref] if ref else []
    fingerprints = (
        [
            reverse_lineage.build_source_fingerprint(
                source_index=1,
                content_hash=content_hash,
                locator=cover,
            )
        ]
        if ref and content_hash
        else []
    )
    return refs, analysis, fingerprints


def collect_refs(
    body: ReverseIn | ReverseOperationCreate,
    db: Session,
    user: User,
    *,
    frame_budget: int | None = None,
    video_preset: str | None = None,
    gateway_mock: bool | None = None,
    include_source_fingerprints: bool = False,
    gateway_ref_resolver: GatewayRefResolver = gateway_ref,
    gateway_ref_with_hash_resolver: GatewayRefWithHashResolver = (
        gateway_ref_with_content_hash
    ),
) -> tuple[list[str], dict | None] | tuple[list[str], dict | None, list[dict]]:
    """Resolve an owned asset into the image references sent to the vision model."""
    if not is_video_source(body):
        refs, reference_context, fingerprints = _append_image_sources(
            body,
            db,
            user,
            include_source_fingerprints=include_source_fingerprints,
            gateway_ref_resolver=gateway_ref_resolver,
            gateway_ref_with_hash_resolver=gateway_ref_with_hash_resolver,
        )
        analysis = {"reference_context": reference_context} if len(refs) > 1 else None
    else:
        key = storage.key_from_url(str(body.asset_url or ""))
        local_video_path = None
        if key:
            try:
                local_video_path = asset_refs.generated_video_reference_path(
                    db,
                    user.id,
                    key,
                )
            except asset_refs.AssetRefError as exc:
                raise HTTPException(404, str(exc)) from exc
        sample, source_ranges = _sample_video(
            body,
            local_video_path=local_video_path,
            frame_budget=frame_budget,
            video_preset=video_preset,
            gateway_mock=gateway_mock,
        )
        if sample and sample.frames:
            refs, analysis, fingerprints = _sampled_video_result(
                body,
                db,
                user,
                sample=sample,
                source_ranges=source_ranges,
                include_source_fingerprints=include_source_fingerprints,
                gateway_ref_resolver=gateway_ref_resolver,
                gateway_ref_with_hash_resolver=gateway_ref_with_hash_resolver,
            )
        else:
            refs, analysis, fingerprints = _cover_fallback_result(
                body,
                db,
                user,
                sample=sample,
                local_video_path=local_video_path,
                include_source_fingerprints=include_source_fingerprints,
                gateway_ref_resolver=gateway_ref_resolver,
                gateway_ref_with_hash_resolver=gateway_ref_with_hash_resolver,
            )
    if include_source_fingerprints:
        return refs, analysis, fingerprints
    return refs, analysis
