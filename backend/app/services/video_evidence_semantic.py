"""Semantic-provider contract, normalization, and health checks for video evidence."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import threading
import time
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from PIL import Image

from ..config import settings
from . import gateway

SEMANTIC_CONTRACT_VERSION = "video-semantic-evidence.v1"
SEMANTIC_CAPABILITIES = ("subject_tracking", "pose", "action", "transition")
_SEMANTIC_EVIDENCE_KEYS = {
    "subject_tracking": "tracks",
    "pose": "observations",
    "action": "events",
    "transition": "events",
}
_SEMANTIC_EVIDENCE_PREFIXES = {
    "subject_tracking": "subject-track-",
    "pose": "pose-",
    "action": "action-",
    "transition": "transition-",
}
_PROVIDER_LAST_SUCCESS: dict[str, Any] = {}
_PROVIDER_HEALTH_CACHE: dict[str, tuple[str, float, dict[str, Any]]] = {}
_PROVIDER_STATE_LOCK = threading.RLock()
_PROVIDER_HEALTH_PROBE_LOCK = threading.Lock()
_MAX_SEMANTIC_FRAMES = 64

_finite_number: Callable[..., float]
_positive_index: Callable[..., int]
_provider_label: Callable[..., str]
_provider_reason: Callable[..., str | None]
_frame_lookup: Callable[..., dict[tuple[int, int], dict[str, Any]]]
_segment_bounds: Callable[..., dict[int, tuple[float, float]]]
_validate_subject_tracks: Callable[..., Any]
_validate_pose_observations: Callable[..., Any]
_validate_action_events: Callable[..., Any]
_validate_transition_events: Callable[..., Any]


def _bind_contract_helpers(
    *,
    finite_number: Callable[..., float],
    positive_index: Callable[..., int],
    provider_label: Callable[..., str],
    provider_reason: Callable[..., str | None],
    frame_lookup: Callable[..., dict[tuple[int, int], dict[str, Any]]],
    segment_bounds: Callable[..., dict[int, tuple[float, float]]],
    validate_subject_tracks: Callable[..., Any],
    validate_pose_observations: Callable[..., Any],
    validate_action_events: Callable[..., Any],
    validate_transition_events: Callable[..., Any],
) -> None:
    """Bind contract validators owned by the legacy evidence facade."""
    global _finite_number
    global _frame_lookup
    global _positive_index
    global _provider_label
    global _provider_reason
    global _segment_bounds
    global _validate_action_events
    global _validate_pose_observations
    global _validate_subject_tracks
    global _validate_transition_events

    _finite_number = finite_number
    _positive_index = positive_index
    _provider_label = provider_label
    _provider_reason = provider_reason
    _frame_lookup = frame_lookup
    _segment_bounds = segment_bounds
    _validate_subject_tracks = validate_subject_tracks
    _validate_pose_observations = validate_pose_observations
    _validate_action_events = validate_action_events
    _validate_transition_events = validate_transition_events


def _semantic_runtime_config() -> dict[str, Any]:
    return {
        "url": str(settings.video_evidence_semantic_url or "").strip(),
        "api_key": str(settings.video_evidence_semantic_api_key or "").strip(),
        "timeout_seconds": settings.video_evidence_semantic_timeout_seconds,
        "health_url": str(settings.video_evidence_semantic_health_url or "").strip(),
        "health_timeout_seconds": settings.video_evidence_semantic_health_timeout_seconds,
    }


def _semantic_capability_status(
    capability: str,
    status: str,
    *,
    analyzer: str,
    analyzer_version: str,
    evidence: list[dict[str, Any]] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    rows = evidence or []
    return {
        "status": status,
        "analyzer": analyzer,
        "analyzer_version": analyzer_version,
        "evidence_count": len(rows),
        "degraded_reason": reason,
        _SEMANTIC_EVIDENCE_KEYS[capability]: rows,
    }


def _semantic_failure(status: str, reason: str) -> dict[str, Any]:
    analyzer = "http_video_semantic_provider"
    return {
        "contract_version": SEMANTIC_CONTRACT_VERSION,
        **{
            capability: _semantic_capability_status(
                capability,
                status,
                analyzer=analyzer,
                analyzer_version="unconfigured" if status == "unsupported" else "unknown",
                reason=reason,
            )
            for capability in SEMANTIC_CAPABILITIES
        },
    }


def _normalize_semantic_provider_response(
    payload: Any,
    *,
    frames: list[dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("semantic provider response must be an object")
    if payload.get("contract_version") != SEMANTIC_CONTRACT_VERSION:
        raise ValueError(f"semantic provider must return {SEMANTIC_CONTRACT_VERSION}")
    analyzer = _provider_label(
        payload.get("analyzer"), path="semantic provider response.analyzer"
    )
    analyzer_version = _provider_label(
        payload.get("analyzer_version"), path="semantic provider response.analyzer_version"
    )
    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, dict):
        raise ValueError("semantic provider response.capabilities must be an object")
    if set(capabilities) != set(SEMANTIC_CAPABILITIES):
        raise ValueError(
            "semantic provider response.capabilities must contain exactly "
            + ", ".join(SEMANTIC_CAPABILITIES)
        )
    lookup = _frame_lookup(frames)
    if not lookup:
        raise ValueError("semantic analysis requires at least one decoded sampled frame")
    bounds = _segment_bounds(lookup)
    normalized: dict[str, Any] = {"contract_version": SEMANTIC_CONTRACT_VERSION}
    subjects: dict[str, dict[str, Any]] = {}
    used_provider_ids: set[str] = set()

    for capability in SEMANTIC_CAPABILITIES:
        raw = capabilities.get(capability)
        try:
            if not isinstance(raw, dict):
                raise ValueError(f"capabilities.{capability} must be an object")
            status = raw.get("status")
            if status not in {"analyzed", "partial", "unsupported", "degraded"}:
                raise ValueError(f"capabilities.{capability}.status is invalid")
            evidence = raw.get("evidence")
            reason = _provider_reason(
                raw.get("degraded_reason"),
                path=f"capabilities.{capability}.degraded_reason",
                required=status in {"partial", "unsupported", "degraded"},
            )
            if status in {"unsupported", "degraded"}:
                if evidence != []:
                    raise ValueError(
                        f"capabilities.{capability} cannot return evidence with status={status}"
                    )
                normalized[capability] = _semantic_capability_status(
                    capability,
                    str(status),
                    analyzer=analyzer,
                    analyzer_version=analyzer_version,
                    reason=reason,
                )
                continue
            if status == "analyzed" and reason:
                raise ValueError(
                    f"capabilities.{capability} analyzed response cannot include degraded_reason"
                )
            if capability == "subject_tracking":
                rows, validated_subjects, provider_ids = _validate_subject_tracks(
                    evidence,
                    lookup=lookup,
                    bounds=bounds,
                )
                subjects = validated_subjects
            elif capability == "pose":
                rows, provider_ids = _validate_pose_observations(
                    evidence,
                    lookup=lookup,
                    bounds=bounds,
                    subjects=subjects,
                )
            elif capability == "action":
                rows, provider_ids = _validate_action_events(
                    evidence,
                    lookup=lookup,
                    bounds=bounds,
                    subjects=subjects,
                )
            else:
                rows, provider_ids = _validate_transition_events(
                    evidence,
                    lookup=lookup,
                    bounds=bounds,
                )
            duplicate_ids = used_provider_ids & provider_ids
            if duplicate_ids:
                raise ValueError(
                    f"capabilities.{capability} reused provider evidence ids: "
                    + ", ".join(sorted(duplicate_ids)[:3])
                )
            used_provider_ids.update(provider_ids)
            effective_status = str(status)
            if not rows:
                effective_status = "partial"
                reason = reason or "provider 未返回可验证的独立证据"
            normalized[capability] = _semantic_capability_status(
                capability,
                effective_status,
                analyzer=analyzer,
                analyzer_version=analyzer_version,
                evidence=rows,
                reason=reason,
            )
        except Exception as exc:  # noqa: BLE001
            if capability == "subject_tracking":
                subjects = {}
            normalized[capability] = _semantic_capability_status(
                capability,
                "degraded",
                analyzer=analyzer,
                analyzer_version=analyzer_version,
                reason=str(exc)[:200],
            )
    return normalized


def _record_semantic_provider_success(result: dict[str, Any], *, source: str) -> None:
    analyzer = "http_video_semantic_provider"
    analyzer_version = "unknown"
    capability_statuses: dict[str, str] = {}
    for capability in SEMANTIC_CAPABILITIES:
        status = result.get(capability)
        if not isinstance(status, dict):
            continue
        analyzer = str(status.get("analyzer") or analyzer)
        analyzer_version = str(status.get("analyzer_version") or analyzer_version)
        capability_statuses[capability] = str(status.get("status") or "degraded")
    record = {
        "last_success_at": datetime.now(timezone.utc).isoformat(),
        "last_success_source": source,
        "last_analyzer": analyzer,
        "last_analyzer_version": analyzer_version,
        "last_capability_statuses": capability_statuses,
    }
    with _PROVIDER_STATE_LOCK:
        _PROVIDER_LAST_SUCCESS.clear()
        _PROVIDER_LAST_SUCCESS.update(record)
        _PROVIDER_HEALTH_CACHE.clear()


def http_semantic_provider(
    images: list[Image.Image],
    frames: list[dict[str, Any]],
) -> dict[str, Any]:
    """Call the configured video semantic analyzer with a bounded strict contract."""
    config = _semantic_runtime_config()
    url = config["url"]
    api_key = config["api_key"]
    if not url:
        return _semantic_failure("unsupported", "video semantic provider 未配置")
    if not images or len(images) != len(frames):
        return _semantic_failure("degraded", "video semantic provider 缺少对应的抽样帧")
    if len(images) > _MAX_SEMANTIC_FRAMES:
        return _semantic_failure("degraded", "video semantic provider 单次最多接收 64 个抽样帧")
    try:
        timeout = min(120, max(1, int(config["timeout_seconds"])))
    except (TypeError, ValueError):
        return _semantic_failure("degraded", "video semantic provider timeout 配置无效")
    request_frames: list[dict[str, Any]] = []
    try:
        for image, frame in zip(images, frames, strict=True):
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            frame_index = _positive_index(
                frame.get("frame_index"), path="sampled_frames[].frame_index"
            )
            segment = _positive_index(
                frame.get("source_segment_index"),
                path="sampled_frames[].source_segment_index",
            )
            timestamp = _finite_number(
                frame.get("timestamp_seconds"),
                path="sampled_frames[].absolute_timestamp_seconds",
                minimum=0,
            )
            request_frames.append({
                "frame_index": frame_index,
                "absolute_timestamp_seconds": round(timestamp, 3),
                "source_segment_index": segment,
                "source_content_hash": str(frame.get("source_content_hash") or ""),
                "width": image.width,
                "height": image.height,
                "image_media_type": "image/png",
                "image_base64": base64.b64encode(buffer.getvalue()).decode("ascii"),
            })
        payload = gateway._request_json(
            "POST",
            url,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            payload={
                "contract_version": SEMANTIC_CONTRACT_VERSION,
                "capabilities": list(SEMANTIC_CAPABILITIES),
                "frames": request_frames,
            },
            timeout=timeout,
            retries=0,
            trusted_hosts=settings.trusted_analyzer_host_list,
        )
        result = _normalize_semantic_provider_response(payload, frames=frames)
        if any(
            result[capability].get("status") in {"analyzed", "partial"}
            and int(result[capability].get("evidence_count") or 0) > 0
            for capability in SEMANTIC_CAPABILITIES
        ):
            _record_semantic_provider_success(result, source="analysis")
        return result
    except Exception as exc:  # noqa: BLE001
        return _semantic_failure("degraded", str(exc)[:200])


def _last_success_record() -> dict[str, Any]:
    with _PROVIDER_STATE_LOCK:
        return deepcopy(_PROVIDER_LAST_SUCCESS)


def _provider_health_cache_key(config: dict[str, Any]) -> str:
    payload = {
        "url": config["url"],
        "health_url": config["health_url"],
        "api_key_sha256": hashlib.sha256(config["api_key"].encode()).hexdigest(),
        "timeout": config["health_timeout_seconds"],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _cached_health_result(cache_key: str) -> dict[str, Any] | None:
    now = time.monotonic()
    with _PROVIDER_STATE_LOCK:
        cached = _PROVIDER_HEALTH_CACHE.get("semantic")
        if cached and cached[0] == cache_key and cached[1] > now:
            return deepcopy(cached[2])
        if cached:
            _PROVIDER_HEALTH_CACHE.pop("semantic", None)
    return None


def _probe_provider_health(
    config: dict[str, Any],
    *,
    last_success: dict[str, Any],
) -> dict[str, Any]:
    analyzer = "http_video_semantic_provider"
    try:
        timeout = min(10, max(1, int(config["health_timeout_seconds"])))
        payload = gateway._request_json(
            "GET",
            config["health_url"],
            headers={"Authorization": f"Bearer {config['api_key']}"}
            if config["api_key"]
            else {},
            payload=None,
            timeout=timeout,
            retries=0,
            trusted_hosts=settings.trusted_analyzer_host_list,
        )
        if not isinstance(payload, dict):
            raise ValueError("health response must be an object")
        if payload.get("contract_version") != SEMANTIC_CONTRACT_VERSION:
            raise ValueError(
                f"health response.contract_version must be {SEMANTIC_CONTRACT_VERSION}"
            )
        health_status = str(payload.get("status") or "").strip().lower()
        if payload.get("ok") is not True and health_status not in {
            "ok", "healthy", "available", "ready",
        }:
            raise ValueError("health response did not report ready")
        provider_analyzer = _provider_label(
            payload.get("analyzer"), path="health response.analyzer"
        )
        version = _provider_label(
            payload.get("analyzer_version"), path="health response.analyzer_version"
        )
        capability_statuses = payload.get("capability_statuses")
        if not isinstance(capability_statuses, dict):
            raise ValueError("health response.capability_statuses must be an object")
        if set(capability_statuses) != set(SEMANTIC_CAPABILITIES) or any(
            value not in {"available", "degraded", "unsupported"}
            for value in capability_statuses.values()
        ):
            raise ValueError(
                "health response.capability_statuses must report every semantic capability"
            )
        success = {
            "last_success_at": datetime.now(timezone.utc).isoformat(),
            "last_success_source": "health_probe",
            "last_analyzer": provider_analyzer,
            "last_analyzer_version": version,
            "last_capability_statuses": dict(capability_statuses),
        }
        with _PROVIDER_STATE_LOCK:
            _PROVIDER_LAST_SUCCESS.clear()
            _PROVIDER_LAST_SUCCESS.update(deepcopy(success))
        return {
            "status": "available",
            "analyzer": provider_analyzer,
            "analyzer_version": version,
            "configured": True,
            "verification_status": "health_probe",
            "health_url_configured": True,
            "degraded_reason": None,
            **success,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "degraded",
            "analyzer": analyzer,
            "configured": True,
            "verification_status": "health_probe_failed",
            "health_url_configured": True,
            "degraded_reason": str(exc)[:200],
            **last_success,
        }


def _cache_health_result(cache_key: str, result: dict[str, Any]) -> None:
    try:
        ttl = min(
            60,
            max(1, int(settings.video_evidence_semantic_health_cache_ttl_seconds)),
        )
    except (TypeError, ValueError):
        ttl = 15
    with _PROVIDER_STATE_LOCK:
        _PROVIDER_HEALTH_CACHE["semantic"] = (
            cache_key,
            time.monotonic() + ttl,
            deepcopy(result),
        )


def _semantic_provider_health() -> dict[str, Any]:
    config = _semantic_runtime_config()
    analyzer = "http_video_semantic_provider"
    if not config["url"]:
        return {
            "status": "unsupported",
            "analyzer": analyzer,
            "configured": False,
            "verification_status": "unconfigured",
            "health_url_configured": False,
            "degraded_reason": "video semantic provider 未配置",
        }
    last_success = _last_success_record()
    if not config["health_url"]:
        if last_success:
            return {
                "status": "available",
                "analyzer": analyzer,
                "configured": True,
                "verification_status": "last_success",
                "health_url_configured": False,
                "degraded_reason": None,
                **last_success,
            }
        return {
            "status": "degraded",
            "analyzer": analyzer,
            "configured": True,
            "verification_status": "configured_unverified",
            "health_url_configured": False,
            "degraded_reason": "provider 已配置，但尚无 health probe 或成功分析记录",
        }
    cache_key = _provider_health_cache_key(config)
    cached = _cached_health_result(cache_key)
    if cached is not None:
        return cached
    with _PROVIDER_HEALTH_PROBE_LOCK:
        cached = _cached_health_result(cache_key)
        if cached is not None:
            return cached
        result = _probe_provider_health(config, last_success=_last_success_record())
        _cache_health_result(cache_key, result)
        return result
