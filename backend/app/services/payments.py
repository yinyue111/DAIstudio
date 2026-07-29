"""Payment order orchestration.

The production invariant is simple: only a server-confirmed paid order grants
credits, and the order transition is idempotent. Provider-specific QR creation
is kept behind this module so the rest of the app does not care whether the
payment came from Alipay or WeChat Pay.
"""
from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urlparse

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from ..config import settings
from ..models import PaymentOrder, User
from . import credits, locks, payment_config
from .config_store import get_bool_setting
from .user_events import publish_user_event

PAYMENT_PACKAGES = payment_config.DEFAULT_PAYMENT_PACKAGES
log = logging.getLogger("payments")

VALID_PROVIDERS = {"alipay", "wechat"}
PENDING = "pending"
PAID = "paid"
CLOSED = "closed"
FAILED = "failed"
REFUNDED = "refunded"
REFUND_PENDING = "pending"
REFUND_SUCCEEDED = "succeeded"
REFUND_FAILED = "failed"
_RECONCILE_LOCK_KEY = "payment:reconcile"
_RECONCILE_CURSOR_SETTING = "payment_reconcile_cursor"


class PaymentError(Exception):
    pass


def _is_local_url(value: str) -> bool:
    parsed = urlparse(str(value or ""))
    host = (parsed.hostname or "").lower()
    return parsed.scheme in {"http", "https"} and host in {"localhost", "127.0.0.1", "::1"}


def public_base_is_local() -> bool:
    return _is_local_url(settings.public_base_url)


def mock_payments_allowed() -> bool:
    return (
        settings.debug
        and settings.payment_mock_enabled
        and _is_local_url(settings.public_base_url)
        and _is_local_url(settings.payment_frontend_base_url)
    )


def list_packages(db: Session, *, enabled_only: bool = True) -> list[dict]:
    payment_config.seed_defaults(db)
    return [
        payment_config.package_to_dict(p)
        for p in payment_config.list_packages(db, enabled_only=enabled_only)
    ]


def public_config(db: Session) -> dict:
    payment_enabled = get_bool_setting(db, "payment_enabled", False)
    status = payment_config.export_public_status(db)
    mock_allowed = mock_payments_allowed()
    providers = []
    if payment_enabled:
        for p in status.get("providers", []):
            provider_mode = p.get("mode") or "mock"
            provider_enabled = bool(p.get("enabled"))
            provider_ready = bool(p.get("ready"))
            if provider_mode == "mock":
                provider_enabled = mock_allowed
                provider_ready = mock_allowed
            providers.append({
                "provider": p["provider"],
                "enabled": provider_enabled,
                "ready": provider_ready,
                "mode": provider_mode,
            })
    return {
        "enabled": payment_enabled,
        "packages": status.get("packages", []) if payment_enabled else [],
        "providers": providers,
    }


def _assert_positive_package(pkg: dict) -> None:
    if int(pkg.get("amount_cents") or 0) <= 0 or int(pkg.get("credits") or 0) <= 0:
        raise PaymentError("充值套餐金额或积分配置非法")


def _assert_positive_order(order: PaymentOrder) -> None:
    if int(order.amount_cents or 0) <= 0 or int(order.credits or 0) <= 0:
        raise PaymentError("支付订单金额或积分非法")


def package_by_id(db: Session, package_id: str) -> dict:
    payment_config.seed_defaults(db)
    row = payment_config.get_enabled_package(db, package_id)
    if row:
        pkg = payment_config.package_to_dict(row)
        _assert_positive_package(pkg)
        return pkg
    raise PaymentError("充值套餐不存在")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _provider_trade_no_conflict_message(exc: IntegrityError) -> str | None:
    detail = str(getattr(exc, "orig", exc))
    lowered = detail.lower()
    if "provider_trade_no" not in lowered and "uq_payment_orders_provider_trade_no" not in lowered:
        return None
    return "支付流水号已入账到其他订单"


def make_order_no(provider: str) -> str:
    return f"R{provider[:1].upper()}{int(time.time())}{secrets.token_hex(5)}"


def _mock_code_url(order_no: str, provider: str, credits_: int) -> str:
    # The frontend turns this into a QR image. It is intentionally local and
    # harmless; DEBUG mock success still requires an authenticated API call.
    base = settings.payment_frontend_base_url.rstrip("/")
    return f"{base}/recharge?mock_order={quote_plus(order_no)}&provider={provider}&credits={credits_}"


def _alipay_configured(cfg: payment_config.ProviderRuntimeConfig) -> bool:
    return bool(
        cfg.public.get("app_id")
        and cfg.secret.get("private_key")
        and cfg.secret.get("public_key")
    )


def _wechat_configured(cfg: payment_config.ProviderRuntimeConfig) -> bool:
    return bool(
        cfg.public.get("appid")
        and cfg.public.get("mchid")
        and cfg.public.get("serial_no")
        and cfg.secret.get("private_key")
        and cfg.secret.get("api_v3_key")
        and cfg.secret.get("platform_cert_pem")
    )



from . import payment_transport as _payment_transport  # noqa: E402
from .payment_transport import (  # noqa: E402, F401, I001
    _alipay_precreate,
    _alipay_query,
    _normalise_pem_text,
    _normalise_private_pem,
    _normalise_public_pem,
    _notify_url,
    _provider_code_url,
    _provider_raw_subset,
    _query_provider_order,
    _rsa_sha256_sign,
    _rsa_sha256_verify,
    _wechat_authorization,
    _wechat_native,
    _wechat_query,
    _wrap_pem_body,
    amount_to_cents,
    ensure_amount_matches,
)


def create_order(db: Session, user: User, provider: str, package_id: str) -> PaymentOrder:
    if not get_bool_setting(db, "payment_enabled", False):
        raise PaymentError("支付充值功能未开启")
    if provider not in VALID_PROVIDERS:
        raise PaymentError("支付渠道非法")
    pkg = package_by_id(db, package_id)
    _assert_positive_package(pkg)
    lock_key = f"payment:create:{user.id}"
    lock_ttl = max(180, int(settings.gateway_timeout_seconds) + 60)
    lock_token = locks.acquire(lock_key, ttl=lock_ttl)
    if not lock_token:
        raise PaymentError("支付订单正在创建,请稍后重试")
    try:
        now = _now()
        db.execute(
            update(PaymentOrder)
            .where(
                PaymentOrder.user_id == user.id,
                PaymentOrder.status == PENDING,
                PaymentOrder.expires_at < now,
            )
            .values(status=CLOSED)
        )
        db.execute(
            update(PaymentOrder)
            .where(
                PaymentOrder.user_id == user.id,
                PaymentOrder.status == PENDING,
                PaymentOrder.code_url.is_(None),
                PaymentOrder.paid_at.is_(None),
            )
            .values(
                status=FAILED,
                raw={"error": "支付二维码未创建完成,订单已自动关闭,请重新下单"},
            )
        )
        pending_count = db.execute(
            select(func.count())
            .select_from(PaymentOrder)
            .where(
                PaymentOrder.user_id == user.id,
                PaymentOrder.status == PENDING,
                PaymentOrder.expires_at >= now,
            )
        ).scalar_one()
        if int(pending_count or 0) >= int(settings.payment_order_pending_limit):
            db.commit()
            raise PaymentError("待支付订单过多,请先完成或等待旧订单过期")
        order = PaymentOrder(
            order_no=make_order_no(provider),
            user_id=user.id,
            provider=provider,
            package_id=pkg["id"],
            amount_cents=int(pkg["amount_cents"]),
            credits=int(pkg["credits"]),
            status=PENDING,
            expires_at=now + timedelta(minutes=settings.payment_order_expire_minutes),
        )
        db.add(order)
        db.commit()
        db.refresh(order)
        try:
            code_url, raw = _provider_code_url(order, db)
        except Exception as e:
            order.status = FAILED
            order.raw = {"error": str(e)[:500]}
            db.commit()
            raise
        order.code_url = code_url
        order.raw = raw
        db.commit()
        db.refresh(order)
        return order
    finally:
        locks.release(lock_key, lock_token)


def user_order(db: Session, order_no: str, user_id: int) -> PaymentOrder | None:
    return db.execute(
        select(PaymentOrder).where(
            PaymentOrder.order_no == order_no,
            PaymentOrder.user_id == user_id,
        )
    ).scalar_one_or_none()


def list_user_orders(
    db: Session,
    user_id: int,
    limit: int = 20,
    *,
    offset: int = 0,
    before_id: int | None = None,
) -> list[PaymentOrder]:
    """List one user's orders, newest first.

    分页与积分账单游标同思路:订单 id 追加只增,`before_id` 传上一页最后一条的
    id 即为稳定游标;`offset` 供无游标场景兜底。
    """
    stmt = select(PaymentOrder).where(PaymentOrder.user_id == user_id)
    if before_id is not None and int(before_id) > 0:
        stmt = stmt.where(PaymentOrder.id < int(before_id))
    orders = list(
        db.execute(
            stmt.order_by(PaymentOrder.id.desc())
            .offset(max(int(offset), 0))
            .limit(min(max(limit, 1), 100))
        ).scalars()
    )
    return [with_display_status(order) for order in orders]


def list_user_invoice_orders(db: Session, user_id: int, limit: int = 100) -> list[PaymentOrder]:
    return list(
        db.execute(
            select(PaymentOrder)
            .where(
                PaymentOrder.user_id == user_id,
                PaymentOrder.invoice_status != "none",
            )
            .order_by(PaymentOrder.id.desc())
            .limit(min(max(limit, 1), 200))
        ).scalars()
    )


def get_order(db: Session, order_no: str) -> PaymentOrder | None:
    return db.execute(
        select(PaymentOrder).where(PaymentOrder.order_no == order_no)
    ).scalar_one_or_none()


def mark_paid(
    db: Session,
    order_no: str,
    *,
    provider: str | None = None,
    provider_trade_no: str | None = None,
    raw: dict | None = None,
    allow_expired: bool = False,
) -> tuple[PaymentOrder, bool]:
    order = db.execute(
        select(PaymentOrder)
        .where(PaymentOrder.order_no == order_no)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if not order:
        raise PaymentError("订单不存在")
    if provider and order.provider != provider:
        raise PaymentError("订单支付渠道不匹配")
    _assert_positive_order(order)
    provider_trade_no = (provider_trade_no or "").strip()
    if order.status == PAID:
        if not provider_trade_no:
            raise PaymentError("支付通知缺少第三方流水号")
        if order.provider_trade_no and order.provider_trade_no != provider_trade_no:
            raise PaymentError("支付流水号与已入账订单不匹配")
        return order, False
    if provider_trade_no:
        existing = db.execute(
            select(PaymentOrder).where(
                PaymentOrder.provider == order.provider,
                PaymentOrder.provider_trade_no == provider_trade_no,
                PaymentOrder.id != order.id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise PaymentError("支付流水号已入账到其他订单")
    expires_at = _aware(order.expires_at)
    now = _now()
    if expires_at and expires_at < now and not allow_expired:
        order.status = CLOSED
        db.commit()
        raise PaymentError("订单已过期")
    payable_statuses = (PENDING,)
    if allow_expired and order.status == CLOSED and not order.paid_at:
        payable_statuses = (PENDING, CLOSED)
    if order.status not in payable_statuses:
        raise PaymentError("订单状态不可入账")
    if not provider_trade_no:
        raise PaymentError("支付通知缺少第三方流水号")
    order.status = PAID
    order.paid_at = _now()
    order.provider_trade_no = provider_trade_no
    if raw is not None:
        order.raw = raw
    credits.grant(
        db,
        order.user_id,
        order.credits,
        note=f"payment {order.provider} {order.order_no}",
        biz_type="payment",
        biz_ref=order.id,
        commit=False,
    )
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        message = _provider_trade_no_conflict_message(e)
        if message:
            raise PaymentError(message) from e
        raise
    db.refresh(order)
    publish_user_event(
        order.user_id,
        "payment_paid",
        {
            "order_no": order.order_no,
            "provider": order.provider,
            "credits": order.credits,
            "amount_cents": order.amount_cents,
            "paid_at": order.paid_at.isoformat() if order.paid_at else None,
        },
    )
    return order, True


def with_display_status(order: PaymentOrder) -> PaymentOrder:
    if order.status == PENDING and _aware(order.expires_at) and _aware(order.expires_at) < _now():
        set_committed_value(order, "status", CLOSED)
    return order



from . import payment_notifications as _payment_notifications  # noqa: E402
from . import payment_reconciliation as _payment_reconciliation  # noqa: E402
from .payment_notifications import (  # noqa: E402, F401
    decrypt_wechat_resource,
    ensure_wechat_merchant_matches,
    ensure_wechat_notify_success,
    verify_alipay_notify,
    wechat_signature_valid,
)
from .payment_reconciliation import (  # noqa: E402, F401
    _apply_provider_query_result,
    _claim_reconcile_state,
    _coerce_reconcile_position,
    _pending_reconcile_orders,
    _persist_reconcile_state,
    _reconcile_high_water,
    reconcile_pending_orders,
)

# --- 管理端订单查询 / 手工补单 ---------------------------------------------

def admin_order_dict(order: PaymentOrder, phone: str | None = None) -> dict:
    order = with_display_status(order)
    return {
        "id": int(order.id),
        "order_no": order.order_no,
        "user_id": int(order.user_id),
        "phone": phone,
        "provider": order.provider,
        "package_id": order.package_id,
        "amount_cents": int(order.amount_cents),
        "credits": int(order.credits),
        "status": order.status,
        "provider_trade_no": order.provider_trade_no,
        "refunded_amount_cents": int(order.refunded_amount_cents or 0),
        "refunded_at": order.refunded_at,
        "invoice_status": order.invoice_status or "none",
        "invoice_type": order.invoice_type,
        "invoice_title": order.invoice_title,
        "invoice_tax_no": order.invoice_tax_no,
        "invoice_email": order.invoice_email,
        "invoice_note": order.invoice_note,
        "invoice_requested_at": order.invoice_requested_at,
        "invoice_issued_at": order.invoice_issued_at,
        "expires_at": order.expires_at,
        "paid_at": order.paid_at,
        "created_at": order.created_at,
    }


def admin_search_orders(
    db: Session,
    *,
    phone: str | None = None,
    order_no: str | None = None,
    provider_trade_no: str | None = None,
    provider: str | None = None,
    status: str | None = None,
    invoice_status: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """客服查单:按手机号/订单号/渠道单号/时间范围过滤,不限 24h 对账窗口。"""
    filters = []
    if phone and phone.strip():
        filters.append(User.phone == phone.strip())
    if order_no and order_no.strip():
        filters.append(PaymentOrder.order_no == order_no.strip())
    if provider_trade_no and provider_trade_no.strip():
        filters.append(PaymentOrder.provider_trade_no == provider_trade_no.strip())
    if provider:
        filters.append(PaymentOrder.provider == provider)
    if status:
        filters.append(PaymentOrder.status == status)
    if invoice_status == "any":
        filters.append(PaymentOrder.invoice_status != "none")
    elif invoice_status:
        filters.append(PaymentOrder.invoice_status == invoice_status)
    if created_from is not None:
        filters.append(PaymentOrder.created_at >= created_from)
    if created_to is not None:
        filters.append(PaymentOrder.created_at <= created_to)
    limit = min(max(int(limit), 1), 100)
    offset = max(int(offset), 0)
    joined = select(PaymentOrder, User.phone).join(User, User.id == PaymentOrder.user_id)
    total = int(
        db.scalar(
            select(func.count())
            .select_from(PaymentOrder)
            .join(User, User.id == PaymentOrder.user_id)
            .where(*filters)
        )
        or 0
    )
    rows = db.execute(
        joined.where(*filters)
        .order_by(PaymentOrder.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return {
        "items": [admin_order_dict(order, user_phone) for order, user_phone in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def admin_sync_order(db: Session, order_no: str) -> tuple[dict, str]:
    """单笔主动向渠道查单并补单。

    复用对账兜底的 `_query_provider_order` + `_apply_provider_query_result`,
    与自动对账保持同一套状态机;渠道确认支付会立即入账积分。
    """
    order = get_order(db, order_no)
    if not order:
        raise PaymentError("订单不存在")
    result = _query_provider_order(db, order)
    outcome = _apply_provider_query_result(db, order, result)
    refreshed = get_order(db, order_no)
    return admin_order_dict(refreshed), outcome


# --- 渠道退款 ---------------------------------------------------------------


from . import payment_refunds as _payment_refunds  # noqa: E402
from .compat_facade import (  # noqa: E402
    install_assignment_forwarding as _install_assignment_forwarding,
)
from .payment_refunds import (  # noqa: E402, F401, I001
    _alipay_refund,
    _alipay_refund_query,
    _provider_refund,
    _provider_refund_query,
    _reclaimed_credits_before,
    _resolve_pending_refunds,
    _wechat_refund,
    _wechat_refund_query,
    list_order_refunds,
    make_refund_no,
    refund_order,
    refunds_enabled,
)

_install_assignment_forwarding(
    __name__,
    (_payment_transport, _payment_reconciliation, _payment_notifications, _payment_refunds),
)
