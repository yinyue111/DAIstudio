"""Explainable source-versus-generation reproduction assessment service."""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    ReproductionFinding,
    ReverseOperation,
    ReverseResultRevision,
    UploadedAsset,
)
from ..reproduction_schemas import (
    ReproductionAssessmentCreateIn,
)
from . import reproduction_correction as _reproduction_correction
from . import (
    reverse_operations,  # noqa: F401 - preserved re-export and monkeypatch surface
    storage,
)
from .compat_facade import install_assignment_forwarding
from .reproduction_comparators import (
    _IMAGE_MAX_EDGE,  # noqa: F401 - preserved re-export
    _SCORE_THRESHOLD,  # noqa: F401 - preserved re-export
    ReproductionAssessmentError,
    _analyzer_record,  # noqa: F401 - preserved re-export
    _bbox_iou,  # noqa: F401 - preserved re-export
    _dhash_similarity,  # noqa: F401 - preserved re-export
    _difference_regions,  # noqa: F401 - preserved re-export
    _heatmap,  # noqa: F401 - preserved re-export
    _image_pair,  # noqa: F401 - preserved re-export
    _image_subject_semantics,  # noqa: F401 - preserved re-export
    _labeled_evidence_similarity,  # noqa: F401 - preserved re-export
    _normalized_event_center,  # noqa: F401 - preserved re-export
    _ocr_dimension,  # noqa: F401 - preserved re-export
    _ocr_record,  # noqa: F401 - preserved re-export
    _open_image,  # noqa: F401 - preserved re-export
    _pair_failure_status,  # noqa: F401 - preserved re-export
    _region_bbox,  # noqa: F401 - preserved re-export
    _severity,  # noqa: F401 - preserved re-export
    _visible_text,  # noqa: F401 - preserved re-export
)
from .reproduction_contracts import (
    CORRECTABLE_STATUSES,  # noqa: F401 - preserved re-export
    TERMINAL_STATUSES,
    ReproductionAssessmentConflict,
    ReproductionAssessmentNotFound,
)
from .reproduction_contracts import (
    canonical_hash as _canonical_hash,  # noqa: F401 - preserved re-export
)
from .reproduction_contracts import (
    request_fingerprint as _request_fingerprint,
)
from .reproduction_correction import (
    _deep_merge,  # noqa: F401 - preserved re-export
    _derived_finding_patch,  # noqa: F401 - preserved re-export
    _has_revision_ancestor,  # noqa: F401 - preserved re-export
    _latest_parent_for_correction,  # noqa: F401 - preserved re-export
    _revision_summary,  # noqa: F401 - preserved re-export
    create_correction,  # noqa: F401 - preserved re-export
    serialize_correction,  # noqa: F401 - preserved re-export
)
from .reproduction_image_analysis import _image_analysis
from .reproduction_video_analysis import (
    _VIDEO_SAMPLE_COUNT,  # noqa: F401 - preserved re-export
    _audio_comparison,  # noqa: F401 - preserved re-export
    _camera_motion_comparison,  # noqa: F401 - preserved re-export
    _frame_time_range,  # noqa: F401 - preserved re-export
    _motion_score,  # noqa: F401 - preserved re-export
    _revision_shots,  # noqa: F401 - preserved re-export
    _semantic_video_finding,  # noqa: F401 - preserved re-export
    _shot_id,  # noqa: F401 - preserved re-export
    _subject_track_rows,  # noqa: F401 - preserved re-export
    _video_analysis,
    _video_semantic_capability_comparison,  # noqa: F401 - preserved re-export
    _video_semantic_inputs,  # noqa: F401 - preserved re-export
)
from .user_assets import (
    AssetNotFound,
    InvalidAssetRef,
    ResolvedAsset,
    resolve_asset_ref,
)

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
        task is None or int(task.user_id) != int(user_id) or str(task.category) != media_type
    ):
        raise ReproductionAssessmentNotFound("生成任务不存在")
    if task is not None and task.status not in {"succeeded", "needs_review"}:
        raise ReproductionAssessmentError("只能评估已产生结果的生成任务")

    task_operation_id = (
        int(task.reverse_operation_id) if task and task.reverse_operation_id else None
    )
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
    generated_resolved = _safe_resolve(db, user_id=user_id, asset_ref=body.generated_asset_ref)
    if generated_resolved.origin != "generated" or not isinstance(generated_resolved.row, GenAsset):
        raise ReproductionAssessmentError("生成结果必须是 GenAsset 生成资产")
    if body.source_asset_ref == body.generated_asset_ref:
        raise ReproductionAssessmentError("源素材和生成结果不能是同一资产")
    source_type = _media_type(source)
    generated_type = _media_type(generated_resolved)
    if source_type != generated_type:
        raise ReproductionAssessmentError("源素材与生成结果的媒体类型必须一致")

    source_snapshot, source_warnings = _asset_descriptor(source, side="source")
    generated_snapshot, generated_warnings = _asset_descriptor(generated_resolved, side="generated")
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
        delete(ReproductionFinding).where(ReproductionFinding.assessment_id == int(assessment_id))
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
                    float(finding["confidence"]) if finding.get("confidence") is not None else None
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


install_assignment_forwarding(__name__, (_reproduction_correction,))
