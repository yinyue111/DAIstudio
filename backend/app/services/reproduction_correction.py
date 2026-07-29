"""Revision-safe correction workflow for reproduction assessments."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import (
    ReproductionAssessment,
    ReproductionCorrection,
    ReproductionFinding,
    ReverseResultRevision,
)
from ..reproduction_schemas import ReproductionCorrectionCreateIn
from . import reverse_operations
from .reproduction_contracts import (
    CORRECTABLE_STATUSES,
    ReproductionAssessmentConflict,
    ReproductionAssessmentError,
    ReproductionAssessmentNotFound,
    request_fingerprint,
)


def _owned_assessment(
    db: Session,
    *,
    assessment_id: int,
    user_id: int,
) -> ReproductionAssessment:
    row = db.scalar(
        select(ReproductionAssessment).where(
            ReproductionAssessment.id == int(assessment_id),
            ReproductionAssessment.user_id == int(user_id),
        )
    )
    if row is None:
        raise ReproductionAssessmentNotFound("复刻度评估不存在")
    return row


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
    fingerprint = request_fingerprint(body)
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
        error_class = (
            ReproductionAssessmentConflict
            if isinstance(exc, reverse_operations.ReverseOperationConflict)
            else ReproductionAssessmentError
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
