"""Decision and revision application for prompt optimization proposals."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import AuditLog, PromptOptimizationProposal
from . import reverse_operations
from .prompt_optimization_compiler import _set_path
from .prompt_optimization_read import (
    PromptOptimizationError,
    _as_dict,
    _load_lineage_source,
    _resolve_user_edit_parent,
)


def _load_owned_proposal(
    db: Session,
    *,
    user_id: int,
    proposal_id: int,
) -> PromptOptimizationProposal:
    proposal = db.scalar(
        select(PromptOptimizationProposal)
        .where(
            PromptOptimizationProposal.id == int(proposal_id),
            PromptOptimizationProposal.user_id == int(user_id),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if proposal is None:
        raise PromptOptimizationError(404, "优化建议不存在")
    return proposal


def _ensure_proposal_decidable(
    db: Session,
    *,
    proposal: PromptOptimizationProposal,
    proposal_version: int,
    idempotency_key: str,
) -> dict[str, Any] | None:
    if proposal.status != "proposed":
        if proposal.decision_idempotency_key == idempotency_key and isinstance(
            proposal.decision_result, dict
        ):
            return deepcopy(proposal.decision_result)
        raise PromptOptimizationError(409, "该优化建议已处理")
    if int(proposal.version) != int(proposal_version):
        raise PromptOptimizationError(409, "优化建议版本已更新，请刷新后重试")
    expires_at = proposal.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        proposal.status = "expired"
        proposal.version += 1
        db.commit()
        raise PromptOptimizationError(410, "优化建议已过期")
    return None


def _resolve_segment_decision(
    proposal: PromptOptimizationProposal,
    *,
    accepted_segment_ids: list[str],
    rejected_segment_ids: list[str],
    reject_all: bool,
) -> tuple[set[str], set[str], set[str], dict[str, Any]]:
    segments = deepcopy(proposal.diff or [])
    segment_map = {str(item.get("id")): item for item in segments if isinstance(item, dict)}
    all_ids = set(segment_map)
    accepted = set(accepted_segment_ids)
    rejected = set(rejected_segment_ids)
    if not accepted.issubset(all_ids) or not rejected.issubset(all_ids):
        raise PromptOptimizationError(422, "决策中包含不属于该建议的分段")
    if reject_all:
        accepted = set()
        rejected = all_ids
    elif not accepted and not rejected:
        accepted = all_ids
    else:
        rejected |= all_ids - accepted - rejected
    result = deepcopy(_as_dict(proposal.original))
    for segment_id in accepted:
        segment = segment_map[segment_id]
        _set_path(result, str(segment["field_path"]), segment.get("suggestion"))
    return all_ids, accepted, rejected, result


def _create_reverse_revision(
    db: Session,
    *,
    user_id: int,
    accepted: set[str],
    result: dict[str, Any],
    provenance: dict[str, Any],
) -> dict[str, Any] | None:
    if not (
        accepted
        and provenance.get("reverse_operation_id")
        and provenance.get("reverse_revision_id")
    ):
        return None
    operation, revision, chain = _load_lineage_source(
        db,
        user_id=user_id,
        operation_id=int(provenance["reverse_operation_id"]),
        revision_id=int(provenance["reverse_revision_id"]),
    )
    resolved_parent = _resolve_user_edit_parent(revision, chain)
    if (
        revision.payload_hash != provenance.get("reverse_revision_hash")
        or int(revision.id) != int(provenance.get("selected_revision_id") or 0)
        or revision.source != provenance.get("selected_revision_source")
        or int(resolved_parent.id) != int(provenance.get("resolved_parent_revision_id") or 0)
        or resolved_parent.source != provenance.get("resolved_parent_revision_source")
    ):
        raise PromptOptimizationError(409, "反推源版本已无法验证")
    try:
        edited = reverse_operations.create_result_revision(
            db,
            operation_id=int(operation.id),
            user_id=user_id,
            source="user_edit",
            payload=result,
            parent_revision_id=int(resolved_parent.id),
            commit=False,
        )
        created = reverse_operations.create_result_revision(
            db,
            operation_id=int(operation.id),
            user_id=user_id,
            source="applied",
            payload=result,
            parent_revision_id=int(edited.id),
            commit=False,
        )
    except (
        reverse_operations.ReverseOperationInvalid,
        reverse_operations.ReverseOperationConflict,
        reverse_operations.ReverseOperationNotFound,
    ) as exc:
        db.rollback()
        raise PromptOptimizationError(409, str(exc)) from exc
    return {
        "id": int(created.id),
        "operation_id": int(created.operation_id),
        "version": int(created.version),
        "source": created.source,
        "payload": deepcopy(created.payload),
        "parent_revision_id": int(created.parent_revision_id),
        "payload_hash": created.payload_hash,
        "lineage_status": created.lineage_status,
        "user_edit_revision_id": int(edited.id),
        "selected_revision_id": int(revision.id),
        "selected_revision_source": revision.source,
        "resolved_parent_revision_id": int(resolved_parent.id),
        "resolved_parent_revision_source": resolved_parent.source,
    }


def _decision_contract(
    proposal: PromptOptimizationProposal,
    *,
    all_ids: set[str],
    accepted: set[str],
    rejected: set[str],
    result: dict[str, Any],
    revision: dict[str, Any] | None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    status = (
        "rejected" if not accepted else "accepted" if accepted == all_ids else "partially_accepted"
    )
    decision_result = {
        "proposal_id": int(proposal.id),
        "proposal_version": int(proposal.version) + 1,
        "status": status,
        "accepted_segment_ids": sorted(accepted),
        "rejected_segment_ids": sorted(rejected),
        "result": result if accepted else None,
        "revision": revision,
    }
    metrics = {
        **deepcopy(proposal.metrics or {}),
        "adoption_rate": len(accepted) / max(1, len(all_ids)),
        "accepted_segments": len(accepted),
        "total_segments": len(all_ids),
        "converted_to_revision": revision is not None,
    }
    return status, decision_result, metrics


def _persist_decision(
    db: Session,
    *,
    user_id: int,
    proposal: PromptOptimizationProposal,
    proposal_version: int,
    idempotency_key: str,
    status: str,
    accepted: set[str],
    rejected: set[str],
    decision_result: dict[str, Any],
    metrics: dict[str, Any],
    revision: dict[str, Any] | None,
) -> None:
    changed = db.execute(
        update(PromptOptimizationProposal)
        .where(
            PromptOptimizationProposal.id == proposal.id,
            PromptOptimizationProposal.user_id == user_id,
            PromptOptimizationProposal.status == "proposed",
            PromptOptimizationProposal.version == proposal_version,
        )
        .values(
            status=status,
            version=proposal_version + 1,
            decision_idempotency_key=idempotency_key,
            accepted_segment_ids=sorted(accepted),
            rejected_segment_ids=sorted(rejected),
            decision_result=decision_result,
            metrics=metrics,
            decided_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )
    if changed.rowcount != 1:
        db.rollback()
        raise PromptOptimizationError(409, "优化建议已被并发处理")
    db.add(
        AuditLog(
            user_id=user_id,
            action=f"studio.prompt_optimization.{status}",
            biz_type="prompt_optimization",
            biz_id=int(proposal.id),
            detail={
                "accepted_segment_ids": sorted(accepted),
                "rejected_segment_ids": sorted(rejected),
                "revision_id": revision["id"] if revision else None,
                "selected_revision_id": revision["selected_revision_id"] if revision else None,
                "selected_revision_source": revision["selected_revision_source"]
                if revision
                else None,
                "resolved_parent_revision_id": (
                    revision["resolved_parent_revision_id"] if revision else None
                ),
                "adoption_rate": metrics["adoption_rate"],
            },
        )
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise PromptOptimizationError(
            409,
            "反推结果版本已被并发更新，请刷新后重试",
        ) from exc


def decide_proposal(
    db: Session,
    *,
    user_id: int,
    proposal_id: int,
    proposal_version: int,
    idempotency_key: str,
    accepted_segment_ids: list[str],
    rejected_segment_ids: list[str],
    reject_all: bool = False,
) -> dict[str, Any]:
    proposal = _load_owned_proposal(db, user_id=user_id, proposal_id=proposal_id)
    replay = _ensure_proposal_decidable(
        db,
        proposal=proposal,
        proposal_version=proposal_version,
        idempotency_key=idempotency_key,
    )
    if replay is not None:
        return replay
    all_ids, accepted, rejected, result = _resolve_segment_decision(
        proposal,
        accepted_segment_ids=accepted_segment_ids,
        rejected_segment_ids=rejected_segment_ids,
        reject_all=reject_all,
    )
    revision = _create_reverse_revision(
        db,
        user_id=user_id,
        accepted=accepted,
        result=result,
        provenance=deepcopy(proposal.provenance or {}),
    )
    status, decision_result, metrics = _decision_contract(
        proposal,
        all_ids=all_ids,
        accepted=accepted,
        rejected=rejected,
        result=result,
        revision=revision,
    )
    _persist_decision(
        db,
        user_id=user_id,
        proposal=proposal,
        proposal_version=proposal_version,
        idempotency_key=idempotency_key,
        status=status,
        accepted=accepted,
        rejected=rejected,
        decision_result=decision_result,
        metrics=metrics,
        revision=revision,
    )
    return decision_result
