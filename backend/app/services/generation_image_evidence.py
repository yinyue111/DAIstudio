"""Server-authoritative masks derived from reviewed image reverse evidence."""
from __future__ import annotations

import base64
import hashlib
import io
import math
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageDraw
from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from ..models import GenTask, ReverseOperation, ReverseResultRevision
from . import reverse_lineage, storage

MAX_EVIDENCE_ITEMS = 128
MAX_POLYGON_POINTS = 128
MASK_PLAN_SCHEMA_VERSION = "image-mask-plan.v1"
_EVIDENCE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_EVIDENCE_TYPES = {
    "visual_field",
    "ocr",
    "logo",
    "packaging",
    "subject_protection",
}
_FACT_STATUSES = frozenset({"visible", "inferred", "unknown"})
_REVIEW_STATUSES = frozenset({"pending", "confirmed", "rejected"})


class ReviewedEvidenceMaskError(ValueError):
    """The saved review cannot be safely converted into a generation mask."""


@dataclass(frozen=True)
class ReviewedEvidenceRegion:
    evidence_id: str
    source_index: int
    protected: bool
    bbox: tuple[float, float, float, float] | None
    polygon: tuple[tuple[float, float], ...] | None


@dataclass(frozen=True)
class ReviewedEvidencePlan:
    operation_id: int
    revision_id: int
    revision_version: int
    source_index: int
    source_url: str
    protected_regions: tuple[ReviewedEvidenceRegion, ...]
    editable_regions: tuple[ReviewedEvidenceRegion, ...]
    source_content_hash: str | None = None

    @property
    def evidence_ids(self) -> list[str]:
        return [
            region.evidence_id
            for region in (*self.protected_regions, *self.editable_regions)
        ]


@dataclass(frozen=True)
class ReviewedEvidenceMask:
    data_uri: str
    mode: str
    confidence: float
    bbox: tuple[int, int, int, int] | None
    width: int
    height: int
    reason: str
    mask_hash: str
    protected_fraction: float
    editable_fraction: float
    plan: ReviewedEvidencePlan

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "_edit_mask_mode": self.mode,
            "_edit_mask_confidence": self.confidence,
            "_edit_mask_bbox": list(self.bbox) if self.bbox else None,
            "_edit_mask_source": self.reason,
            "_evidence_mask_schema_version": MASK_PLAN_SCHEMA_VERSION,
            "_evidence_mask_operation_id": self.plan.operation_id,
            "_evidence_mask_revision_id": self.plan.revision_id,
            "_evidence_mask_revision_version": self.plan.revision_version,
            "_evidence_mask_source_index": self.plan.source_index,
            "_evidence_mask_source_content_hash": self.plan.source_content_hash,
            "_evidence_mask_evidence_ids": self.plan.evidence_ids,
            "_evidence_mask_hash": self.mask_hash,
            "_evidence_mask_protected_fraction": round(self.protected_fraction, 6),
            "_evidence_mask_editable_fraction": round(self.editable_fraction, 6),
        }


def _finite_coordinate(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReviewedEvidenceMaskError(f"{label} 必须是有限数值")
    number = float(value)
    if not math.isfinite(number) or number < 0 or number > 1:
        raise ReviewedEvidenceMaskError(f"{label} 必须在 0..1 范围内")
    return round(number, 6)


def _finite_confidence(value: Any, evidence_id: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReviewedEvidenceMaskError(f"证据 {evidence_id} 的 confidence 必须是有限数值")
    number = float(value)
    if not math.isfinite(number) or number < 0 or number > 1:
        raise ReviewedEvidenceMaskError(f"证据 {evidence_id} 的 confidence 必须在 0..1 范围内")
    return round(number, 6)


def _normalized_bbox(value: Any, evidence_id: str) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ReviewedEvidenceMaskError(f"证据 {evidence_id} 的 bbox 必须是对象")
    x = _finite_coordinate(value.get("x"), f"证据 {evidence_id}.bbox.x")
    y = _finite_coordinate(value.get("y"), f"证据 {evidence_id}.bbox.y")
    width = _finite_coordinate(value.get("width"), f"证据 {evidence_id}.bbox.width")
    height = _finite_coordinate(value.get("height"), f"证据 {evidence_id}.bbox.height")
    if width <= 0 or height <= 0 or x + width > 1.000001 or y + height > 1.000001:
        raise ReviewedEvidenceMaskError(f"证据 {evidence_id} 的 bbox 超出源图范围")
    return x, y, width, height


def _polygon_area(points: tuple[tuple[float, float], ...]) -> float:
    return abs(sum(
        point[0] * points[(index + 1) % len(points)][1]
        - points[(index + 1) % len(points)][0] * point[1]
        for index, point in enumerate(points)
    ) / 2)


def _normalized_polygon(value: Any, evidence_id: str) -> tuple[tuple[float, float], ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not 3 <= len(value) <= MAX_POLYGON_POINTS:
        raise ReviewedEvidenceMaskError(
            f"证据 {evidence_id} 的 polygon 需要 3..{MAX_POLYGON_POINTS} 个点"
        )
    points: list[tuple[float, float]] = []
    for index, point in enumerate(value):
        if not isinstance(point, dict):
            raise ReviewedEvidenceMaskError(f"证据 {evidence_id}.polygon[{index}] 必须是对象")
        points.append((
            _finite_coordinate(point.get("x"), f"证据 {evidence_id}.polygon[{index}].x"),
            _finite_coordinate(point.get("y"), f"证据 {evidence_id}.polygon[{index}].y"),
        ))
    normalized = tuple(points)
    if _polygon_area(normalized) <= 0.00000001:
        raise ReviewedEvidenceMaskError(f"证据 {evidence_id} 的 polygon 面积无效")
    return normalized


def _reviewed_regions(
    payload: dict[str, Any],
    source_count: int,
    *,
    strict_persisted: bool = False,
    require_all_confirmed: bool = False,
) -> list[ReviewedEvidenceRegion]:
    if isinstance(source_count, bool) or not isinstance(source_count, int) or source_count < 1:
        raise ReviewedEvidenceMaskError("反推任务没有可校验的来源素材")
    raw = payload.get("image_evidence")
    if raw is None:
        if strict_persisted and "image_evidence" in payload:
            raise ReviewedEvidenceMaskError("image_evidence 必须是数组")
        return []
    if not isinstance(raw, list):
        raise ReviewedEvidenceMaskError("image_evidence 必须是数组")
    if len(raw) > MAX_EVIDENCE_ITEMS:
        raise ReviewedEvidenceMaskError(f"图片证据最多 {MAX_EVIDENCE_ITEMS} 条")

    reviewed: list[ReviewedEvidenceRegion] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ReviewedEvidenceMaskError(f"image_evidence[{index}] 必须是对象")
        review_status_value = item.get("review_status")
        has_review_status = "review_status" in item
        if strict_persisted and not has_review_status:
            raise ReviewedEvidenceMaskError(
                f"image_evidence[{index}] 缺少显式 review_status"
            )
        review_status = review_status_value if isinstance(review_status_value, str) else "pending"
        if has_review_status and (
            not isinstance(review_status_value, str)
            or review_status not in _REVIEW_STATUSES
        ):
            raise ReviewedEvidenceMaskError(
                f"image_evidence[{index}] 的 review_status 无效"
            )
        if require_all_confirmed and review_status != "confirmed":
            raise ReviewedEvidenceMaskError(
                f"image_evidence[{index}] 尚未明确确认，applied 不能包含待审阅或已驳回证据"
            )
        evidence_id_value = item.get("evidence_id")
        evidence_id = evidence_id_value if isinstance(evidence_id_value, str) else ""
        if (strict_persisted or has_review_status) and not _EVIDENCE_ID_RE.fullmatch(
            evidence_id
        ):
            raise ReviewedEvidenceMaskError(f"image_evidence[{index}] 缺少稳定 evidence_id")
        if evidence_id:
            if not _EVIDENCE_ID_RE.fullmatch(evidence_id):
                raise ReviewedEvidenceMaskError(f"image_evidence[{index}] 的 evidence_id 无效")
            if evidence_id in seen_ids:
                raise ReviewedEvidenceMaskError(f"证据 ID 重复: {evidence_id}")
            seen_ids.add(evidence_id)
        label = evidence_id or f"image_evidence[{index}]"
        if str(item.get("evidence_type") or "") not in _EVIDENCE_TYPES:
            raise ReviewedEvidenceMaskError(f"证据 {label} 类型无效")
        field_key = item.get("field_key")
        evidence_text = item.get("evidence_text")
        if not isinstance(field_key, str) or not 1 <= len(field_key.strip()) <= 64:
            raise ReviewedEvidenceMaskError(f"证据 {label} 的 field_key 无效")
        if not isinstance(evidence_text, str) or not 1 <= len(evidence_text.strip()) <= 2000:
            raise ReviewedEvidenceMaskError(f"证据 {label} 的 evidence_text 无效")
        _finite_confidence(item.get("confidence"), label)
        source_index = item.get("source_index")
        if isinstance(source_index, bool) or not isinstance(source_index, int):
            raise ReviewedEvidenceMaskError(f"证据 {label} 的 source_index 必须是整数")
        if source_index < 1 or source_index > source_count:
            raise ReviewedEvidenceMaskError(f"证据 {label} 引用了不存在的源图")
        fact_status = str(item.get("fact_status") or "")
        if fact_status not in _FACT_STATUSES:
            raise ReviewedEvidenceMaskError(f"证据 {label} 的 fact_status 无效")
        protected = item.get("protected")
        editable = item.get("editable")
        if not isinstance(protected, bool) or not isinstance(editable, bool):
            raise ReviewedEvidenceMaskError(f"证据 {label} 的区域用途必须是布尔值")
        if protected and editable:
            raise ReviewedEvidenceMaskError(f"证据 {label} 不能同时选择保护和可编辑")
        bbox = _normalized_bbox(item.get("bbox"), label)
        polygon = _normalized_polygon(item.get("polygon"), label)
        if bbox is not None and polygon is not None:
            raise ReviewedEvidenceMaskError(
                f"证据 {label} 的 bbox 和 polygon 必须且只能提供一种"
            )
        has_region = bbox is not None or polygon is not None
        if fact_status == "visible" and not has_region:
            raise ReviewedEvidenceMaskError(f"证据 {label} 的可见事实没有可执行区域")
        if fact_status != "visible" and (protected or editable):
            raise ReviewedEvidenceMaskError(
                f"证据 {label} 只有可见事实才能选择保护或可编辑"
            )
        if str(item.get("evidence_type") or "") == "subject_protection" and not (
            fact_status == "visible" and has_region and protected and not editable
        ):
            raise ReviewedEvidenceMaskError(
                f"证据 {label} 的 subject_protection 组合无效"
            )
        if review_status != "confirmed":
            continue
        if fact_status != "visible":
            continue
        if protected == editable:
            raise ReviewedEvidenceMaskError(
                f"证据 {label} 确认前必须且只能选择保护或可编辑"
            )
        reviewed.append(ReviewedEvidenceRegion(
            evidence_id=evidence_id,
            source_index=source_index,
            protected=protected,
            bbox=bbox,
            polygon=polygon,
        ))
    return reviewed


def normalize_inherited_image_evidence_for_review(value: Any) -> list[dict[str, Any]]:
    """Make provider evidence persistable without treating it as user-reviewed."""
    if not isinstance(value, list):
        raise ReviewedEvidenceMaskError("image_evidence 必须是数组")
    normalized: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ReviewedEvidenceMaskError(f"image_evidence[{index}] 必须是对象")
        row = deepcopy(item)
        requested = item.get("evidence_id")
        source_index = item.get("source_index")
        source_token = (
            source_index
            if isinstance(source_index, int)
            and not isinstance(source_index, bool)
            and 1 <= source_index <= 12
            else index + 1
        )
        fallback_id = f"evidence-{source_token}-{index + 1}"
        base_id = (
            requested
            if isinstance(requested, str) and _EVIDENCE_ID_RE.fullmatch(requested)
            else fallback_id
        )
        evidence_id = base_id
        suffix = 2
        while evidence_id in used_ids or not _EVIDENCE_ID_RE.fullmatch(evidence_id):
            evidence_id = f"{fallback_id}-{suffix}"
            suffix += 1
        used_ids.add(evidence_id)
        row["evidence_id"] = evidence_id
        # Normalized/provider rows are model output, never proof of user review.
        row["review_status"] = "pending"
        normalized.append(row)
    return normalized


def validate_saved_reviewed_image_evidence(
    operation: ReverseOperation,
    payload: dict[str, Any],
    *,
    require_all_confirmed: bool = False,
) -> None:
    """Validate persisted review state with the same parser used for masks."""
    if not isinstance(payload, dict):
        raise ReviewedEvidenceMaskError("反推结果版本 payload 必须是对象")
    _reviewed_regions(
        payload,
        len(_operation_sources(operation)),
        strict_persisted=True,
        require_all_confirmed=require_all_confirmed,
    )


def _asset_identity(value: str | None) -> tuple[str, str]:
    normalized = str(value or "").strip()
    if not normalized:
        return "", ""
    key = storage.key_from_url(normalized)
    return ("local", key) if key else ("url", normalized)


def _operation_sources(operation: ReverseOperation) -> list[str]:
    context = operation.request_context if isinstance(operation.request_context, dict) else {}
    rows = context.get("sources") if isinstance(context.get("sources"), list) else []
    sources = [
        str(row.get("asset_url") or "").strip()
        for row in rows
        if isinstance(row, dict) and str(row.get("asset_url") or "").strip()
    ]
    return sources or [str(operation.asset_url or "").strip()]


_SOURCE_HASH_CACHE: dict[str, tuple[tuple[int, int], str]] = {}
_SOURCE_HASH_CACHE_LIMIT = 256
_MISSING_SOURCE = "__missing__"


def _current_source_content_hash(source_url: str) -> str | None:
    """本地存储下计算当前源素材的内容哈希（按 size+mtime 缓存）。

    外部 URL 或对象存储直接返回 None（序列化路径上不做网络 IO，内容校验
    仍由生成时的 rasterize 兜底）；本地文件缺失返回 ``_MISSING_SOURCE``。
    """
    key = storage.key_from_url(str(source_url or "").strip())
    if not key or storage.is_object_storage_enabled():
        return None
    try:
        path = storage.local_path(key)
        stat = path.stat()
    except (OSError, ValueError):
        return _MISSING_SOURCE
    token = (int(stat.st_size), int(stat.st_mtime_ns))
    cached = _SOURCE_HASH_CACHE.get(key)
    if cached and cached[0] == token:
        return cached[1]
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return _MISSING_SOURCE
    value = digest.hexdigest()
    while len(_SOURCE_HASH_CACHE) >= _SOURCE_HASH_CACHE_LIMIT:
        _SOURCE_HASH_CACHE.pop(next(iter(_SOURCE_HASH_CACHE)))
    _SOURCE_HASH_CACHE[key] = (token, value)
    return value


def _readiness(status: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "reason": reason, **extra}


def _latest_reviewed_revision(
    db: Session, operation: ReverseOperation
) -> ReverseResultRevision | None:
    return db.execute(
        select(ReverseResultRevision)
        .where(
            ReverseResultRevision.operation_id == int(operation.id),
            ReverseResultRevision.user_id == int(operation.user_id),
            ReverseResultRevision.source.in_(["user_edit", "applied"]),
        )
        .order_by(ReverseResultRevision.version.desc())
        .limit(1)
    ).scalars().first()


def image_mask_readiness_for_operation(
    operation: ReverseOperation,
    *,
    db: Session | None = None,
    supported: bool = True,
) -> dict[str, Any]:
    """服务端权威的蒙版就绪状态，随反推 result/operation 回传给前端。

    与 ``resolve_reviewed_evidence_plan`` 使用同一套审阅解析逻辑，
    保证用户在标完区域证据后能看到真实的服务端校验结论，而不是恒为待校验。
    """
    pending = _readiness(
        "pending", "保存审阅版本后，由服务端校验源素材与证据；不会自动应用。"
    )
    if getattr(operation, "error_code", None) == "RESULT_EXPIRED":
        return _readiness("source_expired", "原始素材已过期，不能生成服务端蒙版。")
    if not supported:
        return _readiness("unsupported", "当前反推目标不支持服务端证据蒙版。")
    if db is None:
        try:
            db = object_session(operation)
        except Exception:  # noqa: BLE001 — 兼容测试中的非 ORM 对象
            db = None
    if db is None or getattr(operation, "id", None) is None:
        return pending
    try:
        with db.no_autoflush:
            revision = _latest_reviewed_revision(db, operation)
    except Exception:  # noqa: BLE001 — 查询失败时保持待校验，不阻塞序列化
        return pending
    if revision is None:
        return pending
    detail: dict[str, Any] = {
        "revision_id": int(revision.id),
        "revision_version": int(revision.version),
        "schema_version": MASK_PLAN_SCHEMA_VERSION,
    }
    sources = _operation_sources(operation)
    payload = revision.payload if isinstance(revision.payload, dict) else {}
    try:
        regions = _reviewed_regions(payload, len(sources))
    except ReviewedEvidenceMaskError as exc:
        return _readiness("degraded", f"已保存的审阅版本不能安全生成蒙版：{exc}", **detail)
    if not regions:
        return _readiness(
            "pending",
            "审阅版本尚未确认可执行区域，确认保护或可编辑区域后由服务端生成蒙版。",
            **detail,
        )
    if getattr(revision, "lineage_status", None) != reverse_lineage.VERIFIED:
        return _readiness(
            "degraded", "反推版本未完成血缘验证，服务端无法可靠生成蒙版。", **detail
        )
    for index in sorted({region.source_index for region in regions}):
        fingerprint = reverse_lineage.fingerprint_for_source_index(revision, index)
        if fingerprint is None:
            return _readiness(
                "degraded", "反推版本缺少素材内容指纹，服务端无法校验源素材。", **detail
            )
        current = _current_source_content_hash(sources[index - 1])
        if current == _MISSING_SOURCE:
            return _readiness(
                "source_expired", "原始素材已不可读，不能生成服务端蒙版。", **detail
            )
        if current is not None and current != str(fingerprint.get("content_sha256") or ""):
            return _readiness(
                "source_hash_changed",
                "源素材内容与证据版本不一致，请重新反推分析。",
                **detail,
            )
    return _readiness("ready", "服务端已校验证据版本与源素材内容。", **detail)


def resolve_reviewed_evidence_plan(
    db: Session,
    task: GenTask,
    *,
    edit_source_url: str | None,
) -> ReviewedEvidencePlan | None:
    """Resolve confirmed regions from the task's owned immutable source revision."""
    if task.reverse_operation_id is None and task.source_revision_id is None:
        return None
    if task.reverse_operation_id is None or task.source_revision_id is None:
        raise ReviewedEvidenceMaskError("生成任务的反推血缘不完整")
    return resolve_reviewed_evidence_plan_for_lineage(
        db,
        user_id=int(task.user_id),
        operation_id=int(task.reverse_operation_id),
        revision_id=int(task.source_revision_id),
        edit_source_url=edit_source_url,
    )


def resolve_reviewed_evidence_plan_for_lineage(
    db: Session,
    *,
    user_id: int,
    operation_id: int | None,
    revision_id: int | None,
    edit_source_url: str | None,
) -> ReviewedEvidencePlan | None:
    if operation_id is None and revision_id is None:
        return None
    if operation_id is None or revision_id is None:
        raise ReviewedEvidenceMaskError("反推任务和版本必须同时提供")
    operation = db.get(ReverseOperation, int(operation_id))
    revision = db.get(ReverseResultRevision, int(revision_id))
    if (
        operation is None
        or revision is None
        or int(operation.user_id) != int(user_id)
        or int(revision.user_id) != int(user_id)
        or int(revision.operation_id) != int(operation.id)
    ):
        raise ReviewedEvidenceMaskError("生成请求引用的反推版本无效")
    lineage_status = getattr(revision, "lineage_status", None)
    if lineage_status == reverse_lineage.VERIFIED:
        try:
            reverse_lineage.validate_revision_chain(
                db,
                revision,
                terminal_source="applied",
            )
        except reverse_lineage.ReverseLineageError as exc:
            raise ReviewedEvidenceMaskError(str(exc)) from exc
    payload = revision.payload if isinstance(revision.payload, dict) else {}
    sources = _operation_sources(operation)
    verified_lineage = lineage_status == reverse_lineage.VERIFIED
    regions = _reviewed_regions(
        payload,
        len(sources),
        strict_persisted=verified_lineage,
        require_all_confirmed=verified_lineage and revision.source == "applied",
    )
    if not regions:
        return None
    if revision.source not in {"user_edit", "applied"}:
        raise ReviewedEvidenceMaskError("未经用户审阅的反推版本不能生成蒙版")
    source_identity = _asset_identity(edit_source_url)
    source_index = next(
        (
            index
            for index, source_url in enumerate(sources, start=1)
            if _asset_identity(source_url) == source_identity
        ),
        None,
    )
    if source_index is None:
        raise ReviewedEvidenceMaskError("当前编辑源图与反推版本的参考图不一致")
    foreign = [region.evidence_id for region in regions if region.source_index != source_index]
    if foreign:
        raise ReviewedEvidenceMaskError(
            "当前图片网关只支持第一张编辑源图的单蒙版，"
            f"请取消其他参考图的蒙版区域: {', '.join(foreign[:5])}"
        )
    selected = [region for region in regions if region.source_index == source_index]
    protected = tuple(region for region in selected if region.protected)
    editable = tuple(region for region in selected if not region.protected)
    source_fingerprint = reverse_lineage.fingerprint_for_source_index(
        revision,
        source_index,
    )
    expected_content_hash = (
        str(source_fingerprint.get("content_sha256"))
        if source_fingerprint is not None
        else None
    )
    if lineage_status == reverse_lineage.VERIFIED and expected_content_hash is None:
        raise ReviewedEvidenceMaskError("反推版本缺少当前证据来源的内容指纹")
    return ReviewedEvidencePlan(
        operation_id=int(operation.id),
        revision_id=int(revision.id),
        revision_version=int(revision.version),
        source_index=source_index,
        source_url=sources[source_index - 1],
        protected_regions=protected,
        editable_regions=editable,
        source_content_hash=expected_content_hash,
    )


def _decode_reference_image(data_uri: str) -> Image.Image:
    try:
        header, encoded = str(data_uri or "").split(",", 1)
        if not header.lower().startswith("data:image/") or ";base64" not in header.lower():
            raise ValueError("not an image data URI")
        raw = base64.b64decode(encoded, validate=True)
        image = Image.open(io.BytesIO(raw))
        image.load()
        if image.width <= 0 or image.height <= 0:
            raise ValueError("empty image")
        return image
    except Exception as exc:  # noqa: BLE001
        raise ReviewedEvidenceMaskError("编辑源图无法用于蒙版栅格化") from exc


def _pixel_bbox(
    bbox: tuple[float, float, float, float],
    width: int,
    height: int,
) -> tuple[int, int, int, int]:
    x, y, region_width, region_height = bbox
    left = max(0, min(width - 1, math.floor(x * width)))
    top = max(0, min(height - 1, math.floor(y * height)))
    right = max(left, min(width - 1, math.ceil((x + region_width) * width) - 1))
    bottom = max(top, min(height - 1, math.ceil((y + region_height) * height) - 1))
    return left, top, right, bottom


def _draw_region(
    draw: ImageDraw.ImageDraw,
    region: ReviewedEvidenceRegion,
    *,
    width: int,
    height: int,
    fill: int,
) -> None:
    if region.polygon:
        points = [
            (
                max(0, min(width - 1, round(x * (width - 1)))),
                max(0, min(height - 1, round(y * (height - 1)))),
            )
            for x, y in region.polygon
        ]
        draw.polygon(points, fill=fill)
    elif region.bbox:
        draw.rectangle(_pixel_bbox(region.bbox, width, height), fill=fill)


def rasterize_reviewed_evidence_mask(
    plan: ReviewedEvidencePlan,
    *,
    reference_data_uri: str,
    reference_content_hash: str | None = None,
) -> ReviewedEvidenceMask:
    if plan.source_content_hash is not None:
        normalized_hash = str(reference_content_hash or "").strip().lower()
        if normalized_hash != plan.source_content_hash:
            raise ReviewedEvidenceMaskError("编辑源图内容已变更，不能复用旧反推证据或蒙版")
    reference = _decode_reference_image(reference_data_uri)
    width, height = reference.size
    # OpenAI edit masks use transparent pixels as editable and opaque pixels as preserved.
    default_alpha = 255 if plan.editable_regions else 0
    alpha = Image.new("L", (width, height), default_alpha)
    draw = ImageDraw.Draw(alpha)
    for region in plan.editable_regions:
        _draw_region(draw, region, width=width, height=height, fill=0)
    # Explicit protected regions always win overlapping conflicts.
    for region in plan.protected_regions:
        _draw_region(draw, region, width=width, height=height, fill=255)
    alpha_bytes = alpha.tobytes()
    protected_pixels = sum(1 for value in alpha_bytes if value > 0)
    total_pixels = max(1, len(alpha_bytes))
    mask = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    mask.putalpha(alpha)
    output = io.BytesIO()
    mask.save(output, format="PNG", optimize=False)
    raw = output.getvalue()
    opaque_bbox = alpha.getbbox()
    bbox = (
        (opaque_bbox[0], opaque_bbox[1], opaque_bbox[2] - 1, opaque_bbox[3] - 1)
        if opaque_bbox
        else None
    )
    return ReviewedEvidenceMask(
        data_uri=f"data:image/png;base64,{base64.b64encode(raw).decode('ascii')}",
        mode="reviewed_evidence",
        confidence=1.0,
        bbox=bbox,
        width=width,
        height=height,
        reason="reverse_revision_reviewed_evidence",
        mask_hash=hashlib.sha256(raw).hexdigest(),
        protected_fraction=protected_pixels / total_pixels,
        editable_fraction=(total_pixels - protected_pixels) / total_pixels,
        plan=plan,
    )


def attach_mask_metadata_to_generation_revision(
    db: Session,
    task: GenTask,
    mask: ReviewedEvidenceMask,
) -> None:
    if task.generation_revision_id is None:
        return
    revision = db.get(ReverseResultRevision, int(task.generation_revision_id))
    if (
        revision is None
        or revision.source != "generation"
        or int(revision.user_id) != int(task.user_id)
        or int(revision.operation_id) != int(mask.plan.operation_id)
    ):
        raise ReviewedEvidenceMaskError("生成版本与证据蒙版血缘不一致")
    payload = dict(revision.payload or {})
    payload["image_mask"] = {
        "schema_version": MASK_PLAN_SCHEMA_VERSION,
        "source_revision_id": mask.plan.revision_id,
        "source_revision_version": mask.plan.revision_version,
        "source_index": mask.plan.source_index,
        "source_content_hash": mask.plan.source_content_hash,
        "evidence_ids": mask.plan.evidence_ids,
        "mask_hash": mask.mask_hash,
        "width": mask.width,
        "height": mask.height,
        "protected_fraction": round(mask.protected_fraction, 6),
        "editable_fraction": round(mask.editable_fraction, 6),
    }
    revision.payload = payload
    revision.payload_hash = reverse_lineage.canonical_payload_hash(payload)
