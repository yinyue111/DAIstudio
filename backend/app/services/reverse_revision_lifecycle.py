"""Creation, application, and listing of reverse-result revisions."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import ReverseOperation, ReverseResultRevision
from . import reverse_lineage
from .reverse_operation_records import _get_owned
from .reverse_quotes import (
    ReverseOperationConflict,
    ReverseOperationInvalid,
    ReverseOperationNotFound,
    utcnow,
)


def list_result_revisions(
    db: Session,
    *,
    operation_id: int,
    user_id: int,
) -> list[ReverseResultRevision]:
    _get_owned(db, operation_id, user_id)
    return list(db.execute(
        select(ReverseResultRevision)
        .where(ReverseResultRevision.operation_id == operation_id)
        .order_by(ReverseResultRevision.version.asc())
    ).scalars())


def _resolve_revision_parent(
    db: Session,
    *,
    operation_id: int,
    user_id: int,
    parent_revision_id: int | None,
    allowed_parent_sources: tuple[str, ...],
    source: str,
) -> ReverseResultRevision:
    if parent_revision_id is None:
        parent = db.execute(
            select(ReverseResultRevision)
            .where(
                ReverseResultRevision.operation_id == operation_id,
                ReverseResultRevision.user_id == user_id,
                ReverseResultRevision.source.in_(allowed_parent_sources),
            )
            .order_by(ReverseResultRevision.version.desc())
            .limit(1)
        ).scalar_one_or_none()
    else:
        parent = db.get(ReverseResultRevision, int(parent_revision_id))
    if (
        parent is None
        or int(parent.operation_id) != int(operation_id)
        or int(parent.user_id) != int(user_id)
        or parent.source not in allowed_parent_sources
    ):
        expected = " 或 ".join(allowed_parent_sources)
        raise ReverseOperationConflict(
            f"{source} 必须基于同一反推任务的 {expected} 版本"
        )
    return parent


def _candidate_revision_payload(
    operation: ReverseOperation,
    *,
    source: str,
    payload: dict[str, Any],
    parent: ReverseResultRevision,
    clear_image_evidence: bool,
) -> tuple[dict[str, Any], str]:
    parent_payload = deepcopy(parent.payload) if isinstance(parent.payload, dict) else {}
    candidate = deepcopy(payload)
    parent_evidence = parent_payload.get("image_evidence")
    has_parent_evidence = isinstance(parent_evidence, list) and bool(parent_evidence)
    from .generation_image_evidence import (
        ReviewedEvidenceMaskError,
        normalize_inherited_image_evidence_for_review,
        validate_saved_reviewed_image_evidence,
    )

    if source == "user_edit":
        if clear_image_evidence:
            if candidate.get("image_evidence") != [] or not has_parent_evidence:
                raise ReverseOperationInvalid(
                    "明确清空图片证据时必须从非空证据开始并传 image_evidence=[]"
                )
            evidence_action = "cleared"
        elif "image_evidence" not in candidate:
            if "image_evidence" in parent_payload:
                try:
                    candidate["image_evidence"] = (
                        normalize_inherited_image_evidence_for_review(parent_evidence)
                    )
                except ReviewedEvidenceMaskError as exc:
                    raise ReverseOperationInvalid(
                        f"图片证据审阅数据无效：{exc}"
                    ) from exc
                evidence_action = "inherited"
            else:
                evidence_action = "not_applicable"
        elif candidate.get("image_evidence") == [] and has_parent_evidence:
            raise ReverseOperationInvalid(
                "清空图片证据必须显式设置 clear_image_evidence=true"
            )
        else:
            evidence_action = "updated"
    else:
        if clear_image_evidence:
            raise ReverseOperationInvalid("只能在 user_edit 版本中明确清空图片证据")
        if candidate:
            if "image_evidence" not in candidate and "image_evidence" in parent_payload:
                candidate["image_evidence"] = deepcopy(parent_evidence)
            if candidate.get("image_evidence") == [] and has_parent_evidence:
                raise ReverseOperationInvalid(
                    "applied 不能跳过 user_edit 直接清空图片证据"
                )
            if reverse_lineage.canonical_payload_hash(candidate) != parent.payload_hash:
                raise ReverseOperationInvalid(
                    "applied 必须完整应用其 user_edit 父版本，不能同时改写内容"
                )
        candidate = parent_payload
        evidence_action = (
            "inherited" if "image_evidence" in parent_payload else "not_applicable"
        )

    try:
        validate_saved_reviewed_image_evidence(
            operation,
            candidate,
            require_all_confirmed=source == "applied",
        )
    except ReviewedEvidenceMaskError as exc:
        raise ReverseOperationInvalid(f"图片证据审阅数据无效：{exc}") from exc
    return candidate, evidence_action


def create_result_revision(
    db: Session,
    *,
    operation_id: int,
    user_id: int,
    source: str,
    payload: dict[str, Any],
    parent_revision_id: int | None = None,
    clear_image_evidence: bool = False,
    allow_applied_correction_parent: bool = False,
    commit: bool = True,
) -> ReverseResultRevision:
    operation = db.execute(
        select(ReverseOperation)
        .where(
            ReverseOperation.id == operation_id,
            ReverseOperation.user_id == user_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if operation is None:
        raise ReverseOperationNotFound("反推任务不存在")
    if operation.status != "succeeded":
        raise ReverseOperationConflict("反推任务成功后才能保存结果版本")
    allowed_parent_sources = reverse_lineage.allowed_parent_sources(source)
    if source == "user_edit" and not allow_applied_correction_parent:
        allowed_parent_sources = tuple(
            parent_source
            for parent_source in allowed_parent_sources
            if parent_source != "applied"
        )
    if not allowed_parent_sources or not set(allowed_parent_sources).issubset(
        {"normalized", "user_edit", "applied"}
    ):
        raise ReverseOperationInvalid("客户端反推版本类型无效")
    parent = _resolve_revision_parent(
        db,
        operation_id=operation_id,
        user_id=user_id,
        parent_revision_id=parent_revision_id,
        allowed_parent_sources=allowed_parent_sources,
        source=source,
    )
    try:
        reverse_lineage.validate_revision_chain(
            db,
            parent,
            terminal_source=parent.source,
        )
        reverse_lineage.reject_client_lineage_fields(payload)
    except reverse_lineage.ReverseLineageError as exc:
        raise ReverseOperationInvalid(str(exc)) from exc

    candidate, evidence_action = _candidate_revision_payload(
        operation,
        source=source,
        payload=payload,
        parent=parent,
        clear_image_evidence=clear_image_evidence,
    )
    latest = db.execute(
        select(func.max(ReverseResultRevision.version)).where(
            ReverseResultRevision.operation_id == operation_id
        )
    ).scalar_one()
    revision = ReverseResultRevision(
        operation_id=operation_id,
        user_id=user_id,
        version=int(latest or 0) + 1,
        source=source,
        payload=candidate,
        parent_revision_id=int(parent.id),
        source_content_hash=parent.source_content_hash,
        source_fingerprints=deepcopy(parent.source_fingerprints),
        payload_hash=reverse_lineage.canonical_payload_hash(candidate),
        lineage_status=reverse_lineage.VERIFIED,
        evidence_review_action=evidence_action,
    )
    try:
        db.add(revision)
        db.flush()
        if source == "applied":
            operation.applied_result_version = revision.version
            operation.updated_at = utcnow()
        if commit:
            db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ReverseOperationConflict(
            "反推结果版本已被并发更新，请刷新后重试"
        ) from exc
    if commit:
        db.refresh(revision)
    return revision


def apply_result_revision(
    db: Session,
    *,
    operation_id: int,
    user_id: int,
    payload: dict[str, Any],
    parent_revision_id: int,
    clear_image_evidence: bool = False,
    commit: bool = True,
) -> tuple[ReverseResultRevision, ReverseResultRevision]:
    """Persist a reviewed edit and its application as one transaction."""
    operation = db.execute(
        select(ReverseOperation)
        .where(
            ReverseOperation.id == operation_id,
            ReverseOperation.user_id == user_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if operation is None:
        raise ReverseOperationNotFound("反推任务不存在")
    if operation.status != "succeeded":
        raise ReverseOperationConflict("反推任务成功后才能应用结果")

    latest_parent = db.execute(
        select(ReverseResultRevision)
        .where(
            ReverseResultRevision.operation_id == operation_id,
            ReverseResultRevision.user_id == user_id,
            ReverseResultRevision.source.in_(("normalized", "user_edit")),
        )
        .order_by(ReverseResultRevision.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest_parent is None:
        raise ReverseOperationConflict("反推结果缺少可应用的 normalized 版本")
    if int(latest_parent.id) != int(parent_revision_id):
        raise ReverseOperationConflict("反推结果版本已更新，请刷新后重试")

    try:
        edited = create_result_revision(
            db,
            operation_id=operation_id,
            user_id=user_id,
            source="user_edit",
            payload=payload,
            parent_revision_id=int(latest_parent.id),
            clear_image_evidence=clear_image_evidence,
            commit=False,
        )
        applied = create_result_revision(
            db,
            operation_id=operation_id,
            user_id=user_id,
            source="applied",
            payload={},
            parent_revision_id=int(edited.id),
            commit=False,
        )
        if commit:
            db.commit()
    except (ReverseOperationNotFound, ReverseOperationConflict, ReverseOperationInvalid):
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        raise ReverseOperationConflict(
            "反推结果版本已被并发更新，请刷新后重试"
        ) from exc
    except Exception:
        db.rollback()
        raise

    if commit:
        db.refresh(edited)
        db.refresh(applied)
    return edited, applied
