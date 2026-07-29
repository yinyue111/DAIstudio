"""History, usage accounting, and successful settlement for reverse operations."""
from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import ReverseOperation, ReverseResultRevision
from . import credits, reverse_lineage, usage
from .prompt_history import remember_prompt
from .reverse_quotes import BIZ_TYPE, _log_operation_event, utcnow

log = logging.getLogger("reverse_operations")


def _history_title(target: str) -> tuple[str, str]:
    if target == "video":
        return "视频反推提示词", "video"
    if target == "portrait_profile":
        return "人物身份档案", "image"
    if target == "product_profile":
        return "产品身份档案", "image"
    return "图片反推提示词", "image"


def _remember_history(operation: ReverseOperation, result: dict[str, Any]) -> None:
    db = SessionLocal()
    try:
        title, category = _history_title(operation.target)
        context = dict(operation.request_context or {})
        workspace = (
            dict(context.get("workspace_snapshot_v2"))
            if isinstance(context.get("workspace_snapshot_v2"), dict)
            else {}
        )
        snapshot = {
            **workspace,
            "version": 2,
            "target": operation.target,
            "request_context": context,
            "workspace_snapshot_v2": workspace or None,
            "structured": result.get("structured"),
            "final_text": result.get("final_text"),
            "video_analysis": result.get("video_analysis"),
            "reference_count": max(1, int(operation.reference_count or 1)),
        }
        if operation.target in {"product_profile", "portrait_profile"}:
            profile_structured = (
                dict(result.get("structured"))
                if isinstance(result.get("structured"), dict)
                else {}
            )
            profile = {
                "structured": profile_structured,
                "final_text": str(result.get("final_text") or ""),
            }
            profile_key = operation.target
            snapshot["subject_mode"] = (
                "portrait" if operation.target == "portrait_profile" else "product"
            )
            snapshot["subject_profile"] = profile
            snapshot[profile_key] = profile
            if not isinstance(snapshot.get("product_asset"), dict):
                selected = snapshot.get("selected")
                snapshot["product_asset"] = (
                    dict(selected)
                    if isinstance(selected, dict)
                    else {"type": "image", "url": operation.asset_url}
                )
        remember_prompt(
            db,
            user_id=operation.user_id,
            prompt=result.get("final_text"),
            title=title,
            category=category,
            source="reverse",
            params={
                "asset_url": operation.asset_url,
                "reference_count": max(1, int(operation.reference_count or 1)),
                "workspace_snapshot_v2": workspace or None,
                "reverse_snapshot_v2": snapshot,
            },
            commit=True,
        )
    except Exception:
        db.rollback()
        log.exception("failed to remember async reverse history operation=%s", operation.id)
    finally:
        db.close()


def _record_gateway_failure(
    db: Session,
    *,
    operation_id: int,
    model_id: str | None,
    user_id: int,
    target: str,
    contract_target: str,
    preset: str,
    cost_credits: int,
    provider_cost_detail: dict[str, Any],
    phase: str,
    error_code: str,
    error: str,
    repair_attempted: bool,
    result: dict[str, Any] | None = None,
) -> None:
    operation = db.get(ReverseOperation, operation_id)
    usage.record_call(
        db,
        kind="reverse",
        model_id=model_id,
        user_id=user_id,
        model_config_id=getattr(operation, "model_config_id", None),
        status="failed",
        latency_ms=(result or {}).get("latency_ms"),
        usage=(result or {}).get("usage"),
        detail={
            "operation_id": operation_id,
            "target": target,
            "contract_target": contract_target,
            "phase": phase,
            "error_code": error_code,
            "preset": preset,
            "cost_credits": max(0, int(cost_credits)),
            **provider_cost_detail,
            "repair_attempted": bool(repair_attempted),
            "error": str(error)[:500],
        },
    )


def _finish_success_impl(
    db: Session,
    operation_id: int,
    *,
    result: dict[str, Any],
    provider_result: dict[str, Any] | None = None,
    source_fingerprints: list[dict[str, Any]] | None = None,
    reference_count: int,
    real_cost: int,
    refund_locked,
    merge_completed_shot_reanalysis,
) -> ReverseOperation | None:
    operation = db.execute(
        select(ReverseOperation)
        .where(ReverseOperation.id == operation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if operation is None or operation.status != "running":
        db.rollback()
        return None
    if operation.cancel_requested:
        refund_locked(db, operation, note="canceled after reverse gateway response")
        operation.status = "canceled"
        operation.phase = None
        operation.progress = 100
        operation.error_code = "CANCELED"
        operation.finished_at = utcnow()
        operation.updated_at = utcnow()
        db.commit()
        _log_operation_event(
            "reverse_operation_canceled",
            operation_id=operation.id,
            status=operation.status,
            phase=operation.phase,
            error_code=operation.error_code,
        )
        return None
    reserved = int(operation.cost_frozen or 0)
    real_cost = min(max(int(real_cost), 0), reserved)
    credits.settle(
        db,
        operation.user_id,
        reserved,
        real_cost,
        operation.id,
        biz_type=BIZ_TYPE,
        commit=False,
    )
    operation.cost_frozen = 0
    operation.cost_settled = real_cost
    operation.charged_credits = real_cost
    operation.reference_count = max(1, int(reference_count))
    result = dict(result)
    result["reference_count"] = operation.reference_count
    result["charged_credits"] = real_cost
    # Keep ORM JSON values detached from the working payload. Shot reanalysis
    # may append merge metadata after a nested flush; sharing the same dict
    # would make SQLAlchemy miss that later JSON change.
    operation.result = deepcopy(result)
    operation.normalized_result = deepcopy(result)
    operation.raw_provider_result = dict(provider_result) if isinstance(provider_result, dict) else None
    operation.status = "succeeded"
    operation.phase = None
    operation.progress = 100
    operation.error_code = None
    operation.error = None
    operation.finished_at = utcnow()
    operation.updated_at = utcnow()
    fingerprints = deepcopy(source_fingerprints) if source_fingerprints else None
    lineage_status = (
        reverse_lineage.VERIFIED if fingerprints else reverse_lineage.LEGACY_UNVERIFIED
    )
    source_hash = (
        reverse_lineage.source_content_hash(fingerprints) if fingerprints else None
    )
    provider_payload = (
        dict(provider_result)
        if isinstance(provider_result, dict)
        else {
            "schema_version": "provider-raw.unavailable.v1",
            "degraded": True,
        }
    )
    provider_revision = ReverseResultRevision(
        operation_id=operation.id,
        user_id=operation.user_id,
        version=1,
        source="provider_raw",
        payload=provider_payload,
        parent_revision_id=None,
        source_content_hash=source_hash,
        source_fingerprints=fingerprints,
        payload_hash=reverse_lineage.canonical_payload_hash(provider_payload),
        lineage_status=lineage_status,
        evidence_review_action="not_applicable",
    )
    db.add(provider_revision)
    db.flush()
    normalized_payload = deepcopy(result)
    normalized_revision = ReverseResultRevision(
        operation_id=operation.id,
        user_id=operation.user_id,
        version=2,
        source="normalized",
        payload=normalized_payload,
        parent_revision_id=int(provider_revision.id),
        source_content_hash=source_hash,
        source_fingerprints=deepcopy(fingerprints),
        payload_hash=reverse_lineage.canonical_payload_hash(normalized_payload),
        lineage_status=lineage_status,
        evidence_review_action=(
            "updated" if "image_evidence" in normalized_payload else "not_applicable"
        ),
    )
    db.add(normalized_revision)
    db.flush()
    if isinstance((operation.request_context or {}).get("shot_reanalysis"), dict):
        try:
            with db.begin_nested():
                merge_result = merge_completed_shot_reanalysis(
                    db,
                    child_operation=operation,
                    child_revision=normalized_revision,
                    result=result,
                )
        except Exception as exc:  # noqa: BLE001 - child result remains valid
            log.exception(
                "shot reanalysis merge failed child_operation_id=%s",
                operation.id,
            )
            merge_result = {
                "status": "failed",
                "reason": f"重分析结果自动合并失败：{str(exc)[:200]}",
            }
        result["shot_reanalysis_merge"] = merge_result
        operation.result = deepcopy(result)
        operation.normalized_result = deepcopy(result)
        normalized_revision.payload = deepcopy(result)
        normalized_revision.payload_hash = reverse_lineage.canonical_payload_hash(result)
    db.commit()
    db.refresh(operation)
    _log_operation_event(
        "reverse_operation_succeeded",
        operation_id=operation.id,
        status=operation.status,
        phase=operation.phase,
        error_code=operation.error_code,
        target=operation.target,
        cost_settled=int(operation.cost_settled or 0),
    )
    return operation
