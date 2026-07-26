"""Recharge credits via QR-code payments."""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import get_client_ip, get_current_user
from ..models import CreditTransaction, User
from ..schemas import PaymentCreateIn, PaymentOrderOut, PaymentPackageOut
from ..schemas.billing import PaymentInvoiceOrderOut, PaymentInvoiceRequestIn
from ..services import audit, payments
from ..services.config_store import get_bool_setting
from ..services.product_edition import is_launch_lite
from ..services.rate_limit import incr_window
from ..services.request_limits import read_limited_body
from .admin_helpers import csv_cell as _csv_cell

router = APIRouter(prefix="/api/payments", tags=["payments"])
_ORDER_RATE_WINDOW_SECONDS = 3600


def _compact_alipay_notify(form: dict) -> dict:
    return {
        k: form.get(k)
        for k in (
            "app_id",
            "seller_id",
            "out_trade_no",
            "trade_no",
            "trade_status",
            "total_amount",
            "receipt_amount",
            "gmt_payment",
            "notify_time",
        )
        if form.get(k) is not None
    }


def _compact_wechat_notify(payload: dict, resource: dict) -> dict:
    amount = resource.get("amount") if isinstance(resource.get("amount"), dict) else {}
    return {
        "id": payload.get("id"),
        "event_type": payload.get("event_type"),
        "out_trade_no": resource.get("out_trade_no") or payload.get("out_trade_no"),
        "transaction_id": resource.get("transaction_id") or payload.get("transaction_id"),
        "trade_state": resource.get("trade_state"),
        "success_time": resource.get("success_time"),
        "mchid": resource.get("mchid"),
        "appid": resource.get("appid"),
        "amount": {"total": amount.get("total"), "currency": amount.get("currency")},
    }


def _rate_limit_order(user_id: int, ip: str) -> None:
    for key, limit in (
        (f"payment:order:user:{user_id}", int(settings.payment_order_rate_limit_per_hour)),
        (f"payment:order:ip:{ip}", int(settings.payment_order_rate_limit_per_hour) * 3),
    ):
        n = incr_window(key, _ORDER_RATE_WINDOW_SECONDS)
        if n > limit:
            raise HTTPException(429, "支付下单过于频繁,请稍后再试")


@router.get("/packages", response_model=list[PaymentPackageOut])
def packages(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    if is_launch_lite() or not get_bool_setting(db, "payment_enabled", False):
        return []
    return payments.list_packages(db, enabled_only=True)


@router.get("/config")
def payment_config(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    if is_launch_lite():
        return {"enabled": False, "packages": [], "providers": []}
    return payments.public_config(db)


@router.post("/orders", response_model=PaymentOrderOut)
def create_order(body: PaymentCreateIn, request: Request,
                 db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    ip = get_client_ip(request)
    if is_launch_lite() or not get_bool_setting(db, "payment_enabled", False):
        raise HTTPException(400, "支付充值功能未开启")
    _rate_limit_order(user.id, ip)
    try:
        order = payments.create_order(db, user, body.provider, body.package_id)
    except payments.PaymentError as e:
        raise HTTPException(400, str(e))
    audit.log(db, user_id=user.id, action="create_payment_order",
              biz_type="payment", biz_id=order.id, ip=ip,
              detail={"provider": order.provider, "amount_cents": order.amount_cents,
                      "credits": order.credits})
    return order


@router.get("/orders", response_model=list[PaymentOrderOut])
def my_orders(limit: int = Query(default=20, ge=1, le=100),
              offset: int = Query(default=0, ge=0),
              cursor: int | None = Query(default=None, ge=1),
              db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    """充值订单列表,新单在前。

    分页与积分账单同思路:`cursor` 传上一页最后一条订单的 id(只取更早的订单,
    翻页期间新订单不会造成错位);`offset` 供无游标场景兜底。
    """
    return payments.list_user_orders(db, user.id, limit=limit, offset=offset, before_id=cursor)


@router.get("/billing/export")
def export_my_billing_csv(db: Session = Depends(get_db),
                          user: User = Depends(get_current_user)):
    """导出当前用户积分账单 CSV(充值、冻结、结算、退款等全部流水)。"""
    rows = list(
        db.execute(
            select(CreditTransaction)
            .where(CreditTransaction.user_id == user.id)
            .order_by(CreditTransaction.id.desc())
            .limit(10000)
        ).scalars()
    )

    def iter_csv():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([
            "时间", "类型", "余额变动", "冻结变动", "变动后余额", "变动后冻结",
            "业务类型", "业务编号", "备注",
        ])
        for tx in rows:
            created = tx.created_at
            if created is not None and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            writer.writerow([
                _csv_cell(created.isoformat() if created else ""),
                _csv_cell(tx.type),
                _csv_cell(int(tx.balance_delta if tx.balance_delta is not None else tx.change or 0)),
                _csv_cell(int(tx.frozen_delta or 0)),
                _csv_cell(int(tx.balance_after or 0)),
                _csv_cell(int(tx.frozen_after) if tx.frozen_after is not None else ""),
                _csv_cell(tx.biz_type or ""),
                _csv_cell(int(tx.biz_ref) if tx.biz_ref is not None else ""),
                _csv_cell(tx.note or ""),
            ])
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    date_tag = datetime.now(timezone.utc).strftime("%Y%m%d")
    return StreamingResponse(
        iter_csv(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename=credit_bill_{date_tag}.csv",
        },
    )


@router.get("/invoices", response_model=list[PaymentInvoiceOrderOut])
def my_invoices(db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """当前用户的开票申请记录(按订单维度)。"""
    return payments.list_user_invoice_orders(db, user.id)


@router.post("/orders/{order_no}/invoice", response_model=PaymentInvoiceOrderOut)
def request_invoice(order_no: str, body: PaymentInvoiceRequestIn, request: Request,
                    db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """对已支付订单发起开票申请。"""
    order = payments.user_order(db, order_no, user.id)
    if not order:
        raise HTTPException(404, "订单不存在")
    if order.status != payments.PAID or not order.paid_at:
        raise HTTPException(400, "仅已支付订单可以申请开票")
    if int(order.refunded_amount_cents or 0) > 0:
        raise HTTPException(400, "订单存在退款,暂不支持开票,请联系客服")
    if order.invoice_status in ("requested", "issued"):
        raise HTTPException(400, "该订单已申请开票,请勿重复提交")
    if body.invoice_type == "company" and not body.tax_no:
        raise HTTPException(400, "企业抬头必须填写税号")
    order.invoice_status = "requested"
    order.invoice_type = body.invoice_type
    order.invoice_title = body.title
    order.invoice_tax_no = body.tax_no
    order.invoice_email = (body.email or "").strip() or None
    order.invoice_note = (body.note or "").strip() or None
    order.invoice_requested_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(order)
    audit.log(db, user_id=user.id, action="request_payment_invoice",
              biz_type="payment", biz_id=order.id, ip=get_client_ip(request),
              detail={"order_no": order.order_no, "invoice_type": body.invoice_type,
                      "title": body.title})
    return order


@router.get("/orders/{order_no}", response_model=PaymentOrderOut)
def get_order(order_no: str, db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    order = payments.user_order(db, order_no, user.id)
    if not order:
        raise HTTPException(404, "订单不存在")
    return payments.with_display_status(order)


@router.post("/orders/{order_no}/mock-pay", response_model=PaymentOrderOut)
def mock_pay(order_no: str, request: Request, db: Session = Depends(get_db),
             user: User = Depends(get_current_user)):
    if not payments.mock_payments_allowed():
        raise HTTPException(404, "not found")
    order = payments.user_order(db, order_no, user.id)
    if not order:
        raise HTTPException(404, "订单不存在")
    try:
        paid, credited = payments.mark_paid(db, order_no, provider_trade_no=f"mock_{order_no}",
                                            provider=order.provider, raw={"mock_paid": True})
    except payments.PaymentError as e:
        raise HTTPException(400, str(e))
    audit.log(db, user_id=user.id, action="mock_payment_paid",
              biz_type="payment", biz_id=paid.id, ip=get_client_ip(request),
              detail={"credited": credited})
    return paid


@router.post("/alipay/notify")
async def alipay_notify(request: Request, db: Session = Depends(get_db)):
    body = await read_limited_body(request, settings.payment_notify_max_body_bytes, "支付回调体过大")
    form = dict(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))
    try:
        order_no, total_amount = payments.verify_alipay_notify(db, form)
        order = payments.get_order(db, order_no)
        if not order:
            raise payments.PaymentError("订单不存在")
        payments.ensure_amount_matches(total_amount, order.amount_cents, "支付宝")
        order, _credited = payments.mark_paid(
            db,
            order_no,
            provider="alipay",
            provider_trade_no=form.get("trade_no"),
            raw={"provider": "alipay", "notify": _compact_alipay_notify(form)},
            allow_expired=True,
        )
        audit.log(db, user_id=order.user_id, action="payment_notify_paid",
                  biz_type="payment", biz_id=order.id, ip=get_client_ip(request),
                  detail={"provider": "alipay", "order_no": order_no, "credited": _credited})
    except payments.PaymentError as e:
        audit.log(db, user_id=None, action="payment_notify_failed",
                  biz_type="payment", ip=get_client_ip(request),
                  detail={"provider": "alipay", "order_no": form.get("out_trade_no"),
                          "error": str(e)[:300]})
        return PlainTextResponse("fail")
    return PlainTextResponse("success")


@router.post("/wechat/notify")
async def wechat_notify(request: Request, db: Session = Depends(get_db)):
    body = await read_limited_body(request, settings.payment_notify_max_body_bytes, "支付回调体过大")
    if not payments.wechat_signature_valid(db, dict(request.headers), body):
        audit.log(db, user_id=None, action="payment_notify_failed",
                  biz_type="payment", ip=get_client_ip(request),
                  detail={"provider": "wechat", "error": "invalid signature"})
        raise HTTPException(401, "invalid signature")
    try:
        payload = json.loads(body.decode("utf-8"))
        resource = payments.decrypt_wechat_resource(db, payload.get("resource") or {})
        order_no = resource.get("out_trade_no") or payload.get("out_trade_no")
        transaction_id = resource.get("transaction_id") or payload.get("transaction_id")
        if not order_no:
            raise payments.PaymentError("微信通知缺少订单号")
        order = payments.get_order(db, order_no)
        if not order:
            raise payments.PaymentError("订单不存在")
        if order.provider != "wechat":
            raise payments.PaymentError("订单支付渠道不匹配")
        payments.ensure_wechat_merchant_matches(db, resource)
        payments.ensure_wechat_notify_success(payload, resource)
        total = (resource.get("amount") or {}).get("total") if isinstance(resource, dict) else None
        if total is None:
            raise payments.PaymentError("微信通知缺少金额")
        if int(total) != int(order.amount_cents):
            raise payments.PaymentError("微信通知金额不匹配")
        order, _credited = payments.mark_paid(
            db,
            order_no,
            provider="wechat",
            provider_trade_no=transaction_id,
            raw={"provider": "wechat", "notify": _compact_wechat_notify(payload, resource)},
            allow_expired=True,
        )
        audit.log(db, user_id=order.user_id, action="payment_notify_paid",
                  biz_type="payment", biz_id=order.id, ip=get_client_ip(request),
                  detail={"provider": "wechat", "order_no": order_no, "credited": _credited})
    except payments.PaymentError as e:
        audit.log(db, user_id=None, action="payment_notify_failed",
                  biz_type="payment", ip=get_client_ip(request),
                  detail={"provider": "wechat", "order_no": locals().get("order_no"),
                          "error": str(e)[:300]})
        raise HTTPException(400, str(e))
    return {"code": "SUCCESS", "message": "成功"}
