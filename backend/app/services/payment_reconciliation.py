"""Pending payment reconciliation and cursor/lease state management."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..models import AppSetting, PaymentOrder
from . import locks
from .config_store import get_bool_setting
from .payment_transport import _query_provider_order
from .payments import (
    _RECONCILE_CURSOR_SETTING,
    _RECONCILE_LOCK_KEY,
    CLOSED,
    FAILED,
    PAID,
    PENDING,
    _now,
    log,
    mark_paid,
)


def _apply_provider_query_result(db: Session, order: PaymentOrder, result: dict) -> str:
    """Apply one verified provider-query result to the local order.

    对账兜底与管理端手工补单共用这一段核心逻辑,避免两份状态机。
    返回值:'paid' 已补单入账 / 'closed' / 'failed' 已同步终态 /
    'skipped' 渠道侧未支付或未知,不改本地 / 'noop' 重复入账或并发竞争,无变化。
    """
    status = str(result.get("status") or "unknown")
    raw = {
        "verified_provider_query": True,
        "provider": order.provider,
        "status": status,
        "query": result.get("raw") or {},
    }
    if status == PAID:
        _paid, credited = mark_paid(
            db,
            order.order_no,
            provider=order.provider,
            provider_trade_no=result.get("provider_trade_no"),
            raw=raw,
            allow_expired=True,
        )
        return PAID if credited else "noop"
    if status in {CLOSED, FAILED} and order.status == PENDING:
        locked = db.execute(
            select(PaymentOrder)
            .where(PaymentOrder.id == order.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one()
        if locked.status == PENDING and locked.paid_at is None:
            locked.status = status
            locked.provider_trade_no = result.get("provider_trade_no") or locked.provider_trade_no
            locked.raw = raw
            db.commit()
            return status
        db.rollback()
        return "noop"
    return "skipped"


def _pending_reconcile_orders(
    db: Session,
    *,
    cutoff: datetime,
    limit: int,
    cursor: int,
    high_water: int,
    exclude_ids: set[int] | None = None,
) -> list[PaymentOrder]:
    filters = (
        PaymentOrder.status.in_((PENDING, CLOSED)),
        PaymentOrder.paid_at.is_(None),
        PaymentOrder.created_at >= cutoff,
    )
    id_filters = [
        PaymentOrder.id > cursor,
        PaymentOrder.id <= high_water,
    ]
    if exclude_ids:
        id_filters.append(PaymentOrder.id.not_in(exclude_ids))
    orders = list(
        db.execute(
            select(PaymentOrder)
            .where(*filters, *id_filters)
            .order_by(PaymentOrder.id.asc())
            .limit(limit)
        ).scalars()
    )
    return orders


def _reconcile_high_water(db: Session, *, cutoff: datetime) -> int:
    return int(
        db.execute(
            select(func.max(PaymentOrder.id)).where(
                PaymentOrder.status.in_((PENDING, CLOSED)),
                PaymentOrder.paid_at.is_(None),
                PaymentOrder.created_at >= cutoff,
            )
        ).scalar_one_or_none()
        or 0
    )


def _coerce_reconcile_position(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _claim_reconcile_state(
    db: Session,
    *,
    owner: str,
    lock_ttl: int,
) -> tuple[int, int] | None:
    row = db.execute(
        select(AppSetting)
        .where(AppSetting.key == _RECONCILE_CURSOR_SETTING)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    observed_value = dict(row.value or {}) if row is not None else None
    value = observed_value or {}
    cursor = _coerce_reconcile_position(value.get("v"))
    high_water = _coerce_reconcile_position(value.get("high_water"))
    claimed_value = {
        "v": cursor,
        "high_water": high_water,
        "owner": owner,
    }
    if not locks.refresh(_RECONCILE_LOCK_KEY, owner, lock_ttl):
        db.rollback()
        return None
    if row is None:
        db.add(AppSetting(key=_RECONCILE_CURSOR_SETTING, value=claimed_value))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return None
    else:
        result = db.execute(
            update(AppSetting)
            .where(
                AppSetting.key == _RECONCILE_CURSOR_SETTING,
                AppSetting.value == observed_value,
            )
            .values(value=claimed_value)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            db.rollback()
            return None
        db.commit()
    if not locks.refresh(_RECONCILE_LOCK_KEY, owner, lock_ttl):
        return None
    return cursor, high_water


def _persist_reconcile_state(
    db: Session,
    *,
    owner: str,
    cursor: int,
    high_water: int,
) -> bool:
    result = db.execute(
        update(AppSetting)
        .where(
            AppSetting.key == _RECONCILE_CURSOR_SETTING,
            AppSetting.value["owner"].as_string() == owner,
        )
        .values(value={
            "v": _coerce_reconcile_position(cursor),
            "high_water": _coerce_reconcile_position(high_water),
            "owner": owner,
        })
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        return False
    db.commit()
    return True


def reconcile_pending_orders(db: Session) -> dict:
    """Query live providers for unpaid local orders and repair callback gaps.

    Provider notifications remain the primary path. This job is a backstop for
    missed callbacks, late provider success after local expiry, and provider-
    confirmed terminal failures. Unknown/provider-pending results never mutate
    local state.
    """
    stats = {
        "checked": 0,
        "paid": 0,
        "closed": 0,
        "failed": 0,
        "skipped": 0,
        "errors": 0,
        "enabled": bool(settings.payment_reconcile_enabled),
    }
    if not settings.payment_reconcile_enabled or not get_bool_setting(db, "payment_enabled", False):
        return stats
    lock_ttl = max(180, int(settings.gateway_timeout_seconds) + 60)
    lock_token = locks.acquire(_RECONCILE_LOCK_KEY, ttl=lock_ttl)
    if not lock_token:
        return stats
    lookback = max(1, int(settings.payment_reconcile_lookback_hours))
    limit = max(1, min(int(settings.payment_reconcile_max_orders), 500))
    cutoff = _now() - timedelta(hours=lookback)
    try:
        try:
            claimed_state = _claim_reconcile_state(
                db,
                owner=lock_token,
                lock_ttl=lock_ttl,
            )
        except Exception:  # noqa: BLE001
            db.rollback()
            stats["errors"] += 1
            log.warning("failed to claim payment reconcile cursor", exc_info=True)
            return stats
        if claimed_state is None:
            stats["errors"] += 1
            log.warning("payment reconcile lock lost or state changed during cursor claim")
            return stats
        cursor, high_water = claimed_state
        if cursor >= high_water:
            cursor = 0
            high_water = _reconcile_high_water(db, cutoff=cutoff)
        orders = _pending_reconcile_orders(
            db,
            cutoff=cutoff,
            limit=limit,
            cursor=cursor,
            high_water=high_water,
        )
        wrapped = False
        if cursor > 0 and len(orders) < limit:
            high_water = _reconcile_high_water(db, cutoff=cutoff)
            wrapped = True
            orders.extend(
                _pending_reconcile_orders(
                    db,
                    cutoff=cutoff,
                    limit=limit - len(orders),
                    cursor=0,
                    high_water=high_water,
                    exclude_ids={order.id for order in orders},
                )
            )
        processed_ids = []
        for order in orders:
            if not locks.refresh(_RECONCILE_LOCK_KEY, lock_token, lock_ttl):
                log.warning("payment reconcile lock lost before order_no=%s", order.order_no)
                break
            try:
                result = _query_provider_order(db, order)
                stats["checked"] += 1
                outcome = _apply_provider_query_result(db, order, result)
                if outcome == "skipped":
                    stats["skipped"] += 1
                elif outcome in {PAID, CLOSED, FAILED}:
                    stats[outcome] += 1
            except Exception as e:  # noqa: BLE001
                db.rollback()
                stats["errors"] += 1
                log.warning(
                    "payment reconcile failed order_no=%s provider=%s error=%s",
                    getattr(order, "order_no", None),
                    getattr(order, "provider", None),
                    str(e)[:200],
                )
            finally:
                processed_ids.append(order.id)
        next_cursor = 0
        if high_water:
            if wrapped and processed_ids:
                next_cursor = processed_ids[-1]
            elif len(orders) < limit:
                next_cursor = high_water
            elif processed_ids:
                next_cursor = processed_ids[-1]
        if high_water:
            if not locks.refresh(_RECONCILE_LOCK_KEY, lock_token, lock_ttl):
                db.rollback()
                stats["errors"] += 1
                log.warning(
                    "payment reconcile lock lost before cursor persist cursor=%s",
                    next_cursor,
                )
            else:
                try:
                    persisted = _persist_reconcile_state(
                        db,
                        owner=lock_token,
                        cursor=next_cursor,
                        high_water=high_water,
                    )
                    if not persisted:
                        stats["errors"] += 1
                        log.warning(
                            "payment reconcile cursor fence rejected owner cursor=%s",
                            next_cursor,
                        )
                except Exception:  # noqa: BLE001
                    db.rollback()
                    stats["errors"] += 1
                    log.warning("failed to persist payment reconcile cursor", exc_info=True)
        return stats
    finally:
        locks.release(_RECONCILE_LOCK_KEY, lock_token)
