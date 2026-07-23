"""Independent, source-addressable image evidence analyzers.

The vision model is deliberately not used by this module. Optional local
analyzers report their availability and failures instead of synthesizing
evidence when they cannot run.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import math
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from PIL import Image, ImageChops, ImageStat

from ..config import settings
from . import gateway

CONTRACT_VERSION = "image-evidence.v1"
REGION_ANALYZER_CONTRACT_VERSION = "region-analyzer.v1"
TESSERACT = shutil.which("tesseract")
Analyzer = Callable[[Image.Image], dict[str, Any]]
_PROVIDER_LAST_SUCCESS: dict[str, dict[str, Any]] = {}
_PROVIDER_HEALTH_CACHE: dict[str, tuple[str, float, dict[str, Any]]] = {}
_PROVIDER_STATE_LOCK = threading.RLock()
_PROVIDER_HEALTH_PROBE_LOCKS = {
    "detector": threading.Lock(),
    "segmenter": threading.Lock(),
}
_REGION_CAPABILITIES = frozenset(_PROVIDER_HEALTH_PROBE_LOCKS)
_REGION_PROVIDER_STATUSES = frozenset({"analyzed", "degraded", "unsupported"})
_TESSERACT_LANGUAGE_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$", flags=re.ASCII)
_MAX_TESSERACT_LANGUAGES = 8
_MAX_TESSERACT_LANGUAGE_CHARS = 128


def _status(
    status: str,
    analyzer: str,
    version: str,
    *,
    evidence: list[dict[str, Any]] | None = None,
    reason: str | None = None,
    **metadata: Any,
) -> dict[str, Any]:
    rows = evidence or []
    return {
        "status": status,
        "analyzer": analyzer,
        "analyzer_version": version,
        "evidence_count": len(rows),
        "degraded_reason": reason,
        "evidence": rows,
        **metadata,
    }


def _decode_data_image(value: str) -> tuple[Image.Image, str]:
    header, encoded = str(value or "").split(",", 1)
    if not header.lower().startswith("data:image/") or ";base64" not in header.lower():
        raise ValueError("independent analyzers require an image data URI")
    raw = base64.b64decode(encoded, validate=True)
    image = Image.open(io.BytesIO(raw)).convert("RGB")
    image.load()
    return image, hashlib.sha256(raw).hexdigest()


def stable_evidence_id(row: dict[str, Any]) -> str:
    canonical = {
        key: row.get(key)
        for key in (
            "source_content_hash",
            "source_index",
            "analyzer_source",
            "field_key",
            "label",
            "evidence_text",
            "bbox",
            "polygon",
        )
    }
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "ev-" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def _bbox(x: int, y: int, width: int, height: int, image: Image.Image) -> dict[str, float]:
    return {
        "x": round(x / image.width, 6),
        "y": round(y / image.height, 6),
        "width": round(width / image.width, 6),
        "height": round(height / image.height, 6),
    }


def _tesseract_version() -> str | None:
    if not TESSERACT:
        return None
    try:
        return subprocess.run(
            [TESSERACT, "--version"], capture_output=True, timeout=5, check=False,
        ).stdout.decode("utf-8", errors="replace").splitlines()[0].strip()
    except (OSError, subprocess.SubprocessError, IndexError):
        return "unknown"


def _tesseract_languages() -> list[str]:
    if not TESSERACT:
        return []
    try:
        completed = subprocess.run(
            [TESSERACT, "--list-langs"], capture_output=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    return sorted({
        line.strip()
        for line in completed.stdout.decode("utf-8", errors="replace").splitlines()
        if line.strip() and not line.lower().startswith("list of available languages")
    })


def _configured_tesseract_languages() -> tuple[list[str], str | None]:
    raw = getattr(settings, "image_evidence_ocr_languages", "")
    if not isinstance(raw, str) or len(raw) > _MAX_TESSERACT_LANGUAGE_CHARS + _MAX_TESSERACT_LANGUAGES:
        return [], "IMAGE_EVIDENCE_OCR_LANGUAGES 配置无效"
    languages = [item.strip() for item in raw.replace(",", "+").split("+")]
    if (
        not languages
        or len(languages) > _MAX_TESSERACT_LANGUAGES
        or sum(len(item) for item in languages) > _MAX_TESSERACT_LANGUAGE_CHARS
        or any(not _TESSERACT_LANGUAGE_RE.fullmatch(item) for item in languages)
    ):
        return [], "IMAGE_EVIDENCE_OCR_LANGUAGES 必须是受限的 ASCII 语言列表"
    return list(dict.fromkeys(languages)), None


def _tesseract_language_metadata(languages: list[str]) -> dict[str, Any]:
    requested, configuration_error = _configured_tesseract_languages()
    missing = [language for language in requested if language not in languages]
    effective = requested if not configuration_error and not missing else []
    chinese_status = (
        "not_requested"
        if "chi_sim" not in requested
        else "available"
        if "chi_sim" in languages
        else "unsupported"
    )
    return {
        "languages": languages,
        "requested_languages": requested,
        "effective_languages": effective,
        "missing_languages": missing,
        "language_argument": "+".join(effective) or None,
        "configuration_error": configuration_error,
        "chinese_status": chinese_status,
    }


def _tesseract_language_reason(metadata: dict[str, Any]) -> str | None:
    if metadata.get("configuration_error"):
        return str(metadata["configuration_error"])
    missing = metadata.get("missing_languages")
    if isinstance(missing, list) and missing:
        return f"Tesseract 未安装配置语言: {'+'.join(map(str, missing))}"
    return None


def tesseract_ocr(image: Image.Image) -> dict[str, Any]:
    if not TESSERACT:
        return _status(
            "unsupported",
            "tesseract_tsv",
            "unavailable",
            reason="服务器未安装 Tesseract OCR",
            binary=None,
            **_tesseract_language_metadata([]),
        )
    version = _tesseract_version() or "unknown"
    languages = _tesseract_languages()
    language_metadata = _tesseract_language_metadata(languages)
    language_reason = _tesseract_language_reason(language_metadata)
    if language_reason:
        return _status(
            "degraded", "tesseract_tsv", version, reason=language_reason,
            binary=TESSERACT, **language_metadata,
        )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    try:
        completed = subprocess.run(
            [
                TESSERACT, "stdin", "stdout", "-l",
                str(language_metadata["language_argument"]), "--psm", "11", "tsv",
            ],
            input=buffer.getvalue(),
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _status(
            "degraded", "tesseract_tsv", version, reason=str(exc)[:200], binary=TESSERACT,
            **language_metadata,
        )
    if completed.returncode != 0:
        reason = completed.stderr.decode("utf-8", errors="replace").strip()
        return _status(
            "degraded", "tesseract_tsv", version, reason=reason[:200], binary=TESSERACT,
            **language_metadata,
        )
    rows: list[dict[str, Any]] = []
    text = completed.stdout.decode("utf-8", errors="replace")
    for item in csv.DictReader(io.StringIO(text), delimiter="\t"):
        value = str(item.get("text") or "").strip()
        try:
            confidence = float(item.get("conf") or -1) / 100
            x, y = int(item["left"]), int(item["top"])
            width, height = int(item["width"]), int(item["height"])
        except (KeyError, TypeError, ValueError):
            continue
        if not value or confidence < 0 or width <= 0 or height <= 0:
            continue
        rows.append({
            "evidence_type": "ocr",
            "field_key": "ocr_text",
            "label": "visible_text",
            "evidence_text": value[:2000],
            "bbox": _bbox(x, y, width, height, image),
            "confidence": round(min(1.0, confidence), 6),
            "fact_status": "visible",
        })
    return _status(
        "analyzed",
        "tesseract_tsv",
        version,
        evidence=rows,
        reason=None,
        binary=TESSERACT,
        **language_metadata,
    )


def pillow_region_segmentation(image: Image.Image) -> dict[str, Any]:
    """Produce a non-semantic region proposal from pixel contrast."""
    if image.width < 2 or image.height < 2:
        return _status(
            "degraded", "pillow_region_proposal", Image.__version__,
            reason="图片尺寸不足以检测区域",
        )
    corner = Image.new("RGB", image.size, image.getpixel((0, 0)))
    difference = ImageChops.difference(image, corner).convert("L")
    mean = ImageStat.Stat(difference).mean[0]
    threshold = max(12, min(64, int(mean * 0.75)))
    mask = difference.point(lambda value: 255 if value >= threshold else 0)
    region = mask.getbbox()
    rows: list[dict[str, Any]] = []
    if region:
        left, top, right, bottom = region
        width, height = right - left, bottom - top
        if width > 0 and height > 0:
            normalized = _bbox(left, top, width, height, image)
            x, y = normalized["x"], normalized["y"]
            right_n, bottom_n = x + normalized["width"], y + normalized["height"]
            rows.append({
                "evidence_type": "visual_field",
                "field_key": "region_proposal",
                "label": "region_proposal",
                "evidence_text": "non-semantic pixel-contrast region proposal",
                "bbox": normalized,
                "confidence": round(min(0.95, 0.5 + mean / 255), 6),
                "fact_status": "visible",
                "mask_extraction": {
                    "status": "analyzed",
                    "analyzer": "pillow_threshold_mask",
                    "analyzer_version": Image.__version__,
                    "polygon": [
                        {"x": x, "y": y}, {"x": right_n, "y": y},
                        {"x": right_n, "y": bottom_n}, {"x": x, "y": bottom_n},
                    ],
                },
            })
    return _status(
        "analyzed", "pillow_region_proposal", Image.__version__, evidence=rows,
    )


def _provider_coordinate(value: Any, *, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path} must be a finite number between 0 and 1")
    coordinate = float(value)
    if not math.isfinite(coordinate) or not 0 <= coordinate <= 1:
        raise ValueError(f"{path} must be a finite number between 0 and 1")
    return coordinate


def _provider_probability(value: Any, *, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path} must be a finite number between 0 and 1")
    probability = float(value)
    if not math.isfinite(probability) or not 0 <= probability <= 1:
        raise ValueError(f"{path} must be a finite number between 0 and 1")
    return probability


def _provider_label(value: Any, *, path: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{path} must be a string")
    label = value.strip()
    if not label or len(label) > 128 or any(ord(char) < 32 for char in label):
        raise ValueError(f"{path} must be 1-128 printable characters")
    return label


def _provider_text(value: Any, *, path: str, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{path} must be a string")
    text = value.strip()
    if (
        (required and not text)
        or len(text) > 2000
        or any(ord(char) < 32 and char not in {"\n", "\r", "\t"} for char in text)
    ):
        raise ValueError(f"{path} must be 1-2000 safe characters")
    return text or None


def _provider_runtime_config(capability: str) -> dict[str, Any]:
    if capability not in _REGION_CAPABILITIES:
        raise ValueError("region analyzer capability must be detector or segmenter")
    prefix = f"image_evidence_{capability}"
    return {
        "url": str(getattr(settings, f"{prefix}_url") or "").strip(),
        "api_key": str(getattr(settings, f"{prefix}_api_key") or "").strip(),
        "timeout_seconds": getattr(settings, f"{prefix}_timeout_seconds"),
        "health_url": str(getattr(settings, f"{prefix}_health_url") or "").strip(),
        "health_timeout_seconds": getattr(settings, f"{prefix}_health_timeout_seconds"),
    }


def _polygon_area(points: list[dict[str, float]]) -> float:
    return abs(sum(
        point["x"] * points[(index + 1) % len(points)]["y"]
        - points[(index + 1) % len(points)]["x"] * point["y"]
        for index, point in enumerate(points)
    )) / 2


def _orientation(
    left: dict[str, float], middle: dict[str, float], right: dict[str, float]
) -> float:
    return (
        (middle["x"] - left["x"]) * (right["y"] - left["y"])
        - (middle["y"] - left["y"]) * (right["x"] - left["x"])
    )


def _polygon_self_intersects(points: list[dict[str, float]]) -> bool:
    edge_count = len(points)
    for left_index in range(edge_count):
        left_start = points[left_index]
        left_end = points[(left_index + 1) % edge_count]
        for right_index in range(left_index + 1, edge_count):
            if right_index in {
                left_index,
                (left_index + 1) % edge_count,
                (left_index - 1) % edge_count,
            }:
                continue
            right_start = points[right_index]
            right_end = points[(right_index + 1) % edge_count]
            left_turns = (
                _orientation(left_start, left_end, right_start),
                _orientation(left_start, left_end, right_end),
            )
            right_turns = (
                _orientation(right_start, right_end, left_start),
                _orientation(right_start, right_end, left_end),
            )
            if left_turns[0] * left_turns[1] <= 0 and right_turns[0] * right_turns[1] <= 0:
                return True
    return False


def _provider_evidence(value: Any, image: Image.Image, capability: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 128:
        raise ValueError(f"{capability} provider evidence must be an array of at most 128 rows")
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise ValueError(f"{capability} evidence[{index}] must be an object")
        bbox = raw.get("bbox")
        polygon = raw.get("polygon")
        if (bbox is None) == (polygon is None):
            raise ValueError(f"{capability} evidence[{index}] requires exactly one region")
        if bbox is not None:
            if not isinstance(bbox, dict):
                raise ValueError(f"{capability} evidence[{index}].bbox must be an object")
            if set(bbox) != {"x", "y", "width", "height"}:
                raise ValueError(
                    f"{capability} evidence[{index}].bbox must contain only x/y/width/height"
                )
            try:
                x, y, width, height = (
                    _provider_coordinate(
                        bbox[key], path=f"{capability} evidence[{index}].bbox.{key}"
                    )
                    for key in ("x", "y", "width", "height")
                )
            except KeyError as exc:
                raise ValueError(
                    f"{capability} evidence[{index}].bbox.{exc.args[0]} is required"
                ) from exc
            if width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
                raise ValueError(f"{capability} evidence[{index}].bbox is invalid")
            bbox = {"x": x, "y": y, "width": width, "height": height}
        if polygon is not None:
            if not isinstance(polygon, list) or not 3 <= len(polygon) <= 128:
                raise ValueError(f"{capability} evidence[{index}].polygon is invalid")
            normalized_polygon: list[dict[str, float]] = []
            for point_index, point in enumerate(polygon):
                if not isinstance(point, dict):
                    raise ValueError(
                        f"{capability} evidence[{index}].polygon[{point_index}] must be an object"
                    )
                if set(point) != {"x", "y"}:
                    raise ValueError(
                        f"{capability} evidence[{index}].polygon[{point_index}]"
                        " must contain only x/y"
                    )
                try:
                    normalized_polygon.append({
                        key: _provider_coordinate(
                            point[key],
                            path=(
                                f"{capability} evidence[{index}]"
                                f".polygon[{point_index}].{key}"
                            ),
                        )
                        for key in ("x", "y")
                    })
                except KeyError as exc:
                    raise ValueError(
                        f"{capability} evidence[{index}].polygon[{point_index}]"
                        f".{exc.args[0]} is required"
                    ) from exc
            polygon = normalized_polygon
            if len({(point["x"], point["y"]) for point in polygon}) != len(polygon):
                raise ValueError(f"{capability} evidence[{index}].polygon repeats a vertex")
            if _polygon_area(polygon) <= 1e-12:
                raise ValueError(f"{capability} evidence[{index}].polygon has zero area")
            if _polygon_self_intersects(polygon):
                raise ValueError(f"{capability} evidence[{index}].polygon self-intersects")
        confidence = _provider_probability(
            raw.get("confidence"), path=f"{capability} evidence[{index}].confidence"
        )
        label = _provider_label(
            raw.get("label"), path=f"{capability} evidence[{index}].label"
        )
        evidence_text = _provider_text(
            raw.get("text"), path=f"{capability} evidence[{index}].text"
        ) or label
        rows.append({
            "evidence_type": "visual_field",
            "field_key": capability,
            "label": label,
            "evidence_text": evidence_text,
            "confidence": round(confidence, 6),
            "fact_status": "visible",
            **({"bbox": bbox} if bbox is not None else {"polygon": polygon}),
        })
    return rows


def _normalize_region_provider_response(
    payload: Any,
    *,
    image: Image.Image,
    capability: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("region provider response must be an object")
    if payload.get("contract_version") != REGION_ANALYZER_CONTRACT_VERSION:
        raise ValueError(
            f"region provider must return {REGION_ANALYZER_CONTRACT_VERSION}"
        )
    if payload.get("capability") != capability:
        raise ValueError(f"region provider capability must be {capability}")
    status = payload.get("status")
    if status not in _REGION_PROVIDER_STATUSES:
        raise ValueError("region provider status must be analyzed, degraded or unsupported")
    analyzer = _provider_label(
        payload.get("analyzer"), path="region provider response.analyzer"
    )
    analyzer_version = _provider_label(
        payload.get("analyzer_version"),
        path="region provider response.analyzer_version",
    )
    reason = _provider_text(
        payload.get("degraded_reason"),
        path="region provider response.degraded_reason",
        required=status != "analyzed",
    )
    evidence = payload.get("evidence")
    if status != "analyzed":
        if evidence != []:
            raise ValueError(f"region provider cannot return evidence with status={status}")
        return _status(
            str(status), analyzer, analyzer_version, reason=reason,
            contract_version=REGION_ANALYZER_CONTRACT_VERSION,
            capability=capability,
        )
    if reason:
        raise ValueError("region provider analyzed response cannot include degraded_reason")
    rows = _provider_evidence(evidence, image, capability)
    return _status(
        "analyzed", analyzer, analyzer_version, evidence=rows,
        contract_version=REGION_ANALYZER_CONTRACT_VERSION,
        capability=capability,
    )


def _record_provider_success(
    capability: str, *, analyzer: str, analyzer_version: str, source: str
) -> dict[str, Any]:
    record = {
        "last_success_at": datetime.now(timezone.utc).isoformat(),
        "last_success_source": source,
        "last_analyzer": analyzer,
        "last_analyzer_version": analyzer_version,
    }
    with _PROVIDER_STATE_LOCK:
        _PROVIDER_LAST_SUCCESS[capability] = record
        _PROVIDER_HEALTH_CACHE.pop(capability, None)
    return deepcopy(record)


def http_region_provider(image: Image.Image, *, capability: str) -> dict[str, Any]:
    """Call a configured detector/segmenter with a bounded validated contract."""
    try:
        config = _provider_runtime_config(capability)
    except ValueError as exc:
        return _status(
            "unsupported", "http_region_provider", "invalid_capability", reason=str(exc)
        )
    url = config["url"]
    api_key = config["api_key"]
    if not url:
        return _status(
            "unsupported", f"http_{capability}_provider", "unconfigured",
            reason=f"{capability} provider 未配置",
            contract_version=REGION_ANALYZER_CONTRACT_VERSION,
            capability=capability,
        )
    try:
        timeout = min(120, max(1, int(config["timeout_seconds"])))
    except (TypeError, ValueError):
        return _status(
            "degraded", f"http_{capability}_provider", "invalid_config",
            reason=f"{capability} provider timeout 配置无效",
            contract_version=REGION_ANALYZER_CONTRACT_VERSION,
            capability=capability,
        )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    try:
        payload = gateway._request_json(
            "POST",
            url,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            payload={
                "contract_version": REGION_ANALYZER_CONTRACT_VERSION,
                "capability": capability,
                "image_base64": base64.b64encode(buffer.getvalue()).decode("ascii"),
            },
            timeout=timeout,
            retries=0,
            trusted_hosts=settings.trusted_analyzer_host_list,
        )
        result = _normalize_region_provider_response(
            payload, image=image, capability=capability
        )
        if result["status"] == "analyzed":
            _record_provider_success(
                capability,
                analyzer=str(result["analyzer"]),
                analyzer_version=str(result["analyzer_version"]),
                source="analysis",
            )
        return result
    except Exception as exc:  # noqa: BLE001
        return _status(
            "degraded", f"http_{capability}_provider", "unknown", reason=str(exc)[:200],
            contract_version=REGION_ANALYZER_CONTRACT_VERSION,
            capability=capability,
        )


def _provider_health(capability: str) -> dict[str, Any]:
    try:
        config = _provider_runtime_config(capability)
    except ValueError as exc:
        return {
            "status": "unsupported",
            "analyzer": "http_region_provider",
            "configured": False,
            "verification_status": "invalid_capability",
            "health_url_configured": False,
            "degraded_reason": str(exc),
        }
    url = config["url"]
    health_url = config["health_url"]
    api_key = config["api_key"]
    analyzer = f"http_{capability}_provider"
    if not url:
        return {
            "status": "unsupported",
            "analyzer": analyzer,
            "configured": False,
            "verification_status": "unconfigured",
            "health_url_configured": False,
            "degraded_reason": f"{capability} provider 未配置",
        }
    with _PROVIDER_STATE_LOCK:
        last_success = deepcopy(_PROVIDER_LAST_SUCCESS.get(capability) or {})
    if not health_url:
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
    cache_key = hashlib.sha256(json.dumps(
        {
            "url": url,
            "health_url": health_url,
            "api_key": api_key,
            "timeout": config["health_timeout_seconds"],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()

    def cached_result() -> dict[str, Any] | None:
        now = time.monotonic()
        with _PROVIDER_STATE_LOCK:
            cached = _PROVIDER_HEALTH_CACHE.get(capability)
            if cached and cached[0] == cache_key and cached[1] > now:
                return deepcopy(cached[2])
            if cached:
                _PROVIDER_HEALTH_CACHE.pop(capability, None)
        return None

    cached = cached_result()
    if cached is not None:
        return cached

    with _PROVIDER_HEALTH_PROBE_LOCKS[capability]:
        cached = cached_result()
        if cached is not None:
            return cached
        try:
            timeout = min(10, max(1, int(config["health_timeout_seconds"])))
            payload = gateway._request_json(
                "GET",
                health_url,
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
                payload=None,
                timeout=timeout,
                retries=0,
                trusted_hosts=settings.trusted_analyzer_host_list,
            )
            if not isinstance(payload, dict):
                raise ValueError("health response must be an object")
            if (
                payload.get("contract_version") is not None
                and payload.get("contract_version") != REGION_ANALYZER_CONTRACT_VERSION
            ):
                raise ValueError(
                    f"health response.contract_version must be {REGION_ANALYZER_CONTRACT_VERSION}"
                )
            if payload.get("capability") is not None and payload.get("capability") != capability:
                raise ValueError(f"health response.capability must be {capability}")
            health_status = str(payload.get("status") or "").strip().lower()
            if payload.get("ok") is not True and health_status not in {
                "ok", "healthy", "available", "ready",
            }:
                raise ValueError("health response did not report ready")
            provider_analyzer = _provider_label(
                payload.get("analyzer") or analyzer,
                path="health response.analyzer",
            )
            version = _provider_label(
                payload.get("analyzer_version") or "unknown",
                path="health response.analyzer_version",
            )
            success = _record_provider_success(
                capability,
                analyzer=provider_analyzer,
                analyzer_version=version,
                source="health_probe",
            )
            result = {
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
            result = {
                "status": "degraded",
                "analyzer": analyzer,
                "configured": True,
                "verification_status": "health_probe_failed",
                "health_url_configured": True,
                "degraded_reason": str(exc)[:200],
                **last_success,
            }
        ttl = int(settings.image_evidence_health_cache_ttl_seconds)
        with _PROVIDER_STATE_LOCK:
            _PROVIDER_HEALTH_CACHE[capability] = (
                cache_key,
                time.monotonic() + ttl,
                deepcopy(result),
            )
        return result


def _iou(left: dict[str, Any] | None, right: dict[str, Any] | None) -> float:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return 0.0
    lx1, ly1 = float(left["x"]), float(left["y"])
    rx1, ry1 = float(right["x"]), float(right["y"])
    lx2, ly2 = lx1 + float(left["width"]), ly1 + float(left["height"])
    rx2, ry2 = rx1 + float(right["width"]), ry1 + float(right["height"])
    intersection = max(0.0, min(lx2, rx2) - max(lx1, rx1)) * max(
        0.0, min(ly2, ry2) - max(ly1, ry1)
    )
    union = (lx2 - lx1) * (ly2 - ly1) + (rx2 - rx1) * (ry2 - ry1) - intersection
    return intersection / union if union > 0 else 0.0


def _region_bbox(row: dict[str, Any]) -> dict[str, float] | None:
    bbox = row.get("bbox")
    if isinstance(bbox, dict):
        return bbox
    polygon = row.get("polygon")
    if not isinstance(polygon, list) or len(polygon) < 3:
        return None
    try:
        xs = [float(point["x"]) for point in polygon]
        ys = [float(point["y"]) for point in polygon]
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "x": min(xs), "y": min(ys),
        "width": max(xs) - min(xs), "height": max(ys) - min(ys),
    }


def merge_region_evidence(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Annotate overlapping, disagreeing evidence without discarding either source."""
    merged = [dict(row) for row in rows]
    for index, row in enumerate(merged):
        conflicts: list[str] = []
        for other_index, other in enumerate(merged):
            if index == other_index or row.get("source_index") != other.get("source_index"):
                continue
            if row.get("analyzer_source") == other.get("analyzer_source"):
                continue
            if _iou(_region_bbox(row), _region_bbox(other)) < 0.35:
                continue
            left = str(row.get("evidence_text") or row.get("label") or "").casefold()
            right = str(other.get("evidence_text") or other.get("label") or "").casefold()
            if left and right and left != right:
                conflicts.append(str(other.get("evidence_id")))
        row["conflict_status"] = "conflict" if conflicts else "none"
        row["conflicts_with"] = sorted(set(conflicts))
    return merged


def analyze_image_sources(
    refs: list[str],
    *,
    vlm_evidence: list[dict[str, Any]] | None = None,
    source_fingerprints: list[dict[str, Any]] | None = None,
    ocr_analyzer: Analyzer | None = None,
    region_analyzer: Analyzer | None = None,
    detector_analyzer: Analyzer | None = None,
    segmenter_analyzer: Analyzer | None = None,
) -> dict[str, Any]:
    ocr_analyzer = ocr_analyzer or tesseract_ocr
    region_analyzer = region_analyzer or pillow_region_segmentation
    detector_analyzer = detector_analyzer or (
        lambda image: http_region_provider(image, capability="detector")
    )
    segmenter_analyzer = segmenter_analyzer or (
        lambda image: http_region_provider(image, capability="segmenter")
    )
    fingerprints = {
        int(item.get("source_index")): str(item.get("content_sha256") or "")
        for item in source_fingerprints or [] if isinstance(item, dict)
    }
    analyzers: dict[str, list[dict[str, Any]]] = {
        "ocr": [], "region_proposal": [], "detector": [], "segmenter": [],
    }
    evidence: list[dict[str, Any]] = []
    for index, ref in enumerate(refs, start=1):
        try:
            image, decoded_hash = _decode_data_image(ref)
        except Exception as exc:  # noqa: BLE001
            reason = str(exc)[:200]
            analyzers["ocr"].append(_status("unsupported", "independent_ocr", "v1", reason=reason))
            analyzers["region_proposal"].append(
                _status("unsupported", "independent_region", "v1", reason=reason)
            )
            analyzers["detector"].append(
                _status("unsupported", "http_detector_provider", "unavailable", reason=reason)
            )
            analyzers["segmenter"].append(
                _status("unsupported", "http_segmenter_provider", "unavailable", reason=reason)
            )
            continue
        content_hash = fingerprints.get(index) or decoded_hash
        for name, analyzer in (
            ("ocr", ocr_analyzer),
            ("region_proposal", region_analyzer),
            ("detector", detector_analyzer),
            ("segmenter", segmenter_analyzer),
        ):
            try:
                result = analyzer(image)
            except Exception as exc:  # noqa: BLE001
                result = _status("degraded", name, "unknown", reason=str(exc)[:200])
            analyzers[name].append({**result, "source_index": index})
            for raw in result.get("evidence") or []:
                row = {
                    **raw,
                    "source_index": index,
                    "source_content_hash": content_hash,
                    "source_fingerprint": f"sha256:{content_hash}",
                    "analyzer_source": str(result.get("analyzer") or name),
                    "analyzer_status": str(result.get("status") or "degraded"),
                    "analyzer_version": str(result.get("analyzer_version") or "unknown"),
                    "review_status": "pending",
                    "protected": False,
                    "editable": False,
                }
                row["evidence_id"] = stable_evidence_id(row)
                evidence.append(row)
    for raw in vlm_evidence or []:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        source_index = int(row.get("source_index") or 1)
        content_hash = fingerprints.get(source_index, "")
        row.update({
            "source_content_hash": content_hash or None,
            "source_fingerprint": f"sha256:{content_hash}" if content_hash else None,
            "label": row.get("label") or row.get("field_key"),
            "analyzer_source": "vision_language_model",
            "analyzer_status": "analyzed",
            "analyzer_version": "provider_snapshot",
            "review_status": "pending",
        })
        row["evidence_id"] = stable_evidence_id(row)
        evidence.append(row)
    return {
        "contract_version": CONTRACT_VERSION,
        "analyzers": analyzers,
        "evidence": merge_region_evidence(evidence),
    }


def analyzer_health() -> dict[str, Any]:
    version = _tesseract_version()
    languages = _tesseract_languages()
    language_metadata = _tesseract_language_metadata(languages)
    language_reason = _tesseract_language_reason(language_metadata)
    tesseract_status = (
        "unsupported"
        if not TESSERACT
        else "degraded"
        if language_reason
        else "available"
    )
    return {
        "contract_version": CONTRACT_VERSION,
        "ocr": {
            "status": tesseract_status,
            "analyzer": "tesseract_tsv",
            "binary": TESSERACT,
            "analyzer_version": version,
            "degraded_reason": (
                "服务器未安装 Tesseract OCR"
                if not TESSERACT
                else language_reason
            ),
            **language_metadata,
        },
        "region_proposal": {
            "status": "available",
            "analyzer": "pillow_region_proposal",
            "analyzer_version": Image.__version__,
            "semantic_detection": False,
        },
        "detector": _provider_health("detector"),
        "segmenter": _provider_health("segmenter"),
    }
