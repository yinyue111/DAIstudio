"""Explainable source-versus-generation reproduction assessment service."""
from __future__ import annotations

import hashlib
import io
import json
import logging
from copy import deepcopy
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, UnidentifiedImageError
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import (
    GenAsset,
    GenTask,
    ModelCapabilityVersion,
    ModelConfig,
    ModelPriceVersion,
    ReproductionAssessment,
    ReproductionCorrection,
    ReproductionFinding,
    ReverseOperation,
    ReverseResultRevision,
    UploadedAsset,
)
from ..reproduction_schemas import (
    ReproductionAssessmentCreateIn,
    ReproductionCorrectionCreateIn,
)
from . import (
    image_evidence_analysis,
    reverse_operations,
    storage,
    video_audio,
    video_evidence_analysis,
    video_frames,
)
from .user_assets import (
    AssetNotFound,
    InvalidAssetRef,
    ResolvedAsset,
    resolve_asset_ref,
)

log = logging.getLogger(__name__)

TERMINAL_STATUSES = frozenset({"succeeded", "partial", "failed", "canceled"})
CORRECTABLE_STATUSES = frozenset({"succeeded", "partial"})
_IMAGE_MAX_EDGE = 1024
_VIDEO_SAMPLE_COUNT = 8
_SCORE_THRESHOLD = 0.82


class ReproductionAssessmentError(ValueError):
    status_code = 422
    code = "REPRODUCTION_ASSESSMENT_INVALID"


class ReproductionAssessmentNotFound(ReproductionAssessmentError):
    status_code = 404
    code = "REPRODUCTION_ASSESSMENT_NOT_FOUND"


class ReproductionAssessmentConflict(ReproductionAssessmentError):
    status_code = 409
    code = "REPRODUCTION_ASSESSMENT_CONFLICT"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _request_fingerprint(body: Any) -> str:
    payload = body.model_dump(mode="json", exclude={"idempotency_key"})
    return _canonical_hash(payload)


def _media_type(resolved: ResolvedAsset) -> str:
    if resolved.origin == "generated":
        assert isinstance(resolved.row, GenAsset)
        return str(resolved.row.type)
    assert isinstance(resolved.row, UploadedAsset)
    return "video" if resolved.row.key.startswith("upload_video/") else "image"


def _safe_resolve(db: Session, *, user_id: int, asset_ref: str) -> ResolvedAsset:
    try:
        return resolve_asset_ref(db, user_id, asset_ref)
    except InvalidAssetRef as exc:
        raise ReproductionAssessmentError(str(exc)) from exc
    except AssetNotFound as exc:
        raise ReproductionAssessmentNotFound("资产不存在") from exc


def _storage_key(url: str | None) -> str | None:
    return storage.key_from_url(str(url or "")) if url else None


def _generated_descriptor(asset: GenAsset, *, side: str) -> tuple[dict[str, Any], list[str]]:
    if asset.moderation_status != "active":
        raise ReproductionAssessmentNotFound("资产不存在")
    warnings: list[str] = []
    selected_key = None
    variant = None
    if asset.unlocked and asset.hd_url:
        hd_key = _storage_key(asset.hd_url)
        if hd_key and storage.exists(hd_key):
            selected_key = hd_key
            variant = "hd"
    if selected_key is None:
        preview_key = _storage_key(asset.preview_url)
        if preview_key and storage.exists(preview_key):
            selected_key = preview_key
            variant = "preview"
            label = "源素材" if side == "source" else "生成结果"
            warnings.append(f"{label}未授权或无可用 HD，本次使用预览版评估")
    if selected_key is None:
        raise ReproductionAssessmentError("生成资产没有可用的已授权媒体文件")
    return (
        {
            "asset_ref": f"g.{int(asset.id)}",
            "origin": "generated",
            "asset_id": int(asset.id),
            "media_type": str(asset.type),
            "variant": variant,
            "storage_key": selected_key,
            "unlocked": bool(asset.unlocked),
            "watermarked": bool(asset.watermarked),
            "quality_status": "full" if variant == "hd" else "degraded",
        },
        warnings,
    )


def _asset_descriptor(resolved: ResolvedAsset, *, side: str) -> tuple[dict[str, Any], list[str]]:
    if resolved.origin == "generated":
        assert isinstance(resolved.row, GenAsset)
        return _generated_descriptor(resolved.row, side=side)
    assert isinstance(resolved.row, UploadedAsset)
    if not storage.exists(resolved.row.key):
        raise ReproductionAssessmentError("上传资产文件不存在")
    return (
        {
            "asset_ref": resolved.asset_ref,
            "origin": "uploaded",
            "storage_key": resolved.row.key,
            "media_type": _media_type(resolved),
            "variant": "original",
            "quality_status": "full",
        },
        [],
    )


def _existing_id(db: Session, model, value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 and db.get(model, parsed) is not None else None


def _owned_lineage(
    db: Session,
    *,
    user_id: int,
    body: ReproductionAssessmentCreateIn,
    generated: GenAsset,
    media_type: str,
) -> tuple[dict[str, Any], dict[str, int | None]]:
    asset_task_id = int(generated.task_id) if generated.task_id is not None else None
    if body.generation_task_id is not None and asset_task_id != int(body.generation_task_id):
        raise ReproductionAssessmentError("生成任务与 generated_asset_ref 不匹配")
    generation_task_id = int(body.generation_task_id or asset_task_id or 0) or None
    task = db.get(GenTask, generation_task_id) if generation_task_id else None
    if generation_task_id and (
        task is None
        or int(task.user_id) != int(user_id)
        or str(task.category) != media_type
    ):
        raise ReproductionAssessmentNotFound("生成任务不存在")
    if task is not None and task.status not in {"succeeded", "needs_review"}:
        raise ReproductionAssessmentError("只能评估已产生结果的生成任务")

    task_operation_id = int(task.reverse_operation_id) if task and task.reverse_operation_id else None
    if (
        body.reverse_operation_id is not None
        and task_operation_id is not None
        and int(body.reverse_operation_id) != task_operation_id
    ):
        raise ReproductionAssessmentError("反推任务与生成任务血缘不匹配")
    reverse_operation_id = int(body.reverse_operation_id or task_operation_id or 0) or None

    task_revision_id = None
    if task is not None:
        # Corrections branch from the editable reverse result that was supplied
        # to generation. generation_revision_id is immutable compiled/runtime
        # provenance and cannot be a user-edit correction parent.
        task_revision_id = task.source_revision_id
        task_revision_id = int(task_revision_id) if task_revision_id else None
    if (
        body.reverse_revision_id is not None
        and task_revision_id is not None
        and int(body.reverse_revision_id) != task_revision_id
    ):
        raise ReproductionAssessmentError("反推结果版本与生成任务血缘不匹配")
    reverse_revision_id = int(body.reverse_revision_id or task_revision_id or 0) or None
    operation = db.get(ReverseOperation, reverse_operation_id) if reverse_operation_id else None
    revision = db.get(ReverseResultRevision, reverse_revision_id) if reverse_revision_id else None
    if reverse_operation_id and (
        operation is None
        or int(operation.user_id) != int(user_id)
        or str(operation.target) != media_type
    ):
        raise ReproductionAssessmentNotFound("反推任务不存在")
    if reverse_revision_id and (
        revision is None
        or int(revision.user_id) != int(user_id)
        or reverse_operation_id is None
        or int(revision.operation_id) != reverse_operation_id
    ):
        raise ReproductionAssessmentNotFound("反推结果版本不存在")

    model_snapshot = {}
    if task is not None and isinstance(task.params, dict):
        raw_snapshot = task.params.get("_model_snapshot")
        if isinstance(raw_snapshot, dict):
            model_snapshot = deepcopy(raw_snapshot)
    model_config_id = _existing_id(
        db,
        ModelConfig,
        task.model_config_id if task is not None else model_snapshot.get("model_config_id"),
    )
    capability_version_id = _existing_id(
        db, ModelCapabilityVersion, model_snapshot.get("capability_version_id")
    )
    price_version_id = _existing_id(db, ModelPriceVersion, model_snapshot.get("price_version_id"))
    lineage = {
        "reverse_operation_id": reverse_operation_id,
        "reverse_revision_id": reverse_revision_id,
        "generation_task_id": generation_task_id,
        "generation_source_revision_id": (
            int(task.source_revision_id) if task and task.source_revision_id else None
        ),
        "compiled_revision_id": (
            int(task.compiled_revision_id) if task and task.compiled_revision_id else None
        ),
        "generation_revision_id": (
            int(task.generation_revision_id) if task and task.generation_revision_id else None
        ),
        "generated_asset_id": int(generated.id),
        "model_config_id": model_config_id,
        "capability_version_id": capability_version_id,
        "price_version_id": price_version_id,
        "model_snapshot": model_snapshot,
    }
    return lineage, {
        "reverse_operation_id": reverse_operation_id,
        "reverse_revision_id": reverse_revision_id,
        "generation_task_id": generation_task_id,
        "model_config_id": model_config_id,
        "capability_version_id": capability_version_id,
        "price_version_id": price_version_id,
    }


def create_assessment(
    db: Session,
    *,
    user_id: int,
    body: ReproductionAssessmentCreateIn,
) -> tuple[ReproductionAssessment, bool]:
    fingerprint = _request_fingerprint(body)
    existing = db.scalar(
        select(ReproductionAssessment).where(
            ReproductionAssessment.user_id == int(user_id),
            ReproductionAssessment.idempotency_key == body.idempotency_key,
        )
    )
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise ReproductionAssessmentConflict("幂等 key 已用于不同评估请求")
        return existing, False
    source = _safe_resolve(db, user_id=user_id, asset_ref=body.source_asset_ref)
    generated_resolved = _safe_resolve(
        db, user_id=user_id, asset_ref=body.generated_asset_ref
    )
    if generated_resolved.origin != "generated" or not isinstance(
        generated_resolved.row, GenAsset
    ):
        raise ReproductionAssessmentError("生成结果必须是 GenAsset 生成资产")
    if body.source_asset_ref == body.generated_asset_ref:
        raise ReproductionAssessmentError("源素材和生成结果不能是同一资产")
    source_type = _media_type(source)
    generated_type = _media_type(generated_resolved)
    if source_type != generated_type:
        raise ReproductionAssessmentError("源素材与生成结果的媒体类型必须一致")

    source_snapshot, source_warnings = _asset_descriptor(source, side="source")
    generated_snapshot, generated_warnings = _asset_descriptor(
        generated_resolved, side="generated"
    )
    generated = generated_resolved.row
    lineage, lineage_columns = _owned_lineage(
        db,
        user_id=user_id,
        body=body,
        generated=generated,
        media_type=source_type,
    )
    asset_snapshot = {
        "source": source_snapshot,
        "generated": generated_snapshot,
        "quality_status": (
            "degraded"
            if "degraded"
            in {source_snapshot["quality_status"], generated_snapshot["quality_status"]}
            else "full"
        ),
    }
    row = ReproductionAssessment(
        user_id=int(user_id),
        idempotency_key=body.idempotency_key,
        request_fingerprint=fingerprint,
        source_asset_ref=body.source_asset_ref,
        generated_asset_ref=body.generated_asset_ref,
        generated_asset_id=int(generated.id),
        media_type=source_type,
        status="queued",
        phase="queued",
        progress=0,
        cancel_requested=False,
        cost_credits=0,
        asset_snapshot=asset_snapshot,
        lineage_snapshot=lineage,
        metrics={},
        analyzers={},
        warnings=list(dict.fromkeys([*source_warnings, *generated_warnings])),
        **lineage_columns,
    )
    try:
        db.add(row)
        db.commit()
        db.refresh(row)
    except IntegrityError as exc:
        db.rollback()
        replay = db.scalar(
            select(ReproductionAssessment).where(
                ReproductionAssessment.user_id == int(user_id),
                ReproductionAssessment.idempotency_key == body.idempotency_key,
            )
        )
        if replay is not None and replay.request_fingerprint == fingerprint:
            return replay, False
        raise ReproductionAssessmentConflict("评估请求已并发更新，请刷新后重试") from exc
    return row, True


def enqueue_assessment(assessment_id: int) -> None:
    from ..tasks import run_reproduction_assessment_task

    run_reproduction_assessment_task.delay(int(assessment_id))


def mark_enqueue_failed(
    db: Session,
    *,
    assessment: ReproductionAssessment,
    exc: Exception,
) -> ReproductionAssessment:
    """Persist a terminal state when publishing the durable job fails.

    The row has already committed before Celery publish, so leaving it queued
    would hide a job that can never execute. A caller can submit the same
    source pair again with a new idempotency key after the broker recovers.
    """
    if assessment.status not in TERMINAL_STATUSES:
        assessment.status = "failed"
        assessment.phase = "enqueue_failed"
        assessment.progress = 0
        assessment.error_code = "ASSESSMENT_ENQUEUE_FAILED"
        assessment.error = f"评估任务入队失败，请重试：{str(exc)[:1500]}"
        assessment.finished_at = utcnow()
        db.commit()
        db.refresh(assessment)
    return assessment


def _owned_assessment(db: Session, *, assessment_id: int, user_id: int) -> ReproductionAssessment:
    row = db.scalar(
        select(ReproductionAssessment).where(
            ReproductionAssessment.id == int(assessment_id),
            ReproductionAssessment.user_id == int(user_id),
        )
    )
    if row is None:
        raise ReproductionAssessmentNotFound("复刻度评估不存在")
    return row


def _serialize_finding(row: ReproductionFinding) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "finding_key": row.finding_key,
        "position": int(row.position),
        "dimension": row.dimension,
        "kind": row.kind,
        "severity": row.severity,
        "confidence": float(row.confidence) if row.confidence is not None else None,
        "message": row.message,
        "bbox": deepcopy(row.bbox),
        "time_range": deepcopy(row.time_range),
        "shot_id": row.shot_id,
        "evidence": deepcopy(row.evidence or {}),
        "metrics": deepcopy(row.metrics or {}),
    }


def serialize_assessment(db: Session, row: ReproductionAssessment) -> dict[str, Any]:
    findings = list(
        db.scalars(
            select(ReproductionFinding)
            .where(ReproductionFinding.assessment_id == int(row.id))
            .order_by(ReproductionFinding.position.asc(), ReproductionFinding.id.asc())
        )
    )
    return {
        "id": int(row.id),
        "status": row.status,
        "phase": row.phase,
        "progress": int(row.progress),
        "media_type": row.media_type,
        "source_asset_ref": row.source_asset_ref,
        "generated_asset_ref": row.generated_asset_ref,
        "cost_credits": int(row.cost_credits),
        "cancel_requested": bool(row.cancel_requested),
        "schema_version": row.schema_version,
        "asset_snapshot": deepcopy(row.asset_snapshot or {}),
        "lineage": deepcopy(row.lineage_snapshot or {}),
        "metrics": deepcopy(row.metrics or {}),
        "analyzers": deepcopy(row.analyzers or {}),
        "warnings": list(row.warnings or []),
        "error_code": row.error_code,
        "error": row.error,
        "findings": [_serialize_finding(item) for item in findings],
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def get_assessment(db: Session, *, assessment_id: int, user_id: int) -> dict[str, Any]:
    return serialize_assessment(
        db,
        _owned_assessment(db, assessment_id=assessment_id, user_id=user_id),
    )


def list_assessments(
    db: Session,
    *,
    user_id: int,
    status: str | None,
    media_type: str | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    filters = [ReproductionAssessment.user_id == int(user_id)]
    if status:
        filters.append(ReproductionAssessment.status == status)
    if media_type:
        filters.append(ReproductionAssessment.media_type == media_type)
    total = int(
        db.scalar(select(func.count()).select_from(ReproductionAssessment).where(*filters)) or 0
    )
    rows = list(
        db.scalars(
            select(ReproductionAssessment)
            .where(*filters)
            .order_by(ReproductionAssessment.id.desc())
            .limit(int(limit))
            .offset(int(offset))
        )
    )
    return {
        "items": [serialize_assessment(db, row) for row in rows],
        "total": total,
        "limit": int(limit),
        "offset": int(offset),
    }


def request_cancel(
    db: Session,
    *,
    assessment_id: int,
    user_id: int,
) -> ReproductionAssessment:
    row = _owned_assessment(db, assessment_id=assessment_id, user_id=user_id)
    if row.status == "canceled":
        return row
    if row.status in TERMINAL_STATUSES:
        raise ReproductionAssessmentConflict("终态评估不能取消")
    row.cancel_requested = True
    if row.status == "queued":
        row.status = "canceled"
        row.phase = "canceled"
        row.finished_at = utcnow()
    db.commit()
    db.refresh(row)
    return row


def _analysis_path(row: ReproductionAssessment, side: str) -> Path:
    snapshot = row.asset_snapshot if isinstance(row.asset_snapshot, dict) else {}
    descriptor = snapshot.get(side) if isinstance(snapshot.get(side), dict) else {}
    key = str(descriptor.get("storage_key") or "")
    if not key:
        raise ReproductionAssessmentError(f"{side} 缺少存储快照")
    try:
        path = storage.download_to_local_temp(key)
    except (ValueError, storage.StorageUnavailable) as exc:
        raise ReproductionAssessmentError(f"{side} 资产无法物化") from exc
    if not path.exists() or not path.is_file():
        raise ReproductionAssessmentError(f"{side} 资产文件不存在")
    return path


def _open_image(value: Path | bytes) -> Image.Image:
    try:
        source = io.BytesIO(value) if isinstance(value, bytes) else value
        image = Image.open(source).convert("RGB")
        image.load()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ReproductionAssessmentError("资产不是可解码的图片") from exc
    image.thumbnail((_IMAGE_MAX_EDGE, _IMAGE_MAX_EDGE), Image.Resampling.LANCZOS)
    return image


def _dhash_similarity(source_gray: np.ndarray, generated_gray: np.ndarray) -> float:
    source_small = cv2.resize(source_gray, (9, 8), interpolation=cv2.INTER_AREA)
    generated_small = cv2.resize(generated_gray, (9, 8), interpolation=cv2.INTER_AREA)
    source_bits = source_small[:, 1:] > source_small[:, :-1]
    generated_bits = generated_small[:, 1:] > generated_small[:, :-1]
    return float(1.0 - np.mean(source_bits != generated_bits))


def _image_pair(source_image: Image.Image, generated_image: Image.Image) -> dict[str, Any]:
    source_rgb = np.asarray(source_image.convert("RGB"), dtype=np.uint8)
    generated_resized = generated_image.convert("RGB").resize(
        source_image.size, Image.Resampling.LANCZOS
    )
    generated_rgb = np.asarray(generated_resized, dtype=np.uint8)
    source_gray = cv2.cvtColor(source_rgb, cv2.COLOR_RGB2GRAY)
    generated_gray = cv2.cvtColor(generated_rgb, cv2.COLOR_RGB2GRAY)

    gray_similarity = 1.0 - float(
        np.mean(np.abs(source_gray.astype(np.float32) - generated_gray.astype(np.float32)))
        / 255.0
    )
    source_edges = cv2.Canny(source_gray, 60, 160) > 0
    generated_edges = cv2.Canny(generated_gray, 60, 160) > 0
    edge_total = int(source_edges.sum() + generated_edges.sum())
    edge_dice = (
        1.0
        if edge_total == 0
        else float(2 * np.logical_and(source_edges, generated_edges).sum() / edge_total)
    )
    dhash_similarity = _dhash_similarity(source_gray, generated_gray)
    source_ratio = source_image.width / max(1, source_image.height)
    generated_ratio = generated_image.width / max(1, generated_image.height)
    aspect_ratio_score = min(source_ratio, generated_ratio) / max(source_ratio, generated_ratio)
    structure_score = float(
        np.clip(
            0.45 * gray_similarity
            + 0.30 * edge_dice
            + 0.15 * dhash_similarity
            + 0.10 * aspect_ratio_score,
            0,
            1,
        )
    )

    source_lab = cv2.cvtColor(source_rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
    generated_lab = cv2.cvtColor(generated_rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
    color_score = float(
        np.clip(1.0 - np.mean(np.abs(source_lab - generated_lab)) / 255.0, 0, 1)
    )
    source_lap = cv2.Laplacian(source_gray, cv2.CV_32F)
    generated_lap = cv2.Laplacian(generated_gray, cv2.CV_32F)
    source_sharpness = float(np.std(source_lap))
    generated_sharpness = float(np.std(generated_lap))
    sharpness_score = (
        1.0
        if max(source_sharpness, generated_sharpness) < 1e-6
        else min(source_sharpness, generated_sharpness)
        / max(source_sharpness, generated_sharpness)
    )
    gradient_similarity = 1.0 - float(
        np.mean(np.abs(np.abs(source_lap) - np.abs(generated_lap))) / 255.0
    )
    detail_score = float(np.clip(0.6 * sharpness_score + 0.4 * gradient_similarity, 0, 1))
    diff = np.mean(
        np.abs(source_rgb.astype(np.float32) - generated_rgb.astype(np.float32)), axis=2
    )
    return {
        "structure_layout": round(structure_score, 6),
        "color_light": round(color_score, 6),
        "detail_material": round(detail_score, 6),
        "components": {
            "gray_similarity": round(float(np.clip(gray_similarity, 0, 1)), 6),
            "edge_dice": round(float(np.clip(edge_dice, 0, 1)), 6),
            "perceptual_hash_similarity": round(float(np.clip(dhash_similarity, 0, 1)), 6),
            "aspect_ratio_similarity": round(float(np.clip(aspect_ratio_score, 0, 1)), 6),
            "source_sharpness": round(source_sharpness, 6),
            "generated_sharpness": round(generated_sharpness, 6),
        },
        "diff": diff,
    }


def _heatmap(diff: np.ndarray) -> dict[str, Any]:
    width = min(24, max(8, int(diff.shape[1] // 32) or 8))
    height = min(24, max(8, int(diff.shape[0] // 32) or 8))
    resized = cv2.resize(diff / 255.0, (width, height), interpolation=cv2.INTER_AREA)
    return {
        "width": width,
        "height": height,
        "values": np.round(np.clip(resized, 0, 1), 4).tolist(),
    }


def _difference_regions(diff: np.ndarray) -> list[dict[str, float]]:
    threshold = max(20.0, float(np.percentile(diff, 70)))
    mask = (diff >= threshold).astype(np.uint8) * 255
    kernel = np.ones((3, 3), dtype=np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, 8)
    height, width = diff.shape[:2]
    minimum_area = max(4, int(width * height * 0.005))
    regions: list[tuple[int, dict[str, float]]] = []
    for index in range(1, count):
        x, y, region_width, region_height, area = (int(value) for value in stats[index])
        if area < minimum_area:
            continue
        regions.append(
            (
                area,
                {
                    "x": round(x / width, 6),
                    "y": round(y / height, 6),
                    "width": round(region_width / width, 6),
                    "height": round(region_height / height, 6),
                },
            )
        )
    regions.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in regions[:5]] or [
        {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}
    ]


def _severity(score: float) -> str:
    if score < 0.35:
        return "critical"
    if score < 0.55:
        return "high"
    if score < 0.72:
        return "medium"
    return "low"


def _analyzer_record(
    *,
    status: str,
    version: str,
    reason: str | None = None,
    **metadata: Any,
) -> dict[str, Any]:
    return {
        "status": status,
        "version": str(version or "unknown"),
        "reason": reason,
        **metadata,
    }


def _ocr_record(result: dict[str, Any]) -> dict[str, Any]:
    return _analyzer_record(
        status=str(result.get("status") or "degraded"),
        version=str(result.get("analyzer_version") or "unknown"),
        reason=str(result.get("degraded_reason") or "") or None,
        analyzer=str(result.get("analyzer") or "tesseract_tsv"),
        evidence_count=int(result.get("evidence_count") or 0),
    )


def _visible_text(result: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    rows = [item for item in result.get("evidence") or [] if isinstance(item, dict)]
    text = " ".join(str(item.get("evidence_text") or "").strip() for item in rows).strip()
    return text, rows


def _ocr_dimension(
    source_result: dict[str, Any], generated_result: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    source_status = str(source_result.get("status") or "degraded")
    generated_status = str(generated_result.get("status") or "degraded")
    if source_status != "analyzed" or generated_status != "analyzed":
        reason = "; ".join(
            str(item.get("degraded_reason") or "")
            for item in (source_result, generated_result)
            if item.get("degraded_reason")
        ) or "OCR 分析器不可用"
        status = "unsupported" if "unsupported" in {source_status, generated_status} else "degraded"
        return {"status": status, "score": None, "reason": reason}, None
    source_text, source_rows = _visible_text(source_result)
    generated_text, generated_rows = _visible_text(generated_result)
    score = float(SequenceMatcher(None, source_text.casefold(), generated_text.casefold()).ratio())
    dimension = {
        "status": "analyzed",
        "score": round(float(np.clip(score, 0, 1)), 6),
        "source_text": source_text,
        "generated_text": generated_text,
        "source_evidence_count": len(source_rows),
        "generated_evidence_count": len(generated_rows),
    }
    if score >= 0.95:
        return dimension, None
    bbox = source_rows[0].get("bbox") if source_rows else None
    return dimension, {
        "finding_key": "image-ocr-text",
        "dimension": "ocr_text",
        "kind": "text_mismatch",
        "severity": _severity(score),
        "confidence": round(1.0 - score, 6),
        "message": "画面文字与源素材不一致",
        "bbox": bbox or {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
        "time_range": None,
        "shot_id": None,
        "evidence": {"source_text": source_text, "generated_text": generated_text},
        "metrics": {"score": round(score, 6)},
    }


def _region_bbox(row: dict[str, Any]) -> dict[str, float] | None:
    bbox = row.get("bbox")
    if isinstance(bbox, dict):
        try:
            return {
                key: float(bbox[key])
                for key in ("x", "y", "width", "height")
            }
        except (KeyError, TypeError, ValueError):
            return None
    polygon = row.get("polygon")
    if not isinstance(polygon, list) or len(polygon) < 3:
        return None
    try:
        xs = [float(point["x"]) for point in polygon]
        ys = [float(point["y"]) for point in polygon]
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "x": min(xs),
        "y": min(ys),
        "width": max(xs) - min(xs),
        "height": max(ys) - min(ys),
    }


def _bbox_iou(left: dict[str, float] | None, right: dict[str, float] | None) -> float:
    if left is None or right is None:
        return 0.0
    lx1, ly1 = left["x"], left["y"]
    rx1, ry1 = right["x"], right["y"]
    lx2, ly2 = lx1 + left["width"], ly1 + left["height"]
    rx2, ry2 = rx1 + right["width"], ry1 + right["height"]
    intersection = max(0.0, min(lx2, rx2) - max(lx1, rx1)) * max(
        0.0, min(ly2, ry2) - max(ly1, ry1)
    )
    union = left["width"] * left["height"] + right["width"] * right["height"] - intersection
    return intersection / union if union > 0 else 0.0


def _normalized_event_center(row: dict[str, Any], duration: float | None) -> float | None:
    if not duration or duration <= 0:
        return None
    try:
        start = float(row.get("start_seconds", row.get("timestamp_seconds")))
        end = float(row.get("end_seconds", row.get("timestamp_seconds")))
    except (TypeError, ValueError):
        return None
    return float(np.clip(((start + end) / 2) / duration, 0, 1))


def _labeled_evidence_similarity(
    source_rows: list[dict[str, Any]],
    generated_rows: list[dict[str, Any]],
    *,
    source_duration: float | None = None,
    generated_duration: float | None = None,
    spatial_weight: float = 0.0,
    temporal_weight: float = 0.0,
) -> dict[str, Any]:
    def prepared(rows: list[dict[str, Any]], duration: float | None) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for row in rows:
            label = " ".join(str(row.get("label") or "").casefold().split())
            if not label:
                continue
            normalized.append(
                {
                    "label": label,
                    "bbox": _region_bbox(row),
                    "time": _normalized_event_center(row, duration),
                    "raw": row,
                }
            )
        return normalized

    source = prepared(source_rows, source_duration)
    generated = prepared(generated_rows, generated_duration)
    if not source and not generated:
        return {
            "status": "not_applicable",
            "score": None,
            "reason": "分析器未在两侧检测到可比较的语义对象",
            "source_labels": [],
            "generated_labels": [],
        }

    available = set(range(len(generated)))
    matches: list[tuple[int, int, float, float]] = []
    for source_index, source_row in enumerate(source):
        candidates: list[tuple[float, int, float, float]] = []
        for generated_index in available:
            generated_row = generated[generated_index]
            if source_row["label"] != generated_row["label"]:
                continue
            spatial = _bbox_iou(source_row["bbox"], generated_row["bbox"])
            temporal = (
                1.0 - abs(float(source_row["time"]) - float(generated_row["time"]))
                if source_row["time"] is not None and generated_row["time"] is not None
                else 0.0
            )
            candidates.append((spatial_weight * spatial + temporal_weight * temporal, generated_index, spatial, temporal))
        if not candidates:
            continue
        _rank, generated_index, spatial, temporal = max(candidates, key=lambda item: item[0])
        available.remove(generated_index)
        matches.append((source_index, generated_index, spatial, temporal))

    matched_count = len(matches)
    label_f1 = (
        2 * matched_count / (len(source) + len(generated))
        if source or generated
        else 1.0
    )
    spatial_score = (
        float(np.mean([item[2] for item in matches])) if matches and spatial_weight else None
    )
    temporal_score = (
        float(np.mean([item[3] for item in matches])) if matches and temporal_weight else None
    )
    label_weight = max(0.0, 1.0 - spatial_weight - temporal_weight)
    score = label_weight * label_f1
    if spatial_weight:
        score += spatial_weight * float(spatial_score or 0)
    if temporal_weight:
        score += temporal_weight * float(temporal_score or 0)
    matched_source = {item[0] for item in matches}
    unmatched_source = [
        source[index]["raw"] for index in range(len(source)) if index not in matched_source
    ]
    return {
        "status": "analyzed",
        "score": round(float(np.clip(score, 0, 1)), 6),
        "label_f1": round(float(np.clip(label_f1, 0, 1)), 6),
        "spatial_score": (
            round(float(np.clip(spatial_score, 0, 1)), 6)
            if spatial_score is not None
            else None
        ),
        "temporal_score": (
            round(float(np.clip(temporal_score, 0, 1)), 6)
            if temporal_score is not None
            else None
        ),
        "source_labels": [item["label"] for item in source],
        "generated_labels": [item["label"] for item in generated],
        "matched_count": matched_count,
        "unmatched_source": unmatched_source,
    }


def _pair_failure_status(
    source_results: list[dict[str, Any]], generated_results: list[dict[str, Any]]
) -> tuple[str, str]:
    rows = [*source_results, *generated_results]
    statuses = {str(row.get("status") or "degraded") for row in rows}
    reasons = list(
        dict.fromkeys(
            str(row.get("degraded_reason") or "").strip()
            for row in rows
            if str(row.get("degraded_reason") or "").strip()
        )
    )
    status = "unsupported" if statuses == {"unsupported"} else "degraded"
    return status, "; ".join(reasons) or "语义分析证据不完整"


def _image_subject_semantics(
    source_image: Image.Image, generated_image: Image.Image
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    source_results: dict[str, dict[str, Any]] = {}
    generated_results: dict[str, dict[str, Any]] = {}
    comparisons: dict[str, dict[str, Any]] = {}
    for capability in ("detector", "segmenter"):
        source_results[capability] = image_evidence_analysis.http_region_provider(
            source_image, capability=capability
        )
        generated_results[capability] = image_evidence_analysis.http_region_provider(
            generated_image, capability=capability
        )
        if (
            source_results[capability].get("status") == "analyzed"
            and generated_results[capability].get("status") == "analyzed"
        ):
            comparisons[capability] = _labeled_evidence_similarity(
                [row for row in source_results[capability].get("evidence") or [] if isinstance(row, dict)],
                [row for row in generated_results[capability].get("evidence") or [] if isinstance(row, dict)],
                spatial_weight=0.25,
            )

    scored = [row for row in comparisons.values() if row.get("status") == "analyzed"]
    if scored:
        score = round(float(np.mean([float(row["score"]) for row in scored])), 6)
        dimension = {
            "status": "analyzed",
            "score": score,
            "capabilities": comparisons,
        }
        analyzer_status, reason = "analyzed", None
    elif comparisons and all(row.get("status") == "not_applicable" for row in comparisons.values()):
        dimension = {
            "status": "not_applicable",
            "score": None,
            "reason": "检测器和分割器均未在两侧发现可比较主体",
            "capabilities": comparisons,
        }
        analyzer_status, reason = "analyzed", None
        score = None
    else:
        analyzer_status, reason = _pair_failure_status(
            list(source_results.values()), list(generated_results.values())
        )
        dimension = {"status": analyzer_status, "score": None, "reason": reason}
        score = None

    analyzer = _analyzer_record(
        status=analyzer_status,
        version="region-semantic-compare-v1",
        reason=reason,
        capabilities={
            capability: {
                "source_status": source_results[capability].get("status"),
                "generated_status": generated_results[capability].get("status"),
                "source_analyzer": source_results[capability].get("analyzer"),
                "generated_analyzer": generated_results[capability].get("analyzer"),
                "source_version": source_results[capability].get("analyzer_version"),
                "generated_version": generated_results[capability].get("analyzer_version"),
                "source_evidence_count": len(source_results[capability].get("evidence") or []),
                "generated_evidence_count": len(generated_results[capability].get("evidence") or []),
            }
            for capability in ("detector", "segmenter")
        },
    )
    finding = None
    if score is not None and score < _SCORE_THRESHOLD:
        unmatched = next(
            (
                row
                for comparison in scored
                for row in comparison.get("unmatched_source") or []
                if isinstance(row, dict)
            ),
            None,
        )
        source_rows = [
            row
            for result in source_results.values()
            for row in result.get("evidence") or []
            if isinstance(row, dict)
        ]
        finding = {
            "finding_key": "image-subject-semantics",
            "dimension": "subject_semantics",
            "kind": "subject_mismatch",
            "severity": _severity(score),
            "confidence": round(1.0 - score, 6),
            "message": "主体类别、数量或位置与源素材不一致",
            "bbox": _region_bbox(unmatched or (source_rows[0] if source_rows else {}))
            or {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
            "time_range": None,
            "shot_id": None,
            "evidence": {
                "capabilities": {
                    key: {
                        "source_labels": value.get("source_labels", []),
                        "generated_labels": value.get("generated_labels", []),
                    }
                    for key, value in comparisons.items()
                }
            },
            "metrics": {"score": score},
        }
    return dimension, analyzer, finding


def _image_analysis(source_path: Path, generated_path: Path) -> dict[str, Any]:
    source_image = _open_image(source_path)
    generated_image = _open_image(generated_path)
    pair = _image_pair(source_image, generated_image)
    analyzers = {
        "pillow": _analyzer_record(status="analyzed", version=Image.__version__),
        "numpy": _analyzer_record(status="analyzed", version=np.__version__),
        "opencv": _analyzer_record(status="analyzed", version=cv2.__version__),
    }
    dimensions: dict[str, dict[str, Any]] = {
        "structure_layout": {
            "status": "analyzed",
            "score": pair["structure_layout"],
            "components": pair["components"],
        },
        "color_light": {"status": "analyzed", "score": pair["color_light"]},
        "detail_material": {"status": "analyzed", "score": pair["detail_material"]},
    }
    findings: list[dict[str, Any]] = []
    regions = _difference_regions(pair["diff"])
    structure_score = float(pair["structure_layout"])
    if structure_score < _SCORE_THRESHOLD:
        for index, bbox in enumerate(regions, start=1):
            findings.append(
                {
                    "finding_key": f"image-structure-{index}",
                    "dimension": "structure_layout",
                    "kind": "spatial_difference",
                    "severity": _severity(structure_score),
                    "confidence": round(1.0 - structure_score, 6),
                    "message": "构图或局部结构与源素材存在明显差异",
                    "bbox": bbox,
                    "time_range": None,
                    "shot_id": None,
                    "evidence": {"heatmap_region": bbox},
                    "metrics": {"score": round(structure_score, 6)},
                }
            )
    for dimension, message in (
        ("color_light", "色彩或光线与源素材不一致"),
        ("detail_material", "细节与材质表现与源素材不一致"),
    ):
        score = float(pair[dimension])
        if score < _SCORE_THRESHOLD:
            findings.append(
                {
                    "finding_key": f"image-{dimension}",
                    "dimension": dimension,
                    "kind": "global_difference",
                    "severity": _severity(score),
                    "confidence": round(1.0 - score, 6),
                    "message": message,
                    "bbox": regions[0],
                    "time_range": None,
                    "shot_id": None,
                    "evidence": {},
                    "metrics": {"score": round(score, 6)},
                }
            )

    source_ocr = image_evidence_analysis.tesseract_ocr(source_image)
    generated_ocr = image_evidence_analysis.tesseract_ocr(generated_image)
    analyzers["ocr_source"] = _ocr_record(source_ocr)
    analyzers["ocr_generated"] = _ocr_record(generated_ocr)
    ocr_dimension, ocr_finding = _ocr_dimension(source_ocr, generated_ocr)
    dimensions["ocr_text"] = ocr_dimension
    if ocr_finding:
        findings.append(ocr_finding)
    subject_dimension, subject_analyzer, subject_finding = _image_subject_semantics(
        source_image, generated_image
    )
    dimensions["subject_semantics"] = subject_dimension
    analyzers["subject_semantics"] = subject_analyzer
    if subject_finding:
        findings.append(subject_finding)
    warnings = [
        row["reason"]
        for key, row in analyzers.items()
        if (key.startswith("ocr_") or key == "subject_semantics")
        and row.get("status") != "analyzed"
        and row.get("reason")
    ]
    return {
        "metrics": {
            "schema_version": "reproduction-image-metrics.v2",
            "dimensions": dimensions,
            "heatmap": _heatmap(pair["diff"]),
            "available_dimension_count": sum(
                item.get("status") == "analyzed" for item in dimensions.values()
            ),
        },
        "analyzers": analyzers,
        "findings": findings,
        "warnings": warnings,
    }


def _shot_rows(row: ReproductionAssessment) -> list[dict[str, Any]]:
    if row.reverse_revision_id is None:
        return []
    # The worker owns the row, but the revision is loaded later in its current DB session.
    return []


def _revision_shots(db: Session, row: ReproductionAssessment) -> list[dict[str, Any]]:
    if row.reverse_revision_id is None:
        return []
    revision = db.get(ReverseResultRevision, int(row.reverse_revision_id))
    payload = revision.payload if revision is not None and isinstance(revision.payload, dict) else {}
    analysis = payload.get("video_analysis") if isinstance(payload.get("video_analysis"), dict) else {}
    return [item for item in analysis.get("shots") or [] if isinstance(item, dict)]


def _shot_id(shots: list[dict[str, Any]], timestamp: float) -> str | None:
    for shot in shots:
        try:
            start = float(shot.get("start_seconds") or 0)
            end = float(shot.get("end_seconds"))
        except (TypeError, ValueError):
            continue
        if start <= timestamp <= end + 0.001:
            value = str(shot.get("shot_id") or shot.get("detected_shot_id") or "").strip()
            return value or None
    return None


def _frame_time_range(
    timestamps: list[float], index: int, duration: float
) -> dict[str, float]:
    current = timestamps[index]
    previous = timestamps[index - 1] if index else 0.0
    following = timestamps[index + 1] if index + 1 < len(timestamps) else duration
    start = max(0.0, (previous + current) / 2 if index else 0.0)
    end = min(duration, (current + following) / 2 if index + 1 < len(timestamps) else duration)
    if end <= start:
        end = min(duration, start + max(0.05, duration / 100))
    return {"start_seconds": round(start, 3), "end_seconds": round(end, 3)}


def _motion_score(source_pairs: list[dict[str, Any]], generated_pairs: list[dict[str, Any]]) -> float:
    if len(source_pairs) < 2 or len(generated_pairs) < 2:
        return 1.0
    source_changes = [
        abs(float(source_pairs[index]["luma"]) - float(source_pairs[index - 1]["luma"]))
        for index in range(1, len(source_pairs))
    ]
    generated_changes = [
        abs(float(generated_pairs[index]["luma"]) - float(generated_pairs[index - 1]["luma"]))
        for index in range(1, len(generated_pairs))
    ]
    count = min(len(source_changes), len(generated_changes))
    return float(
        np.clip(
            1.0 - np.mean(np.abs(np.asarray(source_changes[:count]) - generated_changes[:count])),
            0,
            1,
        )
    )


def _video_semantic_inputs(
    frames: list[Any],
) -> tuple[list[Image.Image], list[dict[str, Any]]]:
    images: list[Image.Image] = []
    metadata: list[dict[str, Any]] = []
    for index, frame in enumerate(frames, start=1):
        raw = bytes(frame.jpeg)
        image = _open_image(raw)
        images.append(image)
        metadata.append(
            {
                "frame_index": index,
                "timestamp_seconds": round(float(frame.timestamp_seconds), 3),
                "source_segment_index": 1,
                "source_content_hash": hashlib.sha256(raw).hexdigest(),
                "width": image.width,
                "height": image.height,
            }
        )
    return images, metadata


def _subject_track_rows(capability: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for track in capability.get("tracks") or []:
        if not isinstance(track, dict):
            continue
        observations = [
            row
            for row in track.get("observations") or []
            if isinstance(row, dict) and _region_bbox(row) is not None
        ]
        bboxes = [_region_bbox(row) for row in observations]
        bboxes = [bbox for bbox in bboxes if bbox is not None]
        normalized = dict(track)
        if bboxes:
            normalized["bbox"] = {
                key: float(np.mean([bbox[key] for bbox in bboxes]))
                for key in ("x", "y", "width", "height")
            }
        rows.append(normalized)
    return rows


def _video_semantic_capability_comparison(
    source: dict[str, Any],
    generated: dict[str, Any],
    *,
    capability: str,
    source_duration: float,
    generated_duration: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    source_capability = source.get(capability)
    generated_capability = generated.get(capability)
    if not isinstance(source_capability, dict) or not isinstance(generated_capability, dict):
        source_capability = source_capability if isinstance(source_capability, dict) else {}
        generated_capability = generated_capability if isinstance(generated_capability, dict) else {}
    source_status = str(source_capability.get("status") or "degraded")
    generated_status = str(generated_capability.get("status") or "degraded")
    evidence_key = {
        "subject_tracking": "tracks",
        "action": "events",
        "transition": "events",
    }[capability]
    comparable_statuses = {"analyzed", "partial"}
    if source_status in comparable_statuses and generated_status in comparable_statuses:
        source_rows = [
            row for row in source_capability.get(evidence_key) or [] if isinstance(row, dict)
        ]
        generated_rows = [
            row for row in generated_capability.get(evidence_key) or [] if isinstance(row, dict)
        ]
        if capability == "subject_tracking":
            source_rows = _subject_track_rows({"tracks": source_rows})
            generated_rows = _subject_track_rows({"tracks": generated_rows})
            comparison = _labeled_evidence_similarity(
                source_rows,
                generated_rows,
                source_duration=source_duration,
                generated_duration=generated_duration,
                spatial_weight=0.25,
                temporal_weight=0.15,
            )
        else:
            comparison = _labeled_evidence_similarity(
                source_rows,
                generated_rows,
                source_duration=source_duration,
                generated_duration=generated_duration,
                temporal_weight=0.25 if capability == "action" else 0.3,
            )
        comparison["source_rows"] = source_rows
        comparison["generated_rows"] = generated_rows
        dimension = {
            key: value
            for key, value in comparison.items()
            if key not in {"unmatched_source", "source_rows", "generated_rows"}
        }
        analyzer_status = "analyzed"
        reason = None
    else:
        analyzer_status, reason = _pair_failure_status(
            [source_capability], [generated_capability]
        )
        comparison = {
            "status": analyzer_status,
            "score": None,
            "reason": reason,
            "source_rows": [],
            "generated_rows": [],
        }
        dimension = {
            "status": analyzer_status,
            "score": None,
            "reason": reason,
        }
    analyzer = _analyzer_record(
        status=analyzer_status,
        version="video-semantic-compare-v1",
        reason=reason,
        capability=capability,
        source_status=source_status,
        generated_status=generated_status,
        source_analyzer=source_capability.get("analyzer"),
        generated_analyzer=generated_capability.get("analyzer"),
        source_version=source_capability.get("analyzer_version"),
        generated_version=generated_capability.get("analyzer_version"),
        source_evidence_count=int(source_capability.get("evidence_count") or 0),
        generated_evidence_count=int(generated_capability.get("evidence_count") or 0),
    )
    return dimension, {"analyzer": analyzer, "comparison": comparison}


def _semantic_video_finding(
    *,
    capability: str,
    comparison: dict[str, Any],
    source_duration: float,
    shots: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if comparison.get("status") != "analyzed":
        return None
    score = float(comparison.get("score") or 0)
    if score >= _SCORE_THRESHOLD:
        return None
    source_rows = [row for row in comparison.get("source_rows") or [] if isinstance(row, dict)]
    unmatched = next(
        (row for row in comparison.get("unmatched_source") or [] if isinstance(row, dict)),
        source_rows[0] if source_rows else {},
    )
    try:
        timestamp = float(unmatched.get("timestamp_seconds"))
        start = max(0.0, timestamp - max(0.05, source_duration * 0.01))
        end = min(source_duration, timestamp + max(0.05, source_duration * 0.01))
    except (TypeError, ValueError):
        try:
            start = float(unmatched.get("start_seconds") or 0)
            end = float(unmatched.get("end_seconds") or source_duration)
        except (TypeError, ValueError):
            start, end = 0.0, source_duration
    if end <= start:
        end = min(source_duration, start + max(0.05, source_duration * 0.01))
    center = (start + end) / 2
    dimension = {
        "subject_tracking": "subject_semantics",
        "action": "action_semantics",
        "transition": "transition_semantics",
    }[capability]
    message = {
        "subject_tracking": "视频主体类别、数量、位置或持续时间与源视频不一致",
        "action": "视频主体动作或动作时序与源视频不一致",
        "transition": "视频转场类型或转场时点与源视频不一致",
    }[capability]
    return {
        "finding_key": f"video-{dimension}",
        "dimension": dimension,
        "kind": f"{capability}_mismatch",
        "severity": _severity(score),
        "confidence": round(1.0 - score, 6),
        "message": message,
        "bbox": _region_bbox(unmatched) if capability == "subject_tracking" else None,
        "time_range": {
            "start_seconds": round(max(0.0, start), 3),
            "end_seconds": round(max(start + 0.001, end), 3),
        },
        "shot_id": _shot_id(shots, center),
        "evidence": {
            "source_labels": comparison.get("source_labels", []),
            "generated_labels": comparison.get("generated_labels", []),
        },
        "metrics": {"score": round(score, 6)},
    }


def _camera_motion_comparison(
    source: dict[str, Any],
    generated: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    source_status = str(source.get("status") or "degraded")
    generated_status = str(generated.get("status") or "degraded")
    source_samples = [row for row in source.get("samples") or [] if isinstance(row, dict)]
    generated_samples = [row for row in generated.get("samples") or [] if isinstance(row, dict)]
    if source_status == generated_status == "analyzed" and source_samples and generated_samples:
        count = min(len(source_samples), len(generated_samples))
        rows: list[dict[str, Any]] = []
        for index in range(count):
            source_scores = source_samples[index].get("camera_scores") or {}
            generated_scores = generated_samples[index].get("camera_scores") or {}
            score = 1.0 - float(
                np.mean(
                    [
                        abs(float(source_scores.get(key) or 0) - float(generated_scores.get(key) or 0))
                        for key in ("pan", "tilt", "zoom", "static")
                    ]
                )
            )
            rows.append(
                {
                    "score": float(np.clip(score, 0, 1)),
                    "source": source_samples[index],
                    "generated": generated_samples[index],
                }
            )
        score = round(float(np.mean([row["score"] for row in rows])), 6)
        dimension = {
            "status": "analyzed",
            "score": score,
            "sample_count": count,
            "source_labels": [str(row.get("camera") or "") for row in source_samples[:count]],
            "generated_labels": [str(row.get("camera") or "") for row in generated_samples[:count]],
        }
        analyzer_status, reason = "analyzed", None
        worst = min(rows, key=lambda row: row["score"])
    else:
        analyzer_status, reason = _pair_failure_status([source], [generated])
        dimension = {"status": analyzer_status, "score": None, "reason": reason}
        score = None
        worst = None
    analyzer = _analyzer_record(
        status=analyzer_status,
        version="opencv-camera-motion-compare-v1",
        reason=reason,
        source_status=source_status,
        generated_status=generated_status,
        source_version=source.get("analyzer_version"),
        generated_version=generated.get("analyzer_version"),
        source_sample_count=len(source_samples),
        generated_sample_count=len(generated_samples),
    )
    finding = None
    if score is not None and score < _SCORE_THRESHOLD and worst is not None:
        source_row = worst["source"]
        start = float(source_row.get("start_seconds") or 0)
        end = float(source_row.get("end_seconds") or start + 0.05)
        finding = {
            "finding_key": "video-camera-motion",
            "dimension": "camera_motion",
            "kind": "camera_motion_mismatch",
            "severity": _severity(score),
            "confidence": round(1.0 - score, 6),
            "message": "运镜类型或运动强度与源视频不一致",
            "bbox": None,
            "time_range": {
                "start_seconds": round(start, 3),
                "end_seconds": round(max(start + 0.001, end), 3),
            },
            "shot_id": None,
            "evidence": {
                "source_camera": source_row.get("camera"),
                "generated_camera": worst["generated"].get("camera"),
            },
            "metrics": {"score": score},
        }
    return dimension, analyzer, finding


def _audio_comparison(source: dict[str, Any], generated: dict[str, Any]) -> dict[str, Any]:
    source_status = str(source.get("status") or "degraded")
    generated_status = str(generated.get("status") or "degraded")
    if source_status == generated_status == "no_audio":
        return {"status": "not_applicable", "score": None, "reason": "两侧都没有音轨"}
    if source_status not in {"analyzed", "partial"} or generated_status not in {
        "analyzed",
        "partial",
    }:
        return {
            "status": "unsupported" if "disabled" in {source_status, generated_status} else "degraded",
            "score": None,
            "reason": "音频分析证据不完整",
        }
    source_text = str(source.get("transcript") or "")
    generated_text = str(generated.get("transcript") or "")
    transcript_score = SequenceMatcher(
        None, source_text.casefold(), generated_text.casefold()
    ).ratio()
    source_beat = (source.get("features") or {}).get("beat") or {}
    generated_beat = (generated.get("features") or {}).get("beat") or {}
    source_bpm = source_beat.get("bpm")
    generated_bpm = generated_beat.get("bpm")
    bpm_score = None
    try:
        source_bpm = float(source_bpm)
        generated_bpm = float(generated_bpm)
        bpm_score = min(source_bpm, generated_bpm) / max(source_bpm, generated_bpm)
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    scores = [transcript_score, *([bpm_score] if bpm_score is not None else [])]
    return {
        "status": "analyzed",
        "score": round(float(np.clip(np.mean(scores), 0, 1)), 6),
        "transcript_score": round(float(np.clip(transcript_score, 0, 1)), 6),
        "bpm_score": round(float(np.clip(bpm_score, 0, 1)), 6) if bpm_score is not None else None,
    }


def _video_analysis(
    db: Session,
    row: ReproductionAssessment,
    source_path: Path,
    generated_path: Path,
) -> dict[str, Any]:
    source_meta = video_frames.probe_media(str(source_path)) or {}
    generated_meta = video_frames.probe_media(str(generated_path)) or {}
    source_duration = float(source_meta.get("duration_seconds") or source_meta.get("duration") or 0)
    generated_duration = float(
        generated_meta.get("duration_seconds") or generated_meta.get("duration") or 0
    )
    fractions = np.linspace(0.02, 0.98, _VIDEO_SAMPLE_COUNT).tolist()
    source_timestamps = [source_duration * value for value in fractions] if source_duration else []
    generated_timestamps = (
        [generated_duration * value for value in fractions] if generated_duration else []
    )
    source_sample = (
        video_frames.sample_video_from_path(
            str(source_path),
            _VIDEO_SAMPLE_COUNT,
            custom_timestamps=source_timestamps,
        )
        if source_timestamps
        else None
    )
    generated_sample = (
        video_frames.sample_video_from_path(
            str(generated_path),
            _VIDEO_SAMPLE_COUNT,
            custom_timestamps=generated_timestamps,
        )
        if generated_timestamps
        else None
    )
    source_frames = list(source_sample.frames) if source_sample else []
    generated_frames = list(generated_sample.frames) if generated_sample else []
    count = min(len(source_frames), len(generated_frames))
    shots = _revision_shots(db, row)
    findings: list[dict[str, Any]] = []
    frame_pairs: list[dict[str, Any]] = []
    source_motion: list[dict[str, Any]] = []
    generated_motion: list[dict[str, Any]] = []
    dimension_scores: dict[str, list[float]] = {
        "frame_structure": [],
        "frame_color_light": [],
        "frame_detail_material": [],
    }
    actual_source_timestamps: list[float] = []
    for index in range(count):
        source_frame = source_frames[index]
        generated_frame = generated_frames[index]
        source_image = _open_image(source_frame.jpeg)
        generated_image = _open_image(generated_frame.jpeg)
        pair = _image_pair(source_image, generated_image)
        source_timestamp = float(source_frame.timestamp_seconds)
        generated_timestamp = float(generated_frame.timestamp_seconds)
        actual_source_timestamps.append(source_timestamp)
        shot_id = _shot_id(shots, source_timestamp)
        frame_pair = {
            "index": index + 1,
            "normalized_position": round(float(fractions[index]), 6),
            "source_timestamp_seconds": round(source_timestamp, 3),
            "generated_timestamp_seconds": round(generated_timestamp, 3),
            "shot_id": shot_id,
            "scores": {
                "structure": pair["structure_layout"],
                "color_light": pair["color_light"],
                "detail_material": pair["detail_material"],
            },
            "bbox": _difference_regions(pair["diff"])[0],
        }
        frame_pairs.append(frame_pair)
        dimension_scores["frame_structure"].append(float(pair["structure_layout"]))
        dimension_scores["frame_color_light"].append(float(pair["color_light"]))
        dimension_scores["frame_detail_material"].append(float(pair["detail_material"]))
        source_motion.append(
            {"luma": float(np.asarray(source_image.convert("L"), dtype=np.float32).mean() / 255)}
        )
        generated_motion.append(
            {
                "luma": float(
                    np.asarray(generated_image.convert("L"), dtype=np.float32).mean() / 255
                )
            }
        )

    for index, frame_pair in enumerate(frame_pairs):
        score = float(frame_pair["scores"]["structure"])
        if score >= _SCORE_THRESHOLD:
            continue
        time_range = _frame_time_range(actual_source_timestamps, index, source_duration)
        findings.append(
            {
                "finding_key": f"video-frame-{index + 1}-structure",
                "dimension": "frame_structure",
                "kind": "frame_difference",
                "severity": _severity(score),
                "confidence": round(1.0 - score, 6),
                "message": f"源视频 {frame_pair['source_timestamp_seconds']:g}s 附近画面结构不一致",
                "bbox": frame_pair["bbox"],
                "time_range": time_range,
                "shot_id": frame_pair["shot_id"],
                "evidence": {
                    "source_timestamp_seconds": frame_pair["source_timestamp_seconds"],
                    "generated_timestamp_seconds": frame_pair["generated_timestamp_seconds"],
                    "normalized_position": frame_pair["normalized_position"],
                },
                "metrics": {"score": round(score, 6)},
            }
        )

    dimensions: dict[str, dict[str, Any]] = {}
    for key, scores in dimension_scores.items():
        if scores:
            dimensions[key] = {
                "status": "analyzed",
                "score": round(float(np.clip(np.mean(scores), 0, 1)), 6),
                "sample_count": len(scores),
            }
        else:
            dimensions[key] = {
                "status": "unsupported",
                "score": None,
                "reason": "无法获取可对齐的视频帧",
            }
    if source_duration > 0 and generated_duration > 0:
        duration_score = min(source_duration, generated_duration) / max(
            source_duration, generated_duration
        )
        dimensions["duration_pacing"] = {
            "status": "analyzed",
            "score": round(float(np.clip(duration_score, 0, 1)), 6),
            "source_duration_seconds": round(source_duration, 3),
            "generated_duration_seconds": round(generated_duration, 3),
        }
        if duration_score < 0.9:
            findings.append(
                {
                    "finding_key": "video-duration-pacing",
                    "dimension": "duration_pacing",
                    "kind": "duration_difference",
                    "severity": _severity(duration_score),
                    "confidence": round(1.0 - duration_score, 6),
                    "message": "生成视频时长与源视频不一致",
                    "bbox": None,
                    "time_range": {
                        "start_seconds": 0.0,
                        "end_seconds": round(source_duration, 3),
                    },
                    "shot_id": None,
                    "evidence": {
                        "source_duration_seconds": round(source_duration, 3),
                        "generated_duration_seconds": round(generated_duration, 3),
                    },
                    "metrics": {"score": round(duration_score, 6)},
                }
            )
    else:
        dimensions["duration_pacing"] = {
            "status": "unsupported",
            "score": None,
            "reason": "无法读取两侧视频时长",
        }
    motion_score = _motion_score(source_motion, generated_motion)
    dimensions["motion"] = {
        "status": "analyzed" if count >= 2 else "unsupported",
        "score": round(motion_score, 6) if count >= 2 else None,
        "sample_count": count,
        **({"reason": "运动帧数不足"} if count < 2 else {}),
    }

    source_semantic_images, source_semantic_frames = _video_semantic_inputs(source_frames)
    generated_semantic_images, generated_semantic_frames = _video_semantic_inputs(generated_frames)
    source_semantics = video_evidence_analysis.http_semantic_provider(
        source_semantic_images, source_semantic_frames
    )
    generated_semantics = video_evidence_analysis.http_semantic_provider(
        generated_semantic_images, generated_semantic_frames
    )
    semantic_analyzers: dict[str, dict[str, Any]] = {}
    for capability, dimension_key in (
        ("subject_tracking", "subject_semantics"),
        ("action", "action_semantics"),
        ("transition", "transition_semantics"),
    ):
        dimension, detail = _video_semantic_capability_comparison(
            source_semantics,
            generated_semantics,
            capability=capability,
            source_duration=source_duration,
            generated_duration=generated_duration,
        )
        dimensions[dimension_key] = dimension
        semantic_analyzers[dimension_key] = detail["analyzer"]
        finding = _semantic_video_finding(
            capability=capability,
            comparison=detail["comparison"],
            source_duration=source_duration,
            shots=shots,
        )
        if finding:
            findings.append(finding)

    source_camera = video_evidence_analysis.cv2_motion_analysis(
        source_semantic_images, source_semantic_frames
    )
    generated_camera = video_evidence_analysis.cv2_motion_analysis(
        generated_semantic_images, generated_semantic_frames
    )
    camera_dimension, camera_analyzer, camera_finding = _camera_motion_comparison(
        source_camera, generated_camera
    )
    dimensions["camera_motion"] = camera_dimension
    if camera_finding:
        camera_finding["shot_id"] = _shot_id(
            shots,
            (
                float(camera_finding["time_range"]["start_seconds"])
                + float(camera_finding["time_range"]["end_seconds"])
            )
            / 2,
        )
        findings.append(camera_finding)

    ocr_source_results = []
    ocr_generated_results = []
    for index in sorted({0, max(0, count // 2), max(0, count - 1)}):
        if index >= count:
            continue
        ocr_source_results.append(
            image_evidence_analysis.tesseract_ocr(_open_image(source_frames[index].jpeg))
        )
        ocr_generated_results.append(
            image_evidence_analysis.tesseract_ocr(_open_image(generated_frames[index].jpeg))
        )
    if ocr_source_results and ocr_generated_results:
        source_ocr = {
            "status": (
                "analyzed"
                if all(item.get("status") == "analyzed" for item in ocr_source_results)
                else str(ocr_source_results[0].get("status") or "degraded")
            ),
            "evidence": [
                evidence
                for result in ocr_source_results
                for evidence in result.get("evidence") or []
            ],
        }
        generated_ocr = {
            "status": (
                "analyzed"
                if all(item.get("status") == "analyzed" for item in ocr_generated_results)
                else str(ocr_generated_results[0].get("status") or "degraded")
            ),
            "evidence": [
                evidence
                for result in ocr_generated_results
                for evidence in result.get("evidence") or []
            ],
        }
        ocr_dimension, _ocr_finding = _ocr_dimension(source_ocr, generated_ocr)
        dimensions["ocr_text"] = ocr_dimension
        ocr_status = (
            "analyzed"
            if source_ocr["status"] == generated_ocr["status"] == "analyzed"
            else "unsupported"
        )
    else:
        dimensions["ocr_text"] = {
            "status": "unsupported",
            "score": None,
            "reason": "无视频帧可用于 OCR",
        }
        ocr_status = "unsupported"

    source_audio = video_audio.analyze_video_audio_from_path(str(source_path))
    generated_audio = video_audio.analyze_video_audio_from_path(str(generated_path))
    audio_dimension = _audio_comparison(source_audio, generated_audio)
    dimensions["audio"] = audio_dimension
    source_audio_status = str(source_audio.get("status") or "degraded")
    generated_audio_status = str(generated_audio.get("status") or "degraded")
    audio_status = (
        source_audio_status
        if source_audio_status == generated_audio_status
        else "partial"
    )
    analyzers = {
        "ffprobe": _analyzer_record(
            status="analyzed" if source_duration and generated_duration else "degraded",
            version="ffprobe",
            reason=None if source_duration and generated_duration else "媒体时长不完整",
        ),
        "ffmpeg_frames": _analyzer_record(
            status="analyzed" if count else "degraded",
            version="normalized-timestamp-v1",
            reason=None if count else "抽帧不可用",
            sample_count=count,
        ),
        "opencv": _analyzer_record(status="analyzed", version=cv2.__version__),
        "motion": _analyzer_record(
            status="analyzed" if count >= 2 else "degraded",
            version="luma-delta-v1",
            reason=None if count >= 2 else "运动帧数不足",
        ),
        "ocr": _analyzer_record(status=ocr_status, version="tesseract-sampled-frames-v1"),
        "audio": _analyzer_record(
            status=audio_status,
            version="video-audio-evidence-v1",
            source_status=source_audio_status,
            generated_status=generated_audio_status,
        ),
        "camera_motion": camera_analyzer,
        **semantic_analyzers,
    }
    warnings = [
        item["reason"]
        for item in analyzers.values()
        if item.get("status") in {"unsupported", "degraded", "partial"} and item.get("reason")
    ]
    return {
        "metrics": {
            "schema_version": "reproduction-video-metrics.v2",
            "dimensions": dimensions,
            "frame_pairs": frame_pairs,
            "available_dimension_count": sum(
                item.get("status") == "analyzed" for item in dimensions.values()
            ),
        },
        "analyzers": analyzers,
        "findings": findings,
        "warnings": warnings,
    }


def _is_partial(row: ReproductionAssessment, result: dict[str, Any]) -> bool:
    snapshot = row.asset_snapshot if isinstance(row.asset_snapshot, dict) else {}
    if snapshot.get("quality_status") == "degraded":
        return True
    dimensions = (result.get("metrics") or {}).get("dimensions") or {}
    if any(
        item.get("status") not in {"analyzed", "not_applicable"}
        for item in dimensions.values()
        if isinstance(item, dict)
    ):
        return True
    return any(
        item.get("status") in {"unsupported", "degraded", "partial", "failed", "disabled"}
        for item in (result.get("analyzers") or {}).values()
        if isinstance(item, dict)
    )


def _persist_findings(
    db: Session,
    *,
    assessment_id: int,
    findings: list[dict[str, Any]],
) -> None:
    db.execute(
        delete(ReproductionFinding).where(
            ReproductionFinding.assessment_id == int(assessment_id)
        )
    )
    for position, finding in enumerate(findings, start=1):
        db.add(
            ReproductionFinding(
                assessment_id=int(assessment_id),
                finding_key=str(finding["finding_key"]),
                position=position,
                dimension=str(finding["dimension"]),
                kind=str(finding["kind"]),
                severity=str(finding["severity"]),
                confidence=(
                    float(finding["confidence"])
                    if finding.get("confidence") is not None
                    else None
                ),
                message=str(finding["message"]),
                bbox=deepcopy(finding.get("bbox")),
                time_range=deepcopy(finding.get("time_range")),
                shot_id=str(finding.get("shot_id") or "") or None,
                evidence=deepcopy(finding.get("evidence") or {}),
                metrics=deepcopy(finding.get("metrics") or {}),
            )
        )


def _mark_failed(assessment_id: int, exc: Exception) -> None:
    from ..db import SessionLocal

    with SessionLocal() as db:
        row = db.get(ReproductionAssessment, int(assessment_id))
        if row is None or row.status in TERMINAL_STATUSES:
            return
        if row.cancel_requested:
            row.status = "canceled"
            row.phase = "canceled"
        else:
            row.status = "failed"
            row.phase = "failed"
            row.error_code = (
                exc.code if isinstance(exc, ReproductionAssessmentError) else "ASSESSMENT_FAILED"
            )
            row.error = str(exc)[:2000]
        row.finished_at = utcnow()
        db.commit()


def run_assessment(assessment_id: int) -> None:
    from ..db import SessionLocal

    try:
        with SessionLocal() as db:
            row = db.get(ReproductionAssessment, int(assessment_id))
            if row is None or row.status in TERMINAL_STATUSES:
                return
            if row.cancel_requested:
                row.status = "canceled"
                row.phase = "canceled"
                row.finished_at = utcnow()
                db.commit()
                return
            row.status = "running"
            row.phase = "materializing"
            row.progress = 5
            row.started_at = row.started_at or utcnow()
            db.commit()

            source_path = _analysis_path(row, "source")
            generated_path = _analysis_path(row, "generated")
            row.phase = "analyzing"
            row.progress = 20
            db.commit()
            if row.media_type == "image":
                result = _image_analysis(source_path, generated_path)
            else:
                result = _video_analysis(db, row, source_path, generated_path)

            db.expire(row)
            db.refresh(row)
            if row.cancel_requested:
                row.status = "canceled"
                row.phase = "canceled"
                row.finished_at = utcnow()
                db.commit()
                return
            row.phase = "persisting"
            row.progress = 90
            _persist_findings(
                db,
                assessment_id=int(row.id),
                findings=list(result.get("findings") or []),
            )
            row.metrics = deepcopy(result.get("metrics") or {})
            row.analyzers = deepcopy(result.get("analyzers") or {})
            row.warnings = list(
                dict.fromkeys([*(row.warnings or []), *(result.get("warnings") or [])])
            )
            row.status = "partial" if _is_partial(row, result) else "succeeded"
            row.phase = "completed"
            row.progress = 100
            row.error_code = None
            row.error = None
            row.finished_at = utcnow()
            db.commit()
    except Exception as exc:  # noqa: BLE001 - worker persists a stable failure contract
        log.exception("reproduction assessment %s failed", assessment_id)
        _mark_failed(int(assessment_id), exc)


def _deep_merge(target: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(target)
    for key, value in patch.items():
        if value is None:
            merged.pop(key, None)
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _revision_summary(row: ReverseResultRevision) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "operation_id": int(row.operation_id),
        "version": int(row.version),
        "source": row.source,
        "payload": deepcopy(row.payload or {}),
        "parent_revision_id": (
            int(row.parent_revision_id) if row.parent_revision_id is not None else None
        ),
        "payload_hash": row.payload_hash,
        "lineage_status": row.lineage_status,
        "created_at": row.created_at,
    }


def serialize_correction(db: Session, row: ReproductionCorrection) -> dict[str, Any]:
    edited = db.get(ReverseResultRevision, int(row.edited_revision_id))
    applied = (
        db.get(ReverseResultRevision, int(row.applied_revision_id))
        if row.applied_revision_id is not None
        else None
    )
    if edited is None:
        raise ReproductionAssessmentConflict("纠偏版本血缘已损坏")
    return {
        "id": int(row.id),
        "assessment_id": int(row.assessment_id),
        "status": row.status,
        "idempotency_key": row.idempotency_key,
        "parent_revision_id": int(row.parent_revision_id),
        "selected_finding_ids": [int(item) for item in row.selected_finding_ids or []],
        "structured_patch": deepcopy(row.structured_patch or {}),
        "prompt_patch": row.prompt_patch,
        "negative_prompt_patch": row.negative_prompt_patch,
        "mask_patch": deepcopy(row.mask_patch),
        "apply_requested": bool(row.apply_requested),
        "edited_revision": _revision_summary(edited),
        "applied_revision": _revision_summary(applied) if applied is not None else None,
        "created_at": row.created_at,
    }


def _derived_finding_patch(findings: list[ReproductionFinding]) -> dict[str, Any]:
    return {
        "reproduction_corrections": [
            {
                "finding_id": int(finding.id),
                "dimension": finding.dimension,
                "instruction": finding.message,
                "bbox": deepcopy(finding.bbox),
                "time_range": deepcopy(finding.time_range),
                "shot_id": finding.shot_id,
            }
            for finding in findings
        ]
    }


def _has_revision_ancestor(
    db: Session,
    *,
    revision: ReverseResultRevision,
    ancestor_id: int,
) -> bool:
    """Check correction lineage without accepting an unrelated operation revision."""
    current: ReverseResultRevision | None = revision
    seen: set[int] = set()
    while current is not None:
        current_id = int(current.id)
        if current_id in seen:
            return False
        if current_id == int(ancestor_id):
            return True
        seen.add(current_id)
        if current.parent_revision_id is None:
            return False
        current = db.get(ReverseResultRevision, int(current.parent_revision_id))
    return False


def _latest_parent_for_correction(
    db: Session,
    *,
    operation_id: int,
    user_id: int,
    source: str,
) -> ReverseResultRevision | None:
    allowed_sources = ("applied",) if source == "applied" else ("normalized", "user_edit")
    return db.scalar(
        select(ReverseResultRevision)
        .where(
            ReverseResultRevision.operation_id == int(operation_id),
            ReverseResultRevision.user_id == int(user_id),
            ReverseResultRevision.source.in_(allowed_sources),
        )
        .order_by(ReverseResultRevision.version.desc())
        .limit(1)
    )


def create_correction(
    db: Session,
    *,
    assessment_id: int,
    user_id: int,
    body: ReproductionCorrectionCreateIn,
    commit: bool = True,
) -> tuple[dict[str, Any], bool]:
    assessment = _owned_assessment(db, assessment_id=assessment_id, user_id=user_id)
    if assessment.status not in CORRECTABLE_STATUSES:
        raise ReproductionAssessmentConflict("只能纠偏已完成或部分完成的评估")
    if assessment.reverse_operation_id is None:
        raise ReproductionAssessmentConflict("评估没有可纠偏的反推血缘")
    fingerprint = _request_fingerprint(body)
    replay = db.scalar(
        select(ReproductionCorrection).where(
            ReproductionCorrection.assessment_id == int(assessment_id),
            ReproductionCorrection.idempotency_key == body.idempotency_key,
        )
    )
    if replay is not None:
        if replay.request_fingerprint != fingerprint:
            raise ReproductionAssessmentConflict("幂等 key 已用于不同纠偏请求")
        return serialize_correction(db, replay), False

    findings = list(
        db.scalars(
            select(ReproductionFinding).where(
                ReproductionFinding.assessment_id == int(assessment_id),
                ReproductionFinding.id.in_(body.selected_finding_ids),
            )
        )
    )
    if {int(row.id) for row in findings} != set(body.selected_finding_ids):
        raise ReproductionAssessmentError("所选 finding 不属于当前评估")
    findings.sort(key=lambda item: body.selected_finding_ids.index(int(item.id)))
    parent = db.get(ReverseResultRevision, int(body.parent_revision_id))
    if (
        parent is None
        or int(parent.user_id) != int(user_id)
        or int(parent.operation_id) != int(assessment.reverse_operation_id)
        or parent.source not in {"normalized", "user_edit", "applied"}
    ):
        raise ReproductionAssessmentConflict("纠偏父版本不可用")
    if assessment.reverse_revision_id is not None and not _has_revision_ancestor(
        db,
        revision=parent,
        ancestor_id=int(assessment.reverse_revision_id),
    ):
        raise ReproductionAssessmentConflict("纠偏父版本不属于当前评估血缘")
    latest = _latest_parent_for_correction(
        db,
        operation_id=int(assessment.reverse_operation_id),
        user_id=int(user_id),
        source=str(parent.source),
    )
    if latest is None or int(latest.id) != int(parent.id):
        raise ReproductionAssessmentConflict("反推结果已更新，请刷新后重试")

    effective_structured_patch = deepcopy(body.structured_patch)
    if not any(
        (body.structured_patch, body.prompt_patch, body.negative_prompt_patch, body.mask_patch)
    ):
        effective_structured_patch = _derived_finding_patch(findings)
    payload = deepcopy(parent.payload if isinstance(parent.payload, dict) else {})
    current_structured = payload.get("structured")
    if not isinstance(current_structured, dict):
        current_structured = {}
    payload["structured"] = _deep_merge(current_structured, effective_structured_patch)
    if body.prompt_patch is not None:
        payload["final_text"] = body.prompt_patch
    if body.negative_prompt_patch is not None:
        payload["negative_prompt"] = body.negative_prompt_patch
    if body.mask_patch is not None:
        payload["mask_patch"] = deepcopy(body.mask_patch)
    payload["reproduction_correction"] = {
        "assessment_id": int(assessment_id),
        "selected_finding_ids": [int(item) for item in body.selected_finding_ids],
        "apply_requested": bool(body.apply),
    }

    try:
        if body.apply and parent.source != "applied":
            edited, applied = reverse_operations.apply_result_revision(
                db,
                operation_id=int(assessment.reverse_operation_id),
                user_id=int(user_id),
                payload=payload,
                parent_revision_id=int(parent.id),
                commit=False,
            )
        else:
            edited = reverse_operations.create_result_revision(
                db,
                operation_id=int(assessment.reverse_operation_id),
                user_id=int(user_id),
                source="user_edit",
                payload=payload,
                parent_revision_id=int(parent.id),
                allow_applied_correction_parent=parent.source == "applied",
                commit=False,
            )
            if body.apply:
                applied = reverse_operations.create_result_revision(
                    db,
                    operation_id=int(assessment.reverse_operation_id),
                    user_id=int(user_id),
                    source="applied",
                    payload={},
                    parent_revision_id=int(edited.id),
                    commit=False,
                )
            else:
                applied = None
        correction = ReproductionCorrection(
            assessment_id=int(assessment_id),
            user_id=int(user_id),
            idempotency_key=body.idempotency_key,
            request_fingerprint=fingerprint,
            parent_revision_id=int(parent.id),
            edited_revision_id=int(edited.id),
            applied_revision_id=int(applied.id) if applied is not None else None,
            selected_finding_ids=[int(item) for item in body.selected_finding_ids],
            structured_patch=effective_structured_patch,
            prompt_patch=body.prompt_patch,
            negative_prompt_patch=body.negative_prompt_patch,
            mask_patch=deepcopy(body.mask_patch),
            apply_requested=bool(body.apply),
            status="applied" if applied is not None else "created",
        )
        db.add(correction)
        db.flush()
        if commit:
            db.commit()
            db.refresh(correction)
    except (
        reverse_operations.ReverseOperationNotFound,
        reverse_operations.ReverseOperationConflict,
        reverse_operations.ReverseOperationInvalid,
    ) as exc:
        db.rollback()
        status_code = 409 if isinstance(exc, reverse_operations.ReverseOperationConflict) else 422
        error_class = (
            ReproductionAssessmentConflict if status_code == 409 else ReproductionAssessmentError
        )
        raise error_class(str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        replay = db.scalar(
            select(ReproductionCorrection).where(
                ReproductionCorrection.assessment_id == int(assessment_id),
                ReproductionCorrection.idempotency_key == body.idempotency_key,
            )
        )
        if replay is not None and replay.request_fingerprint == fingerprint:
            return serialize_correction(db, replay), False
        raise ReproductionAssessmentConflict("纠偏版本已并发更新") from exc
    return serialize_correction(db, correction), True
