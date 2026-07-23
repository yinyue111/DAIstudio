"""Immutable reverse-result lineage and source-content identities."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any

from sqlalchemy.orm import Session

from ..models import ReverseResultRevision

VERIFIED = "verified"
LEGACY_UNVERIFIED = "legacy_unverified"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EXPECTED_PARENT_SOURCE = {
    "normalized": "provider_raw",
    "user_edit": "normalized",
    "applied": "user_edit",
    "model_compiled": "applied",
    "generation": "model_compiled",
}
_ALLOWED_PARENT_SOURCES = {
    **{source: (parent,) for source, parent in _EXPECTED_PARENT_SOURCE.items()},
    # A reproduction correction may branch from the exact applied revision a
    # generation used. It remains an explicit user edit and is later wrapped
    # in a new applied revision before it can drive another generation.
    "user_edit": ("user_edit", "normalized", "applied"),
}
_CLIENT_RESERVED_PAYLOAD_KEYS = frozenset({
    "parent_revision_id",
    "payload_hash",
    "source_content_hash",
    "source_fingerprints",
    "lineage_status",
    "evidence_review_action",
    "_lineage",
})


class ReverseLineageError(ValueError):
    pass


def canonical_payload_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def bytes_content_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def data_uri_content_hash(value: str) -> str:
    try:
        header, encoded = str(value or "").split(",", 1)
        if ";base64" not in header.lower():
            raise ValueError("not base64")
        return bytes_content_hash(base64.b64decode(encoded, validate=True))
    except Exception as exc:  # noqa: BLE001
        raise ReverseLineageError("素材内容指纹无法解析") from exc


def locator_hash(value: str | None) -> str:
    normalized = str(value or "").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def build_source_fingerprint(
    *,
    source_index: int,
    content_hash: str,
    locator: str | None,
    method: str = "raw_bytes_sha256",
) -> dict[str, Any]:
    normalized_hash = str(content_hash or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized_hash):
        raise ReverseLineageError("素材内容指纹必须是 SHA-256")
    if isinstance(source_index, bool) or int(source_index) < 1:
        raise ReverseLineageError("素材索引无效")
    return {
        "source_index": int(source_index),
        "content_sha256": normalized_hash,
        "locator_sha256": locator_hash(locator),
        "method": str(method or "raw_bytes_sha256"),
    }


def normalize_source_fingerprints(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ReverseLineageError("反推版本缺少素材内容指纹")
    normalized: list[dict[str, Any]] = []
    seen: set[int] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ReverseLineageError("素材内容指纹结构无效")
        index = raw.get("source_index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 1 or index in seen:
            raise ReverseLineageError("素材内容指纹索引无效")
        seen.add(index)
        normalized.append(build_source_fingerprint(
            source_index=index,
            content_hash=str(raw.get("content_sha256") or ""),
            locator=None,
            method=str(raw.get("method") or "raw_bytes_sha256"),
        ))
        locator_value = str(raw.get("locator_sha256") or "").strip().lower()
        if not _SHA256_RE.fullmatch(locator_value):
            raise ReverseLineageError("素材定位指纹无效")
        normalized[-1]["locator_sha256"] = locator_value
    normalized.sort(key=lambda item: item["source_index"])
    if [item["source_index"] for item in normalized] != list(range(1, len(normalized) + 1)):
        raise ReverseLineageError("素材内容指纹索引必须连续")
    return normalized


def source_content_hash(fingerprints: Any) -> str:
    normalized = normalize_source_fingerprints(fingerprints)
    raw = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def reject_client_lineage_fields(payload: dict[str, Any]) -> None:
    forged = sorted(_CLIENT_RESERVED_PAYLOAD_KEYS.intersection(payload))
    if forged:
        raise ReverseLineageError(
            "反推版本血缘字段由服务端生成: " + ", ".join(forged)
        )


def expected_parent_source(source: str) -> str | None:
    return _EXPECTED_PARENT_SOURCE.get(source)


def allowed_parent_sources(source: str) -> tuple[str, ...]:
    return _ALLOWED_PARENT_SOURCES.get(source, ())


def assert_revision_integrity(revision: ReverseResultRevision) -> None:
    if revision.lineage_status != VERIFIED:
        raise ReverseLineageError("历史反推版本未完成血缘验证，不能用于新生成")
    payload = revision.payload if isinstance(revision.payload, dict) else None
    if payload is None or canonical_payload_hash(payload) != revision.payload_hash:
        raise ReverseLineageError("反推版本内容哈希不一致")
    computed_source_hash = source_content_hash(revision.source_fingerprints)
    if computed_source_hash != revision.source_content_hash:
        raise ReverseLineageError("反推版本素材内容哈希不一致")


def validate_revision_chain(
    db: Session,
    revision: ReverseResultRevision,
    *,
    terminal_source: str,
) -> list[ReverseResultRevision]:
    if revision.source != terminal_source:
        raise ReverseLineageError(f"反推版本必须是 {terminal_source}")
    chain: list[ReverseResultRevision] = []
    current = revision
    seen: set[int] = set()
    while True:
        current_id = int(current.id)
        if current_id in seen:
            raise ReverseLineageError("反推版本血缘存在循环")
        seen.add(current_id)
        assert_revision_integrity(current)
        chain.append(current)
        parent_sources = allowed_parent_sources(current.source)
        if not parent_sources:
            if current.source != "provider_raw" or current.parent_revision_id is not None:
                raise ReverseLineageError("反推版本血缘根节点无效")
            break
        if current.parent_revision_id is None:
            raise ReverseLineageError("反推版本血缘不完整")
        parent = db.get(ReverseResultRevision, int(current.parent_revision_id))
        if (
            parent is None
            or int(parent.operation_id) != int(current.operation_id)
            or int(parent.user_id) != int(current.user_id)
            or parent.source not in parent_sources
            or int(parent.version) >= int(current.version)
        ):
            raise ReverseLineageError("反推版本父节点无效")
        if (
            parent.source_content_hash != current.source_content_hash
            or parent.source_fingerprints != current.source_fingerprints
        ):
            raise ReverseLineageError("反推版本与父节点的素材指纹不一致")
        current = parent
    return chain


def fingerprint_for_source_index(
    revision: ReverseResultRevision,
    source_index: int,
) -> dict[str, Any] | None:
    try:
        fingerprints = normalize_source_fingerprints(
            getattr(revision, "source_fingerprints", None)
        )
    except ReverseLineageError:
        return None
    return next(
        (item for item in fingerprints if item["source_index"] == int(source_index)),
        None,
    )
