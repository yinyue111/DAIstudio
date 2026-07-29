"""Proposal execution, billing, persistence, and audit lifecycle."""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..models import GenerationQuote, PromptOptimizationProposal
from ..prompt_optimization_schemas import StudioPromptOptimizationIn
from . import audit, credits, gateway, usage
from .prompt_optimization_compiler import _candidate_payload, _model_compiled_preview
from .prompt_optimization_read import (
    PromptOptimizationError,
    _as_dict,
    _serialize_proposal,
)
from .prompt_optimization_request import (
    _invoke_optimizer,
    prepare_proposal_request,
    request_fingerprint,
)
from .prompt_optimization_validation import _proposal_artifacts

log = logging.getLogger(__name__)

_PROPOSAL_TTL = timedelta(minutes=30)


def _refund_reserved_proposal(
    db: Session,
    *,
    proposal_id: int,
    user_id: int,
    reserved: int,
    error: Exception,
) -> None:
    proposal = db.scalar(
        select(PromptOptimizationProposal)
        .where(PromptOptimizationProposal.id == int(proposal_id))
        .with_for_update()
    )
    if proposal is None:
        db.rollback()
        return
    if reserved:
        credits.refund(
            db,
            user_id,
            reserved,
            proposal.id,
            biz_type="prompt_optimize",
            commit=False,
        )
    proposal.status = "expired"
    proposal.charged_credits = 0
    proposal.expires_at = datetime.now(timezone.utc)
    proposal.metrics = {
        **_as_dict(proposal.metrics),
        "execution_status": "failed",
        "execution_error": str(error)[:500],
    }
    db.commit()


def reap_stale_running_proposals(
    db: Session,
    *,
    max_seconds: int | None = None,
) -> int:
    """Refund paid rewrites abandoned after the reservation was committed.

    The provider call intentionally runs outside a database transaction. A
    process crash in that window leaves an otherwise valid quote consumed and
    the user's credits frozen. Rows are rechecked under a lock so a concurrent
    successful completion wins cleanly.
    """
    timeout_seconds = max(
        60,
        int(
            max_seconds
            if max_seconds is not None
            else settings.prompt_optimization_running_timeout_seconds
        ),
    )
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=timeout_seconds)
    candidate_ids = list(
        db.scalars(
            select(PromptOptimizationProposal.id).where(
                PromptOptimizationProposal.status == "proposed",
                PromptOptimizationProposal.quote_id.is_not(None),
                PromptOptimizationProposal.updated_at < cutoff,
            )
        )
    )
    reaped = 0
    for proposal_id in candidate_ids:
        try:
            proposal = db.scalar(
                select(PromptOptimizationProposal)
                .where(PromptOptimizationProposal.id == int(proposal_id))
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if proposal is None:
                db.rollback()
                continue
            updated_at = proposal.updated_at
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            execution_status = str(_as_dict(proposal.metrics).get("execution_status") or "")
            if (
                proposal.status != "proposed"
                or execution_status != "running"
                or updated_at >= cutoff
            ):
                db.rollback()
                continue
            quote = db.get(GenerationQuote, int(proposal.quote_id))
            reserved = int(quote.estimated_credits or 0) if quote is not None else 0
            if reserved:
                credits.refund(
                    db,
                    int(proposal.user_id),
                    reserved,
                    int(proposal.id),
                    biz_type="prompt_optimize",
                    commit=False,
                )
            proposal.status = "expired"
            proposal.charged_credits = 0
            proposal.expires_at = now
            proposal.metrics = {
                **_as_dict(proposal.metrics),
                "execution_status": "failed",
                "execution_error": "提示词优化执行超时，冻结积分已退回",
                "reaped_at": now.isoformat(),
            }
            db.commit()
            reaped += 1
        except Exception:  # noqa: BLE001
            db.rollback()
            log.exception("failed to reap prompt optimization proposal %s", proposal_id)
    if reaped:
        log.info("reaped %s stale prompt optimization proposal(s)", reaped)
    return reaped


@dataclass
class _ProposalExecution:
    candidate: dict[str, Any]
    result: dict[str, Any]
    reserved_proposal: PromptOptimizationProposal | None
    charged_credits: int


def _existing_proposal_replay(
    db: Session,
    *,
    user_id: int,
    body: StudioPromptOptimizationIn,
    request_hash: str,
) -> dict[str, Any] | None:
    existing = db.scalar(
        select(PromptOptimizationProposal).where(
            PromptOptimizationProposal.user_id == user_id,
            PromptOptimizationProposal.idempotency_key == body.idempotency_key,
        )
    )
    if existing is None:
        return None
    if str((existing.metrics or {}).get("request_hash") or "") != request_hash:
        raise PromptOptimizationError(
            409,
            "idempotency_key 已用于不同的优化请求",
        )
    execution_status = str((existing.metrics or {}).get("execution_status") or "succeeded")
    if execution_status == "running":
        raise PromptOptimizationError(
            409,
            "该提示词优化请求仍在处理中",
        )
    if execution_status == "failed":
        raise PromptOptimizationError(
            409,
            "该提示词优化请求已失败并退款，请重新发起报价",
        )
    if existing.quote_id is not None and int(body.quote_id or 0) != int(existing.quote_id):
        raise PromptOptimizationError(
            409,
            "提示词优化报价与幂等请求不匹配",
        )
    return _serialize_proposal(existing)


def _reserve_paid_proposal(
    db: Session,
    *,
    user_id: int,
    body: StudioPromptOptimizationIn,
    prepared: dict[str, Any],
    request_hash: str,
    charged_credits: int,
) -> tuple[PromptOptimizationProposal, int]:
    if body.quote_id is None:
        raise PromptOptimizationError(
            422,
            "付费提示词优化必须先获取并确认服务端报价",
        )
    from .generation_quotes import (
        consume_execution_quote,
        lock_execution_quote,
        validate_prompt_optimization_quote,
    )

    quote = lock_execution_quote(
        db,
        quote_id=body.quote_id,
        user_id=user_id,
        kind="prompt_optimization",
    )
    charged_credits = validate_prompt_optimization_quote(
        db,
        quote,
        body=body,
        prepared=prepared,
    )
    operation = prepared["operation"]
    revision = prepared["revision"]
    optimizer = prepared["optimizer"]
    target_model = prepared["target_model"]
    profile = prepared["profile"]
    assert optimizer is not None
    reserved = PromptOptimizationProposal(
        user_id=user_id,
        source_operation_id=(int(operation.id) if operation is not None else None),
        source_revision_id=(int(revision.id) if revision is not None else None),
        target_model_config_id=int(target_model.id),
        capability_version_id=int(profile["capability_version_id"]),
        optimizer_model_config_id=int(optimizer.id),
        quote_id=int(quote.id),
        idempotency_key=body.idempotency_key,
        status="proposed",
        version=1,
        category=prepared["category"],
        mode=body.mode,
        optimization_kind="rewrite",
        original=deepcopy(prepared["original"]),
        suggestion=deepcopy(prepared["original"]),
        diff=[],
        constraint_coverage=[],
        warnings=[],
        provenance={"optimizer_model_config_id": int(optimizer.id)},
        catalog_snapshot=deepcopy(profile),
        charged_credits=0,
        metrics={
            "request_hash": request_hash,
            "execution_status": "running",
        },
        expires_at=datetime.now(timezone.utc) + _PROPOSAL_TTL,
    )
    db.add(reserved)
    try:
        db.flush()
        credits.freeze(
            db,
            user_id,
            charged_credits,
            reserved.id,
            biz_type="prompt_optimize",
            commit=False,
        )
        consume_execution_quote(
            quote,
            ref_type="prompt_optimization",
            ref_id=int(reserved.id),
        )
        db.commit()
    except credits.InsufficientCredits as exc:
        db.rollback()
        raise PromptOptimizationError(400, str(exc)) from exc
    return reserved, charged_credits


def _invoke_proposal_optimizer(
    db: Session,
    *,
    user_id: int,
    body: StudioPromptOptimizationIn,
    prepared: dict[str, Any],
    reserved_proposal: PromptOptimizationProposal | None,
    charged_credits: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    optimizer = prepared["optimizer"]
    revision = prepared["revision"]
    assert optimizer is not None
    try:
        result = _invoke_optimizer(
            prepared["source"],
            optimizer=optimizer,
            body=body,
            category=prepared["category"],
            context=prepared["context"],
            target_model=prepared["target_model"],
            capability_snapshot=prepared["profile"],
        )
        candidate = _candidate_payload(
            prepared["original"],
            result,
        )
    except Exception as exc:
        usage.record_call(
            db,
            kind="prompt_optimize",
            model_id=optimizer.model_id,
            model_config_id=optimizer.id,
            user_id=user_id,
            status="failed",
            detail={
                "mode": body.mode,
                "studio": True,
                "error": str(exc)[:300],
            },
        )
        if reserved_proposal is not None:
            _refund_reserved_proposal(
                db,
                proposal_id=int(reserved_proposal.id),
                user_id=user_id,
                reserved=charged_credits,
                error=exc,
            )
        if isinstance(exc, PromptOptimizationError):
            raise
        if isinstance(exc, gateway.GatewayError):
            raise PromptOptimizationError(
                502,
                f"提示词优化失败：{exc}",
            ) from exc
        raise
    usage.record_call(
        db,
        kind="prompt_optimize",
        model_id=optimizer.model_id,
        model_config_id=optimizer.id,
        user_id=user_id,
        status="ok",
        latency_ms=result.get("latency_ms"),
        usage=result.get("usage"),
        detail={
            "mode": body.mode,
            "studio": True,
            "lineage": revision is not None,
        },
    )
    return candidate, result


def _execute_proposal(
    db: Session,
    *,
    user_id: int,
    body: StudioPromptOptimizationIn,
    prepared: dict[str, Any],
    request_hash: str,
) -> _ProposalExecution:
    charged_credits = int(prepared["estimated_credits"])
    if prepared["compile_only"]:
        candidate, result = _model_compiled_preview(
            prepared["original"],
            prepared["source"],
            category=prepared["category"],
            target_model=prepared["target_model"],
            context=prepared["context"],
            capability_profile=prepared["profile"],
        )
        return _ProposalExecution(
            candidate=candidate,
            result=result,
            reserved_proposal=None,
            charged_credits=charged_credits,
        )
    reserved_proposal = None
    if charged_credits:
        reserved_proposal, charged_credits = _reserve_paid_proposal(
            db,
            user_id=user_id,
            body=body,
            prepared=prepared,
            request_hash=request_hash,
            charged_credits=charged_credits,
        )
    candidate, result = _invoke_proposal_optimizer(
        db,
        user_id=user_id,
        body=body,
        prepared=prepared,
        reserved_proposal=reserved_proposal,
        charged_credits=charged_credits,
    )
    return _ProposalExecution(
        candidate=candidate,
        result=result,
        reserved_proposal=reserved_proposal,
        charged_credits=charged_credits,
    )


def _artifacts_for_execution(
    db: Session,
    *,
    user_id: int,
    prepared: dict[str, Any],
    execution: _ProposalExecution,
    request_hash: str,
) -> dict[str, Any]:
    try:
        return _proposal_artifacts(
            original=prepared["original"],
            candidate=execution.candidate,
            result=execution.result,
            constraints=prepared["constraints"],
            profile=prepared["profile"],
            category=prepared["category"],
            source=prepared["source"],
            operation=prepared["operation"],
            revision=prepared["revision"],
            resolved_parent=prepared["resolved_parent"],
            optimizer=prepared["optimizer"],
            compile_only=prepared["compile_only"],
            request_hash=request_hash,
        )
    except Exception as exc:
        if execution.reserved_proposal is not None:
            _refund_reserved_proposal(
                db,
                proposal_id=int(execution.reserved_proposal.id),
                user_id=user_id,
                reserved=execution.charged_credits,
                error=exc,
            )
        raise


def _new_proposal_record(
    *,
    user_id: int,
    body: StudioPromptOptimizationIn,
    prepared: dict[str, Any],
    execution: _ProposalExecution,
    artifacts: dict[str, Any],
) -> PromptOptimizationProposal:
    operation = prepared["operation"]
    revision = prepared["revision"]
    optimizer = prepared["optimizer"]
    target_model = prepared["target_model"]
    profile = prepared["profile"]
    compile_only = prepared["compile_only"]
    return PromptOptimizationProposal(
        user_id=user_id,
        source_operation_id=(int(operation.id) if operation is not None else None),
        source_revision_id=(int(revision.id) if revision is not None else None),
        target_model_config_id=int(target_model.id),
        capability_version_id=int(profile["capability_version_id"]),
        optimizer_model_config_id=(
            int(optimizer.id) if not compile_only and optimizer is not None else None
        ),
        idempotency_key=body.idempotency_key,
        status="proposed",
        version=1,
        category=prepared["category"],
        mode=body.mode,
        optimization_kind=("model_compile" if compile_only else "rewrite"),
        original=prepared["original"],
        suggestion=artifacts["candidate"],
        diff=artifacts["segments"],
        constraint_coverage=artifacts["coverage"],
        warnings=artifacts["warnings"],
        provenance=artifacts["provenance"],
        catalog_snapshot=profile,
        charged_credits=execution.charged_credits,
        metrics=artifacts["metrics"],
        expires_at=datetime.now(timezone.utc) + _PROPOSAL_TTL,
    )


def _complete_reserved_proposal(
    db: Session,
    *,
    prepared: dict[str, Any],
    execution: _ProposalExecution,
    artifacts: dict[str, Any],
) -> PromptOptimizationProposal:
    assert execution.reserved_proposal is not None
    proposal = db.scalar(
        select(PromptOptimizationProposal)
        .where(PromptOptimizationProposal.id == int(execution.reserved_proposal.id))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if proposal is None:
        raise PromptOptimizationError(
            409,
            "提示词优化预留记录丢失，已停止结算",
        )
    if (
        proposal.status != "proposed"
        or str(_as_dict(proposal.metrics).get("execution_status") or "") != "running"
    ):
        db.rollback()
        raise PromptOptimizationError(
            409,
            "提示词优化执行已结束或超时，本次结果未结算",
        )
    proposal.suggestion = artifacts["candidate"]
    proposal.diff = artifacts["segments"]
    proposal.constraint_coverage = artifacts["coverage"]
    proposal.warnings = artifacts["warnings"]
    proposal.provenance = artifacts["provenance"]
    proposal.catalog_snapshot = prepared["profile"]
    proposal.charged_credits = execution.charged_credits
    proposal.metrics = artifacts["metrics"]
    return proposal


def _proposal_record_for_execution(
    db: Session,
    *,
    user_id: int,
    body: StudioPromptOptimizationIn,
    prepared: dict[str, Any],
    execution: _ProposalExecution,
    artifacts: dict[str, Any],
) -> PromptOptimizationProposal:
    if execution.reserved_proposal is None:
        return _new_proposal_record(
            user_id=user_id,
            body=body,
            prepared=prepared,
            execution=execution,
            artifacts=artifacts,
        )
    return _complete_reserved_proposal(
        db,
        prepared=prepared,
        execution=execution,
        artifacts=artifacts,
    )


def _refund_execution(
    db: Session,
    *,
    user_id: int,
    execution: _ProposalExecution,
    error: Exception,
) -> None:
    if execution.reserved_proposal is None:
        return
    _refund_reserved_proposal(
        db,
        proposal_id=int(execution.reserved_proposal.id),
        user_id=user_id,
        reserved=execution.charged_credits,
        error=error,
    )


def _persist_proposal_record(
    db: Session,
    *,
    user_id: int,
    body: StudioPromptOptimizationIn,
    request_hash: str,
    proposal: PromptOptimizationProposal,
    execution: _ProposalExecution,
) -> PromptOptimizationProposal | dict[str, Any]:
    try:
        if execution.reserved_proposal is None:
            db.add(proposal)
        elif execution.charged_credits:
            credits.settle(
                db,
                user_id,
                execution.charged_credits,
                execution.charged_credits,
                proposal.id,
                biz_type="prompt_optimize",
                commit=False,
            )
        db.commit()
        db.refresh(proposal)
        return proposal
    except IntegrityError as exc:
        db.rollback()
        duplicate = db.scalar(
            select(PromptOptimizationProposal).where(
                PromptOptimizationProposal.user_id == user_id,
                PromptOptimizationProposal.idempotency_key == body.idempotency_key,
            )
        )
        if duplicate is None or str((duplicate.metrics or {}).get("request_hash")) != request_hash:
            _refund_execution(
                db,
                user_id=user_id,
                execution=execution,
                error=exc,
            )
            raise PromptOptimizationError(
                409,
                "优化建议写入冲突",
            ) from exc
        return _serialize_proposal(duplicate)
    except Exception as exc:
        db.rollback()
        _refund_execution(
            db,
            user_id=user_id,
            execution=execution,
            error=exc,
        )
        raise


def _audit_proposed(
    db: Session,
    *,
    user_id: int,
    proposal: PromptOptimizationProposal,
    charged_credits: int,
) -> None:
    provenance = _as_dict(proposal.provenance)
    audit.log(
        db,
        user_id=user_id,
        action="studio.prompt_optimization.proposed",
        biz_type="prompt_optimization",
        biz_id=int(proposal.id),
        detail={
            "mode": proposal.mode,
            "source_operation_id": proposal.source_operation_id,
            "source_revision_id": proposal.source_revision_id,
            "selected_revision_source": provenance.get("selected_revision_source"),
            "resolved_parent_revision_id": provenance.get("resolved_parent_revision_id"),
            "resolved_parent_revision_source": provenance.get("resolved_parent_revision_source"),
            "capability_version_id": proposal.capability_version_id,
            "charged_credits": charged_credits,
        },
    )


def create_proposal(
    db: Session,
    *,
    user_id: int,
    body: StudioPromptOptimizationIn,
) -> dict[str, Any]:
    request_hash = request_fingerprint(body)
    replay = _existing_proposal_replay(
        db,
        user_id=user_id,
        body=body,
        request_hash=request_hash,
    )
    if replay is not None:
        return replay
    prepared = prepare_proposal_request(
        db,
        user_id=user_id,
        body=body,
    )
    execution = _execute_proposal(
        db,
        user_id=user_id,
        body=body,
        prepared=prepared,
        request_hash=request_hash,
    )
    artifacts = _artifacts_for_execution(
        db,
        user_id=user_id,
        prepared=prepared,
        execution=execution,
        request_hash=request_hash,
    )
    proposal = _proposal_record_for_execution(
        db,
        user_id=user_id,
        body=body,
        prepared=prepared,
        execution=execution,
        artifacts=artifacts,
    )
    persisted = _persist_proposal_record(
        db,
        user_id=user_id,
        body=body,
        request_hash=request_hash,
        proposal=proposal,
        execution=execution,
    )
    if isinstance(persisted, dict):
        return persisted
    _audit_proposed(
        db,
        user_id=user_id,
        proposal=persisted,
        charged_credits=execution.charged_credits,
    )
    return _serialize_proposal(persisted)
