"""Admin payment package, provider configuration and order operation endpoints."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from ..config import settings as app_config
from ..db import get_db
from ..deps import get_client_ip, require_admin
from ..models import User
from ..schemas import (
    PaymentPackageDisableIn,
    PaymentPackageIn,
    PaymentPackageOut,
    PaymentProviderConfigIn,
    PaymentProviderConfigOut,
)
from ..schemas.billing import (
    AdminInvoiceUpdateIn,
    AdminPaymentOrderPageOut,
    AdminPaymentOrderSyncOut,
    PaymentRefundIn,
    PaymentRefundOut,
)
from ..services import audit, payment_config, payments
from .admin_helpers import page as _page
from .admin_helpers import parse_date as _parse_date
from .admin_helpers import payment_provider_audit_snapshot as _payment_provider_audit_snapshot

router = APIRouter()


@router.get("/payments/packages", response_model=list[PaymentPackageOut])
def admin_payment_packages(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    payment_config.seed_defaults(db)
    return [
        payment_config.package_to_dict(p)
        for p in payment_config.list_packages(db, enabled_only=False)
    ]


@router.post("/payments/packages", response_model=PaymentPackageOut)
def admin_upsert_payment_package(
    body: PaymentPackageIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    request: Request = None,
):
    row = payment_config.upsert_package(db, body.model_dump())
    audit.log(
        db,
        user_id=admin.id,
        action="upsert_payment_package",
        biz_type="payment",
        ip=get_client_ip(request) if request else None,
        detail=payment_config.package_to_dict(row),
    )
    return payment_config.package_to_dict(row)


@router.delete("/payments/packages/{package_id}")
def admin_disable_payment_package(
    package_id: str,
    body: PaymentPackageDisableIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    request: Request = None,
):
    try:
        payment_config.delete_package(db, package_id)
    except payment_config.PaymentConfigError as e:
        raise HTTPException(404, str(e)) from e
    audit.log(
        db,
        user_id=admin.id,
        action="disable_payment_package",
        biz_type="payment",
        ip=get_client_ip(request) if request else None,
        detail={"package_id": package_id},
    )
    return {"ok": True}


@router.get("/payments/providers", response_model=list[PaymentProviderConfigOut])
def admin_payment_providers(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    return payment_config.list_providers_with_runtime(db)


@router.put("/payments/providers/{provider}", response_model=PaymentProviderConfigOut)
def admin_save_payment_provider(
    provider: str,
    body: PaymentProviderConfigIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    request: Request = None,
):
    if body.provider != provider:
        raise HTTPException(400, "路径支付渠道与请求体不一致")
    if body.mode == "mock" and not app_config.debug and body.enabled:
        raise HTTPException(400, "生产环境不允许启用 mock 支付")
    before = _payment_provider_audit_snapshot(payment_config.get_provider(db, provider))
    try:
        row = payment_config.save_provider(
            db,
            provider=provider,
            enabled=body.enabled,
            mode=body.mode,
            public_config=body.public_config,
            secret_config=body.secret_config,
        )
    except payment_config.PaymentConfigError as e:
        raise HTTPException(400, str(e)) from e
    audit.log(
        db,
        user_id=admin.id,
        action="update_payment_provider",
        biz_type="payment",
        ip=get_client_ip(request) if request else None,
        detail={
            "provider": provider,
            "before": before,
            "after": _payment_provider_audit_snapshot(row),
            "secret_keys_changed": sorted(
                key
                for key, value in (body.secret_config or {}).items()
                if key in payment_config.SECRET_FIELDS.get(provider, set())
                and str(value or "").strip()
                and str(value).strip() not in {"__keep__", "已配置"}
            ),
        },
    )
    return payment_config.provider_out(row)


@router.get("/payments/config")
def admin_payment_config(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    return payment_config.export_public_status(db)


# --- 订单查询 / 手工补单 / 退款 / 开票 ---


@router.get("/payments/orders", response_model=AdminPaymentOrderPageOut)
def admin_search_payment_orders(
    phone: str | None = Query(default=None, max_length=32),
    order_no: str | None = Query(default=None, max_length=64),
    provider_trade_no: str | None = Query(default=None, max_length=128),
    provider: str | None = Query(default=None, pattern="^(alipay|wechat)$"),
    status: str | None = Query(default=None, pattern="^(pending|paid|closed|failed|refunded)$"),
    invoice_status: str | None = Query(default=None, pattern="^(none|requested|issued|rejected)$"),
    created_from: str | None = Query(default=None),
    created_to: str | None = Query(default=None),
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """管理端订单查询:按手机号/订单号/渠道单号/时间范围过滤,带分页。"""
    limit, offset = _page(limit, offset, 100)
    return payments.admin_search_orders(
        db,
        phone=phone,
        order_no=order_no,
        provider_trade_no=provider_trade_no,
        provider=provider,
        status=status,
        invoice_status=invoice_status,
        created_from=_parse_date(created_from),
        created_to=_parse_date(created_to, end_of_day=True),
        limit=limit,
        offset=offset,
    )


@router.post("/payments/orders/{order_no}/sync", response_model=AdminPaymentOrderSyncOut)
def admin_sync_payment_order(
    order_no: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    request: Request = None,
):
    """单笔主动向渠道查单并补单(复用自动对账的核心逻辑)。"""
    try:
        order, outcome = payments.admin_sync_order(db, order_no)
    except payments.PaymentError as e:
        message = str(e)
        raise HTTPException(404 if message == "订单不存在" else 400, message) from e
    audit.log(
        db,
        user_id=admin.id,
        action="admin_sync_payment_order",
        biz_type="payment",
        biz_id=order["id"],
        ip=get_client_ip(request) if request else None,
        detail={"order_no": order_no, "outcome": outcome, "status": order["status"]},
    )
    return {"order": order, "outcome": outcome}


@router.post("/payments/orders/{order_no}/refund", response_model=PaymentRefundOut)
def admin_refund_payment_order(
    order_no: str,
    body: PaymentRefundIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    request: Request = None,
):
    """渠道退款(支持部分退款):先扣回积分,再调渠道,失败自动回滚积分。"""
    try:
        refund = payments.refund_order(
            db,
            order_no,
            operator_id=admin.id,
            reason=body.reason,
            amount_cents=body.amount_cents,
        )
    except payments.PaymentError as e:
        message = str(e)
        audit.log(
            db,
            user_id=admin.id,
            action="admin_refund_payment_order_failed",
            biz_type="payment",
            ip=get_client_ip(request) if request else None,
            detail={"order_no": order_no, "amount_cents": body.amount_cents,
                    "reason": body.reason, "error": message[:300]},
        )
        raise HTTPException(404 if message == "订单不存在" else 400, message) from e
    audit.log(
        db,
        user_id=admin.id,
        action="admin_refund_payment_order",
        biz_type="payment",
        biz_id=refund.order_id,
        ip=get_client_ip(request) if request else None,
        detail={
            "order_no": order_no,
            "refund_no": refund.refund_no,
            "amount_cents": int(refund.amount_cents),
            "credits_reclaimed": int(refund.credits_reclaimed or 0),
            "reason": body.reason,
        },
    )
    return refund


@router.get("/payments/orders/{order_no}/refunds", response_model=list[PaymentRefundOut])
def admin_list_payment_order_refunds(
    order_no: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    order = payments.get_order(db, order_no)
    if not order:
        raise HTTPException(404, "订单不存在")
    return payments.list_order_refunds(db, order.id)


@router.get("/payments/invoices", response_model=AdminPaymentOrderPageOut)
def admin_list_payment_invoices(
    status: str = Query(default="requested", pattern="^(requested|issued|rejected|all)$"),
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """管理端查看开票申请。status=all 表示全部已申请过开票的订单。"""
    limit, offset = _page(limit, offset, 100)
    return payments.admin_search_orders(
        db,
        invoice_status="any" if status == "all" else status,
        limit=limit,
        offset=offset,
    )


@router.put("/payments/invoices/{order_no}", response_model=AdminPaymentOrderSyncOut)
def admin_update_payment_invoice(
    order_no: str,
    body: AdminInvoiceUpdateIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    request: Request = None,
):
    """管理端处理开票申请:标记已开票或驳回。"""
    order = payments.get_order(db, order_no)
    if not order:
        raise HTTPException(404, "订单不存在")
    if order.invoice_status not in ("requested", "issued", "rejected"):
        raise HTTPException(400, "该订单尚未申请开票")
    order.invoice_status = body.status
    if body.note is not None and body.note.strip():
        order.invoice_note = body.note.strip()
    order.invoice_issued_at = (
        datetime.now(timezone.utc) if body.status == "issued" else order.invoice_issued_at
    )
    db.commit()
    audit.log(
        db,
        user_id=admin.id,
        action="admin_update_payment_invoice",
        biz_type="payment",
        biz_id=order.id,
        ip=get_client_ip(request) if request else None,
        detail={"order_no": order_no, "invoice_status": body.status, "note": body.note},
    )
    return {"order": payments.admin_order_dict(order), "outcome": body.status}
