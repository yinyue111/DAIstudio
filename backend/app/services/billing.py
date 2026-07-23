"""Read-only user credit ledger projections.

The credit transaction table remains the source of truth. This module only
groups existing rows and enriches them with the referenced business objects.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from ..models import (
    AuditLog,
    CreditTransaction,
    GenAsset,
    GenerationQuote,
    GenTask,
    ModelConfig,
    PaymentOrder,
    PromptOptimizationProposal,
    ReverseOperation,
    ToolDefinition,
    ToolRun,
    WorkflowRun,
)

TRANSACTION_TYPES = {"grant", "freeze", "settle", "refund", "unlock", "consume"}
BILLING_KINDS = {
    "all",
    "generation",
    "reverse",
    "workflow",
    "prompt",
    "unlock",
    "recharge",
    "grant",
    "other",
}
_KNOWN_BIZ_TYPES = {
    "gen_task",
    "reverse_operation",
    "reverse",
    "tool_run",
    "prompt_optimize",
    "unlock",
    "payment",
    "admin",
}
_MODEL_NOTE_RE = re.compile(r"(?:^|\s)model=([^\s]+)")
_PROMPT_MODE_LABELS = {
    "faithful": "忠实整理",
    "concise": "精简",
    "expand": "扩写",
    "commercial": "商业增强",
    "cinematic": "电影化",
    "constraints": "约束补全",
    "translate": "翻译",
    "model_adaptation": "模型适配",
    "target_model_adaptation": "目标模型适配",
}


class InvalidBillingCursor(ValueError):
    pass


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _encode_cursor(created_at: datetime, row_id: int) -> str:
    payload = {"created_at": _utc(created_at).isoformat(), "id": int(row_id)}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(value: str | None) -> tuple[datetime, int] | None:
    if not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        created_at = datetime.fromisoformat(str(payload["created_at"]).replace("Z", "+00:00"))
        row_id = int(payload["id"])
        if row_id <= 0:
            raise ValueError
        return _utc(created_at), row_id
    except (KeyError, TypeError, ValueError, UnicodeError, binascii.Error) as exc:
        raise InvalidBillingCursor("账单游标无效") from exc


def _transaction_dict(row: CreditTransaction) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "type": row.type,
        "change": int(row.change or 0),
        "balance_delta": int(row.balance_delta if row.balance_delta is not None else row.change or 0),
        "frozen_delta": int(row.frozen_delta or 0),
        "balance_after": int(row.balance_after or 0),
        "frozen_after": int(row.frozen_after) if row.frozen_after is not None else None,
        "reserved_amount": int(row.reserved_amount) if row.reserved_amount is not None else None,
        "real_cost": int(row.real_cost) if row.real_cost is not None else None,
        "biz_type": row.biz_type,
        "biz_ref": int(row.biz_ref) if row.biz_ref is not None else None,
        "note": row.note,
        "created_at": row.created_at,
    }


def list_credit_transactions(
    db: Session,
    *,
    user_id: int,
    transaction_type: str = "all",
    biz_type: str | None = None,
    limit: int = 30,
    cursor: str | None = None,
) -> dict[str, Any]:
    cursor_key = _decode_cursor(cursor)
    filters = [CreditTransaction.user_id == user_id]
    if transaction_type != "all":
        filters.append(CreditTransaction.type == transaction_type)
    normalized_biz_type = str(biz_type or "").strip()
    if normalized_biz_type:
        filters.append(CreditTransaction.biz_type == normalized_biz_type)

    stmt = select(CreditTransaction).where(*filters)
    if cursor_key is not None:
        # Ledger ids are append-only and give identical cursor behavior on
        # PostgreSQL and SQLite, whose timestamp precision differs in tests.
        stmt = stmt.where(CreditTransaction.id < cursor_key[1])
    rows = list(
        db.scalars(
            stmt.order_by(CreditTransaction.id.desc()).limit(limit + 1)
        )
    )
    has_more = len(rows) > limit
    selected = rows[:limit]
    total = int(db.scalar(select(func.count()).select_from(CreditTransaction).where(*filters)) or 0)
    return {
        "items": [_transaction_dict(row) for row in selected],
        "next_cursor": (
            _encode_cursor(selected[-1].created_at, int(selected[-1].id))
            if has_more and selected
            else None
        ),
        "has_more": has_more,
        "total": total,
    }


def _kind_clause(kind: str):
    if kind == "generation":
        return CreditTransaction.biz_type == "gen_task"
    if kind == "reverse":
        return CreditTransaction.biz_type.in_(("reverse_operation", "reverse"))
    if kind == "workflow":
        return CreditTransaction.biz_type == "tool_run"
    if kind == "prompt":
        return CreditTransaction.biz_type == "prompt_optimize"
    if kind == "unlock":
        return CreditTransaction.biz_type == "unlock"
    if kind == "recharge":
        return CreditTransaction.biz_type == "payment"
    if kind == "grant":
        return and_(
            CreditTransaction.type == "grant",
            or_(
                CreditTransaction.biz_type.is_(None),
                CreditTransaction.biz_type != "payment",
            ),
        )
    if kind == "other":
        return and_(
            CreditTransaction.type != "grant",
            or_(
                CreditTransaction.biz_type.is_(None),
                CreditTransaction.biz_type.notin_(_KNOWN_BIZ_TYPES),
            ),
        )
    return None


def _status_group(status: str) -> str:
    if status in {"queued", "running", "pending", "compensating"}:
        return "active"
    if status in {"failed"}:
        return "failed"
    if status in {"canceled", "closed"}:
        return "canceled"
    if status in {"waiting_review", "needs_review", "needs_confirmation"}:
        return "needs_attention"
    if status == "refunded":
        return "refunded"
    if status == "credited":
        return "credited"
    return "succeeded"


def _task_model_snapshot(task: GenTask | None) -> dict[str, Any]:
    params = task.params if task is not None and isinstance(task.params, dict) else {}
    snapshot = params.get("_model_snapshot")
    return snapshot if isinstance(snapshot, dict) else {}


def _reverse_model_snapshot(operation: ReverseOperation | None) -> dict[str, Any]:
    snapshot = operation.model_snapshot if operation is not None else None
    return snapshot if isinstance(snapshot, dict) else {}


def _model_fields(
    snapshot: dict[str, Any],
    model_config_id: int | None,
    models: dict[int, ModelConfig],
) -> dict[str, Any]:
    model = models.get(int(model_config_id)) if model_config_id is not None else None
    return {
        "model_config_id": model_config_id,
        "model_name": snapshot.get("model_name") or (model.display_name if model else None),
        "model_id": snapshot.get("model_id") or (model.model_id if model else None),
        "model_provider": snapshot.get("provider") or (model.provider if model else None),
    }


def _quote_fields(quote: GenerationQuote | None) -> dict[str, Any]:
    if quote is None:
        return {
            "quote_id": None,
            "quote_kind": None,
            "quote_status": None,
            "quoted_credits": None,
            "price_version_id": None,
        }
    return {
        "quote_id": int(quote.id),
        "quote_kind": quote.kind,
        "quote_status": quote.status,
        "quoted_credits": int(quote.estimated_credits or 0),
        "price_version_id": int(quote.price_version_id) if quote.price_version_id else None,
    }


def _base_metadata(entry: dict[str, Any]) -> dict[str, Any]:
    refunded = int(entry["refunded_credits"] or 0)
    consumed = int(entry["net_consumed_credits"] or 0)
    credited = int(entry["credited_credits"] or 0)
    if credited > 0:
        status = "credited"
    elif consumed == 0 and refunded > 0 and int(entry["outstanding_frozen_credits"] or 0) == 0:
        status = "refunded"
    else:
        status = "succeeded"
    return {
        "title": "积分变动",
        "subtitle": entry.get("note"),
        "status": status,
        "status_group": _status_group(status),
        "related_kind": None,
        "related_id": None,
        "model_config_id": None,
        "model_name": None,
        "model_id": None,
        "model_provider": None,
        **_quote_fields(None),
        "review_status": None,
        "review_reason": None,
        "review_note": None,
        "reviewed_at": None,
    }


def _enrich_entries(db: Session, user_id: int, entries: list[dict[str, Any]]) -> None:
    gen_ids = {e["biz_ref"] for e in entries if e["biz_type"] == "gen_task" and e["biz_ref"]}
    reverse_ids = {
        e["biz_ref"]
        for e in entries
        if e["biz_type"] in {"reverse_operation", "reverse"} and e["biz_ref"]
    }
    payment_ids = {e["biz_ref"] for e in entries if e["biz_type"] == "payment" and e["biz_ref"]}
    asset_ids = {e["biz_ref"] for e in entries if e["biz_type"] == "unlock" and e["biz_ref"]}
    tool_run_ids = {e["biz_ref"] for e in entries if e["biz_type"] == "tool_run" and e["biz_ref"]}
    prompt_proposal_ids = {
        e["biz_ref"]
        for e in entries
        if e["biz_type"] == "prompt_optimize" and e["biz_ref"]
    }

    gen_tasks = {
        int(row.id): row
        for row in db.scalars(
            select(GenTask).where(GenTask.user_id == user_id, GenTask.id.in_(gen_ids))
        )
    } if gen_ids else {}
    reverse_ops = {
        int(row.id): row
        for row in db.scalars(
            select(ReverseOperation).where(
                ReverseOperation.user_id == user_id,
                ReverseOperation.id.in_(reverse_ids),
            )
        )
    } if reverse_ids else {}
    payments = {
        int(row.id): row
        for row in db.scalars(
            select(PaymentOrder).where(
                PaymentOrder.user_id == user_id,
                PaymentOrder.id.in_(payment_ids),
            )
        )
    } if payment_ids else {}
    assets = {
        int(row.id): row
        for row in db.scalars(
            select(GenAsset).where(GenAsset.user_id == user_id, GenAsset.id.in_(asset_ids))
        )
    } if asset_ids else {}
    tool_runs = {
        int(row.id): row
        for row in db.scalars(
            select(ToolRun).where(ToolRun.user_id == user_id, ToolRun.id.in_(tool_run_ids))
        )
    } if tool_run_ids else {}
    prompt_proposals = {
        int(row.id): row
        for row in db.scalars(
            select(PromptOptimizationProposal).where(
                PromptOptimizationProposal.user_id == user_id,
                PromptOptimizationProposal.id.in_(prompt_proposal_ids),
            )
        )
    } if prompt_proposal_ids else {}
    tool_definition_ids = {int(row.tool_definition_id) for row in tool_runs.values()}
    tool_definitions = {
        int(row.id): row
        for row in db.scalars(
            select(ToolDefinition).where(ToolDefinition.id.in_(tool_definition_ids))
        )
    } if tool_definition_ids else {}
    workflow_runs = {
        int(row.tool_run_id): row
        for row in db.scalars(
            select(WorkflowRun).where(WorkflowRun.tool_run_id.in_(tool_run_ids))
        )
    } if tool_run_ids else {}
    unlock_task_ids = {int(asset.task_id) for asset in assets.values()}
    unlock_tasks = {
        int(row.id): row
        for row in db.scalars(
            select(GenTask).where(GenTask.user_id == user_id, GenTask.id.in_(unlock_task_ids))
        )
    } if unlock_task_ids else {}

    model_config_ids: set[int] = set()
    for task in [*gen_tasks.values(), *unlock_tasks.values()]:
        if task.model_config_id is not None:
            model_config_ids.add(int(task.model_config_id))
    for operation in reverse_ops.values():
        if operation.model_config_id is not None:
            model_config_ids.add(int(operation.model_config_id))
    for proposal in prompt_proposals.values():
        model_config_ids.add(int(proposal.target_model_config_id))
        if proposal.optimizer_model_config_id is not None:
            model_config_ids.add(int(proposal.optimizer_model_config_id))
    models = {
        int(row.id): row
        for row in db.scalars(select(ModelConfig).where(ModelConfig.id.in_(model_config_ids)))
    } if model_config_ids else {}

    quote_ids = {
        int(quote_id)
        for quote_id in [
            *(task.quote_id for task in gen_tasks.values()),
            *(operation.quote_id for operation in reverse_ops.values()),
            *(run.quote_id for run in tool_runs.values()),
            *(proposal.quote_id for proposal in prompt_proposals.values()),
        ]
        if quote_id is not None
    }
    quotes = {
        int(row.id): row
        for row in db.scalars(
            select(GenerationQuote).where(
                GenerationQuote.user_id == user_id,
                GenerationQuote.id.in_(quote_ids),
            )
        )
    } if quote_ids else {}
    unlock_quotes = {
        int(row.consumed_ref_id): row
        for row in db.scalars(
            select(GenerationQuote).where(
                GenerationQuote.user_id == user_id,
                GenerationQuote.kind == "asset_unlock",
                GenerationQuote.consumed_ref_type == "asset_unlock",
                GenerationQuote.consumed_ref_id.in_(asset_ids),
            )
        )
        if row.consumed_ref_id is not None
    } if asset_ids else {}
    review_audits: dict[int, AuditLog] = {}
    if gen_ids:
        for row in db.scalars(
            select(AuditLog)
            .where(
                AuditLog.biz_type == "gen_task",
                AuditLog.biz_id.in_(gen_ids),
                AuditLog.action.in_(("refund_review_task", "settle_review_task")),
            )
            .order_by(AuditLog.id)
        ):
            if row.biz_id is not None:
                review_audits[int(row.biz_id)] = row

    for entry in entries:
        metadata = _base_metadata(entry)
        ref = entry["biz_ref"]
        biz_type = entry["biz_type"]
        if biz_type == "gen_task" and ref in gen_tasks:
            task = gen_tasks[ref]
            category = "视频" if task.category == "video" else "图片"
            stage = "成片" if task.stage == "final" else "预览"
            review = review_audits.get(int(task.id))
            review_status = None
            review_reason = None
            review_note = None
            reviewed_at = None
            if task.status == "needs_review":
                review_status = "pending"
                review_reason = task.error or "供应商结果与账务状态等待人工核对"
            elif review is not None:
                review_status = "refunded" if review.action == "refund_review_task" else "settled"
                review_detail = review.detail if isinstance(review.detail, dict) else {}
                audit_note = str(review_detail.get("note") or "").strip()
                if review_status == "refunded":
                    review_note = audit_note or task.error or "人工核对后已退款"
                else:
                    review_note = audit_note or "人工核对后已确认结果并完成结算"
                reviewed_at = review.created_at
            metadata.update({
                "title": f"{category}生成",
                "subtitle": f"{stage}任务 #{task.id}",
                "status": task.status,
                "status_group": _status_group(task.status),
                "related_kind": "generation",
                "related_id": int(task.id),
                **_model_fields(_task_model_snapshot(task), task.model_config_id, models),
                **_quote_fields(quotes.get(int(task.quote_id)) if task.quote_id else None),
                "review_status": review_status,
                "review_reason": review_reason,
                "review_note": review_note,
                "reviewed_at": reviewed_at,
            })
        elif biz_type in {"reverse_operation", "reverse"} and ref in reverse_ops:
            operation = reverse_ops[ref]
            target = "视频" if operation.target == "video" else "图片"
            metadata.update({
                "title": f"{target}反推",
                "subtitle": f"{operation.analysis_focus or 'comprehensive'} · 任务 #{operation.id}",
                "status": operation.status,
                "status_group": _status_group(operation.status),
                "related_kind": "reverse",
                "related_id": int(operation.id),
                **_model_fields(
                    _reverse_model_snapshot(operation),
                    operation.model_config_id,
                    models,
                ),
                **_quote_fields(
                    quotes.get(int(operation.quote_id)) if operation.quote_id else None
                ),
            })
        elif biz_type == "tool_run" and ref in tool_runs:
            tool_run = tool_runs[ref]
            tool = tool_definitions.get(int(tool_run.tool_definition_id))
            workflow_run = workflow_runs.get(int(tool_run.id))
            metadata.update({
                "title": tool.name if tool is not None else "工具工作流",
                "subtitle": (
                    f"工作流运行 #{workflow_run.id} · 工具执行 #{tool_run.id}"
                    if workflow_run is not None
                    else f"工具执行 #{tool_run.id}"
                ),
                "status": tool_run.status,
                "status_group": _status_group(tool_run.status),
                "related_kind": "workflow",
                "related_id": int(workflow_run.id) if workflow_run is not None else int(tool_run.id),
                **_quote_fields(
                    quotes.get(int(tool_run.quote_id)) if tool_run.quote_id else None
                ),
            })
        elif biz_type == "payment" and ref in payments:
            order = payments[ref]
            provider = "微信" if order.provider == "wechat" else "支付宝"
            metadata.update({
                "title": f"充值 {int(order.credits)} 积分",
                "subtitle": f"{provider} · {order.order_no}",
                "status": order.status,
                "status_group": _status_group(order.status),
                "related_kind": "payment",
                "related_id": int(order.id),
            })
        elif biz_type == "unlock" and ref in assets:
            asset = assets[ref]
            task = unlock_tasks.get(int(asset.task_id))
            category = "视频" if asset.type == "video" else "图片"
            metadata.update({
                "title": f"解锁{category}素材",
                "subtitle": f"素材 #{asset.id}",
                "status": "succeeded",
                "status_group": "succeeded",
                "related_kind": "asset",
                "related_id": int(asset.id),
                **_model_fields(
                    _task_model_snapshot(task),
                    task.model_config_id if task else None,
                    models,
                ),
                **_quote_fields(unlock_quotes.get(int(asset.id))),
            })
        elif biz_type == "prompt_optimize":
            proposal = prompt_proposals.get(ref)
            if proposal is not None:
                metrics = proposal.metrics if isinstance(proposal.metrics, dict) else {}
                execution_status = str(metrics.get("execution_status") or "")
                status = proposal.status
                if metadata["status"] == "refunded":
                    status = "refunded"
                elif execution_status == "running" and entry["outstanding_frozen_credits"] > 0:
                    status = "running"
                model_config_id = proposal.optimizer_model_config_id or proposal.target_model_config_id
                model_snapshot = proposal.provenance if isinstance(proposal.provenance, dict) else {}
                metadata.update({
                    "title": (
                        "提示词模型编译"
                        if proposal.optimization_kind == "model_compile"
                        else "提示词优化"
                    ),
                    "subtitle": (
                        f"{_PROMPT_MODE_LABELS.get(proposal.mode, proposal.mode)} · 提案 #{proposal.id}"
                    ),
                    "status": status,
                    "status_group": _status_group(status),
                    "related_kind": "prompt_optimization",
                    "related_id": int(proposal.id),
                    **_model_fields(model_snapshot, model_config_id, models),
                    **_quote_fields(
                        quotes.get(int(proposal.quote_id)) if proposal.quote_id else None
                    ),
                    "review_note": (
                        str(metrics.get("execution_error") or "") or None
                    ) if status == "refunded" else None,
                })
            else:
                match = _MODEL_NOTE_RE.search(str(entry.get("note") or ""))
                model_id = match.group(1) if match else None
                metadata.update({
                    "title": "提示词优化",
                    "subtitle": (
                        "优化失败已退回"
                        if metadata["status"] == "refunded"
                        else "历史提示词优化调用"
                    ),
                    "model_name": model_id,
                    "model_id": model_id,
                })
        elif biz_type == "admin":
            metadata.update({
                "title": "管理员发放",
                "subtitle": entry.get("note") or "账户积分调整",
                "status": "credited",
                "status_group": "credited",
                "related_kind": "admin",
                "related_id": ref,
            })
        entry.update(metadata)


def list_billing_entries(
    db: Session,
    *,
    user_id: int,
    kind: str = "all",
    limit: int = 30,
    cursor: str | None = None,
) -> dict[str, Any]:
    cursor_key = _decode_cursor(cursor)
    balance_delta = func.coalesce(CreditTransaction.balance_delta, CreditTransaction.change, 0)
    frozen_delta = func.coalesce(CreditTransaction.frozen_delta, 0)
    biz_type_expr = func.coalesce(CreditTransaction.biz_type, "unknown")
    # Rows without a business reference cannot be safely assumed to belong to
    # the same operation. Keep each as its own bill instead of merging unrelated
    # grants or prompt calls into one lifetime total.
    group_ref_expr = case(
        (CreditTransaction.biz_ref.is_(None), -CreditTransaction.id),
        else_=CreditTransaction.biz_ref,
    )
    latest_id = func.max(CreditTransaction.id)
    latest_at = func.max(CreditTransaction.created_at)
    filters = [CreditTransaction.user_id == user_id]
    kind_clause = _kind_clause(kind)
    if kind_clause is not None:
        filters.append(kind_clause)

    frozen_amount = case(
        (
            CreditTransaction.type == "freeze",
            case((balance_delta < 0, -balance_delta), else_=frozen_delta),
        ),
        else_=0,
    )
    settled_amount = case(
        (CreditTransaction.type == "settle", func.coalesce(CreditTransaction.real_cost, 0)),
        else_=0,
    )
    returned_amount = case(
        (
            CreditTransaction.type.in_(("settle", "refund")),
            case((balance_delta > 0, balance_delta), else_=0),
        ),
        else_=0,
    )
    settlement_returned = case(
        (
            CreditTransaction.type == "settle",
            case((balance_delta > 0, balance_delta), else_=0),
        ),
        else_=0,
    )
    reservation_refund = case(
        (
            CreditTransaction.type == "refund",
            case((frozen_delta < 0, balance_delta), else_=0),
        ),
        else_=0,
    )
    consumed_refund = case(
        (
            CreditTransaction.type == "refund",
            case((frozen_delta >= 0, balance_delta), else_=0),
        ),
        else_=0,
    )
    reservation_released = case(
        (
            CreditTransaction.type.in_(("settle", "refund")),
            case((frozen_delta < 0, -frozen_delta), else_=0),
        ),
        else_=0,
    )
    direct_consumed = case(
        (
            CreditTransaction.type.in_(("consume", "unlock")),
            case((balance_delta < 0, -balance_delta), else_=0),
        ),
        else_=0,
    )
    credited = case(
        (
            CreditTransaction.type == "grant",
            case((balance_delta > 0, balance_delta), else_=0),
        ),
        else_=0,
    )

    grouped = (
        select(
            biz_type_expr.label("biz_type"),
            CreditTransaction.biz_ref.label("biz_ref"),
            group_ref_expr.label("group_ref"),
            latest_id.label("latest_id"),
            func.min(CreditTransaction.created_at).label("created_at"),
            latest_at.label("updated_at"),
            func.count(CreditTransaction.id).label("transaction_count"),
            func.sum(frozen_amount).label("frozen_credits"),
            func.sum(settled_amount).label("settled_credits"),
            func.sum(returned_amount).label("refunded_credits"),
            func.sum(settlement_returned).label("settlement_returned"),
            func.sum(reservation_refund).label("reservation_refund"),
            func.sum(consumed_refund).label("consumed_refund"),
            func.sum(reservation_released).label("reservation_released"),
            func.sum(direct_consumed).label("direct_consumed"),
            func.sum(credited).label("credited_credits"),
            func.sum(frozen_delta).label("outstanding_frozen"),
            func.sum(balance_delta).label("net_balance_change"),
        )
        .where(*filters)
        .group_by(biz_type_expr, CreditTransaction.biz_ref, group_ref_expr)
    )
    if cursor_key is not None:
        grouped = grouped.having(latest_id < cursor_key[1])
    grouped = grouped.order_by(latest_id.desc()).limit(limit + 1)
    rows = list(db.execute(grouped).mappings())
    has_more = len(rows) > limit
    selected = rows[:limit]

    latest_transaction_ids = [int(row["latest_id"]) for row in selected]
    latest_transactions = {
        int(row.id): row
        for row in db.scalars(
            select(CreditTransaction).where(CreditTransaction.id.in_(latest_transaction_ids))
        )
    } if latest_transaction_ids else {}

    entries: list[dict[str, Any]] = []
    for row in selected:
        latest = latest_transactions[int(row["latest_id"])]
        settled = max(0, int(row["settled_credits"] or 0))
        direct = max(0, int(row["direct_consumed"] or 0))
        consumed_refunded = max(0, int(row["consumed_refund"] or 0))
        net_consumed = max(0, settled + direct - consumed_refunded)
        frozen = max(0, int(row["frozen_credits"] or 0))
        outstanding = max(0, int(row["outstanding_frozen"] or 0))
        reservation_released_credits = max(0, int(row["reservation_released"] or 0))
        credited_credits = max(0, int(row["credited_credits"] or 0))
        net_balance_change = int(row["net_balance_change"] or 0)
        biz_type = str(row["biz_type"] or "unknown")
        biz_ref = int(row["biz_ref"]) if row["biz_ref"] is not None else None
        key_ref = str(biz_ref) if biz_ref is not None else f"tx-{int(row['latest_id'])}"
        entries.append({
            "key": f"{biz_type}:{key_ref}",
            "kind": _entry_kind(biz_type, latest.type),
            "biz_type": biz_type,
            "biz_ref": biz_ref,
            "frozen_credits": frozen,
            "settled_credits": settled,
            "refunded_credits": max(0, int(row["refunded_credits"] or 0)),
            "settlement_returned_credits": max(0, int(row["settlement_returned"] or 0)),
            "reservation_refunded_credits": max(0, int(row["reservation_refund"] or 0)),
            "consumed_refunded_credits": consumed_refunded,
            "outstanding_frozen_credits": outstanding,
            "net_consumed_credits": net_consumed,
            "credited_credits": credited_credits,
            "net_balance_change": net_balance_change,
            "balance_conserved": (
                net_balance_change == credited_credits - net_consumed - outstanding
            ),
            "reservation_conserved": (
                frozen == reservation_released_credits + outstanding
            ),
            "balance_after": int(latest.balance_after or 0),
            "frozen_after": int(latest.frozen_after) if latest.frozen_after is not None else None,
            "transaction_count": int(row["transaction_count"] or 0),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "note": latest.note,
            "latest_transaction_id": int(row["latest_id"]),
        })
    _enrich_entries(db, user_id, entries)
    for entry in entries:
        entry.pop("note", None)
        entry.pop("latest_transaction_id", None)

    count_query = select(func.count()).select_from(
        select(group_ref_expr)
        .where(*filters)
        .group_by(biz_type_expr, CreditTransaction.biz_ref, group_ref_expr)
        .subquery()
    )
    total = int(db.scalar(count_query) or 0)
    return {
        "items": entries,
        "next_cursor": (
            _encode_cursor(selected[-1]["updated_at"], int(selected[-1]["latest_id"]))
            if has_more and selected
            else None
        ),
        "has_more": has_more,
        "total": total,
    }


def _entry_kind(biz_type: str, transaction_type: str) -> str:
    if biz_type == "gen_task":
        return "generation"
    if biz_type in {"reverse_operation", "reverse"}:
        return "reverse"
    if biz_type == "tool_run":
        return "workflow"
    if biz_type == "prompt_optimize":
        return "prompt"
    if biz_type == "unlock":
        return "unlock"
    if biz_type == "payment":
        return "recharge"
    if biz_type == "admin" or transaction_type == "grant":
        return "grant"
    return "other"
