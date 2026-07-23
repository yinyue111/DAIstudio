"""Source-versus-generation reproduction assessment endpoints."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..reproduction_schemas import (
    AssessmentStatus,
    ReproductionAssessmentCreateIn,
    ReproductionAssessmentOut,
    ReproductionAssessmentPageOut,
    ReproductionCorrectionCreateIn,
    ReproductionCorrectionOut,
    ReproductionRemediationCreateIn,
    ReproductionRemediationOut,
    ReproductionRemediationPageOut,
)
from ..services import reproduction_assessment, reproduction_remediation

router = APIRouter(prefix="/api/reproduction-assessments", tags=["studio"])


def _raise_http(exc: reproduction_assessment.ReproductionAssessmentError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": str(exc)},
    ) from exc


def _raise_remediation_http(exc: reproduction_remediation.ReproductionRemediationError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": str(exc)},
    ) from exc


@router.get("", response_model=ReproductionAssessmentPageOut)
def list_reproduction_assessments(
    assessment_status: AssessmentStatus | None = Query(default=None, alias="status"),
    media_type: Literal["image", "video"] | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return reproduction_assessment.list_assessments(
        db,
        user_id=int(user.id),
        status=assessment_status,
        media_type=media_type,
        limit=limit,
        offset=offset,
    )


@router.post("", response_model=ReproductionAssessmentOut, status_code=status.HTTP_201_CREATED)
def create_reproduction_assessment(
    body: ReproductionAssessmentCreateIn,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        assessment, created = reproduction_assessment.create_assessment(
            db,
            user_id=int(user.id),
            body=body,
        )
        if created:
            try:
                reproduction_assessment.enqueue_assessment(int(assessment.id))
            except Exception as exc:  # noqa: BLE001 - creation already committed
                reproduction_assessment.mark_enqueue_failed(
                    db,
                    assessment=assessment,
                    exc=exc,
                )
            else:
                # Eager workers and short jobs use a separate session. Reload
                # so the creation response never reports a stale queued state.
                db.refresh(assessment)
        else:
            response.status_code = status.HTTP_200_OK
        return reproduction_assessment.serialize_assessment(db, assessment)
    except reproduction_assessment.ReproductionAssessmentError as exc:
        _raise_http(exc)


@router.get("/{assessment_id}", response_model=ReproductionAssessmentOut)
def get_reproduction_assessment(
    assessment_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return reproduction_assessment.get_assessment(
            db,
            assessment_id=assessment_id,
            user_id=int(user.id),
        )
    except reproduction_assessment.ReproductionAssessmentError as exc:
        _raise_http(exc)


@router.post("/{assessment_id}/cancel", response_model=ReproductionAssessmentOut)
def cancel_reproduction_assessment(
    assessment_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        assessment = reproduction_assessment.request_cancel(
            db,
            assessment_id=assessment_id,
            user_id=int(user.id),
        )
        return reproduction_assessment.serialize_assessment(db, assessment)
    except reproduction_assessment.ReproductionAssessmentError as exc:
        _raise_http(exc)


@router.post(
    "/{assessment_id}/corrections",
    response_model=ReproductionCorrectionOut,
    status_code=status.HTTP_201_CREATED,
)
def create_reproduction_correction(
    assessment_id: int,
    body: ReproductionCorrectionCreateIn,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        correction, created = reproduction_assessment.create_correction(
            db,
            assessment_id=assessment_id,
            user_id=int(user.id),
            body=body,
        )
        if not created:
            response.status_code = status.HTTP_200_OK
        return correction
    except reproduction_assessment.ReproductionAssessmentError as exc:
        _raise_http(exc)


@router.post(
    "/{assessment_id}/remediations",
    response_model=ReproductionRemediationOut,
    status_code=status.HTTP_201_CREATED,
)
def create_reproduction_remediation(
    assessment_id: int,
    body: ReproductionRemediationCreateIn,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        remediation, created = reproduction_remediation.create_remediation(
            db,
            assessment_id=assessment_id,
            user_id=int(user.id),
            body=body,
        )
        if not created:
            response.status_code = status.HTTP_200_OK
        return remediation
    except reproduction_remediation.ReproductionRemediationError as exc:
        _raise_remediation_http(exc)


@router.get(
    "/{assessment_id}/remediations",
    response_model=ReproductionRemediationPageOut,
)
def list_reproduction_remediations(
    assessment_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return reproduction_remediation.list_remediations(
            db,
            assessment_id=assessment_id,
            user_id=int(user.id),
            limit=limit,
            offset=offset,
        )
    except reproduction_remediation.ReproductionRemediationError as exc:
        _raise_remediation_http(exc)


@router.get(
    "/{assessment_id}/remediations/{remediation_id}",
    response_model=ReproductionRemediationOut,
)
def get_reproduction_remediation(
    assessment_id: int,
    remediation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return reproduction_remediation.get_remediation(
            db,
            assessment_id=assessment_id,
            remediation_id=remediation_id,
            user_id=int(user.id),
        )
    except reproduction_remediation.ReproductionRemediationError as exc:
        _raise_remediation_http(exc)
