"""Recharge credits via QR-code payments."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_client_ip, get_current_user
from ..models import User
from ..schemas import PaymentCreateIn, PaymentOrderOut, PaymentPackageOut
from ..services import audit, payments

router = APIRouter(prefix="/api/payments", tags=["payments"])


@router.get("/packages", response_model=list[PaymentPackageOut])
def packages(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return payments.list_packages(db, enabled_only=True)


@router.get("/config")
def payment_config(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return payments.public_config(db)


@router.post("/orders", response_model=PaymentOrderOut)
def create_order(body: PaymentCreateIn, request: Request,
                 db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    try:
        order = payments.create_order(db, user, body.provider, body.package_id)
    except payments.PaymentError as e:
        raise HTTPException(400, str(e))
    audit.log(db, user_id=user.id, action="create_payment_order",
              biz_type="payment", biz_id=order.id, ip=get_client_ip(request),
              detail={"provider": order.provider, "amount_cents": order.amount_cents,
                      "credits": order.credits})
    return order


@router.get("/orders", response_model=list[PaymentOrderOut])
def my_orders(limit: int = 20, db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    return payments.list_user_orders(db, user.id, limit=limit)


@router.get("/orders/{order_no}", response_model=PaymentOrderOut)
def get_order(order_no: str, db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    order = payments.user_order(db, order_no, user.id)
    if not order:
        raise HTTPException(404, "订单不存在")
    return payments.close_expired(order, db)


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
    form = dict(await request.form())
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
            raw={"provider": "alipay", "notify": form},
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
    body = await request.body()
    if not payments.wechat_signature_valid(db, dict(request.headers), body):
        audit.log(db, user_id=None, action="payment_notify_failed",
                  biz_type="payment", ip=get_client_ip(request),
                  detail={"provider": "wechat", "error": "invalid signature"})
        raise HTTPException(401, "invalid signature")
    try:
        payload = await request.json()
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
            raw={"provider": "wechat", "notify": payload},
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
