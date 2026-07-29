"""Provider refund transport and refund lifecycle orchestration."""
from __future__ import annotations

import json
import secrets
import time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import PaymentOrder
from ..models.payment import PaymentRefund
from . import credits, locks, payment_config
from .payment_transport import (
    _provider_raw_subset,
    _rsa_sha256_sign,
    _wechat_authorization,
)
from .payments import (
    PAID,
    REFUND_FAILED,
    REFUND_PENDING,
    REFUND_SUCCEEDED,
    REFUNDED,
    PaymentError,
    _alipay_configured,
    _now,
    _wechat_configured,
    mock_payments_allowed,
)
from .ssrf import pinned_client
from .user_events import publish_user_event


def refunds_enabled() -> bool:
    # 渠道退款未在真实商户环境联调过,默认关闭;需显式配置
    # PAYMENT_REFUND_ENABLED=true 才允许发起。getattr 兜底以兼容尚未
    # 添加该配置项的部署。
    return bool(getattr(settings, "payment_refund_enabled", False))


def make_refund_no(provider: str) -> str:
    return f"RF{provider[:1].upper()}{int(time.time())}{secrets.token_hex(5)}"


def _alipay_refund(
    order: PaymentOrder,
    refund: PaymentRefund,
    cfg: payment_config.ProviderRuntimeConfig,
) -> dict:
    biz_content = {
        "out_trade_no": order.order_no,
        "refund_amount": f"{refund.amount_cents / 100:.2f}",
        "out_request_no": refund.refund_no,
        "refund_reason": (refund.reason or "")[:80] or "订单退款",
    }
    params = {
        "app_id": cfg.public.get("app_id"),
        "method": "alipay.trade.refund",
        "format": "JSON",
        "charset": "utf-8",
        "sign_type": "RSA2",
        "timestamp": _now().strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0",
        "biz_content": json.dumps(biz_content, ensure_ascii=False, separators=(",", ":")),
    }
    sign_src = "&".join(f"{k}={params[k]}" for k in sorted(params))
    params["sign"] = _rsa_sha256_sign(sign_src, cfg.secret.get("private_key") or "")
    url = cfg.public.get("gateway_url") or settings.alipay_gateway_url
    with pinned_client(url, timeout=settings.gateway_timeout_seconds, follow_redirects=False) as client:
        resp = client.post(url, data=params)
    if resp.status_code >= 400:
        raise PaymentError(f"支付宝退款失败:{resp.status_code}")
    data = resp.json()
    body = data.get("alipay_trade_refund_response") or {}
    if str(body.get("code") or "") != "10000":
        raise PaymentError(body.get("sub_msg") or body.get("msg") or "支付宝退款失败")
    if body.get("out_trade_no") and str(body.get("out_trade_no")) != order.order_no:
        raise PaymentError("支付宝退款订单号不匹配")
    raw = _provider_raw_subset(
        body,
        ("out_trade_no", "trade_no", "buyer_logon_id", "fund_change", "refund_fee", "gmt_refund_pay"),
    )
    return {"provider_refund_no": body.get("trade_no"), "raw": raw}


def _wechat_refund(
    order: PaymentOrder,
    refund: PaymentRefund,
    cfg: payment_config.ProviderRuntimeConfig,
) -> dict:
    path = "/v3/refund/domestic/refunds"
    payload = {
        "out_trade_no": order.order_no,
        "out_refund_no": refund.refund_no,
        "reason": (refund.reason or "")[:80] or "订单退款",
        "amount": {
            "refund": int(refund.amount_cents),
            "total": int(order.amount_cents),
            "currency": "CNY",
        },
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    headers = {
        "Authorization": _wechat_authorization("POST", path, body, cfg),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    url = (cfg.public.get("gateway_url") or settings.wechat_pay_gateway_url).rstrip("/") + path
    with pinned_client(url, timeout=settings.gateway_timeout_seconds, follow_redirects=False) as client:
        resp = client.post(url, headers=headers, content=body)
    if resp.status_code >= 400:
        raise PaymentError(f"微信退款失败:{resp.status_code} {resp.text[:160]}")
    data = resp.json()
    refund_status = str(data.get("status") or "")
    # 微信退款为异步受理:SUCCESS 立即成功;PROCESSING 表示已受理但尚未到账,
    # 不能当成功入账——退款单保持 pending,由 _resolve_pending_refunds 在
    # 下次退款/管理员操作时按 out_refund_no 查询收敛终态。
    if refund_status not in {"SUCCESS", "PROCESSING"}:
        raise PaymentError(f"微信退款状态异常:{refund_status or '未知'}")
    raw = _provider_raw_subset(
        data,
        ("refund_id", "out_refund_no", "out_trade_no", "status", "channel", "success_time"),
    )
    return {
        "provider_refund_no": data.get("refund_id"),
        "raw": raw,
        "processing": refund_status == "PROCESSING",
    }


def _provider_refund(db: Session, order: PaymentOrder, refund: PaymentRefund) -> dict:
    cfg = payment_config.runtime_or_env(db, order.provider)
    if not cfg.enabled or cfg.mode == "mock":
        if mock_payments_allowed():
            return {
                "provider_refund_no": f"mock_refund_{refund.refund_no}",
                "raw": {"mock": True, "provider": order.provider},
            }
        raise PaymentError(f"{order.provider} 支付渠道未启用真实退款")
    if order.provider == "alipay":
        if not _alipay_configured(cfg):
            raise PaymentError("支付宝商户参数未配置")
        return _alipay_refund(order, refund, cfg)
    if order.provider == "wechat":
        if not _wechat_configured(cfg):
            raise PaymentError("微信支付商户参数未配置")
        return _wechat_refund(order, refund, cfg)
    raise PaymentError("支付渠道非法")


def _reclaimed_credits_before(db: Session, order_id: int) -> int:
    return int(
        db.scalar(
            select(func.coalesce(func.sum(PaymentRefund.credits_reclaimed), 0)).where(
                PaymentRefund.order_id == order_id,
                PaymentRefund.status == REFUND_SUCCEEDED,
            )
        )
        or 0
    )


def _alipay_refund_query(
    order: PaymentOrder,
    refund: PaymentRefund,
    cfg: payment_config.ProviderRuntimeConfig,
) -> str:
    """向支付宝按 out_request_no 查退款结果:succeeded/failed,不确定时抛错。"""
    biz_content = {
        "out_trade_no": order.order_no,
        "out_request_no": refund.refund_no,
    }
    params = {
        "app_id": cfg.public.get("app_id"),
        "method": "alipay.trade.fastpay.refund.query",
        "format": "JSON",
        "charset": "utf-8",
        "sign_type": "RSA2",
        "timestamp": _now().strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0",
        "biz_content": json.dumps(biz_content, ensure_ascii=False, separators=(",", ":")),
    }
    sign_src = "&".join(f"{k}={params[k]}" for k in sorted(params))
    params["sign"] = _rsa_sha256_sign(sign_src, cfg.secret.get("private_key") or "")
    url = cfg.public.get("gateway_url") or settings.alipay_gateway_url
    with pinned_client(url, timeout=settings.gateway_timeout_seconds, follow_redirects=False) as client:
        resp = client.post(url, data=params)
    if resp.status_code >= 400:
        raise PaymentError(f"支付宝退款查询失败:{resp.status_code}")
    body = (resp.json() or {}).get("alipay_trade_fastpay_refund_query_response") or {}
    code = str(body.get("code") or "")
    sub_code = str(body.get("sub_code") or "")
    if code != "10000":
        if sub_code in {"ACQ.TRADE_NOT_EXIST", "TRADE_NOT_EXIST"}:
            # 渠道无此交易/退款请求:原退款调用从未到达渠道
            return "failed"
        raise PaymentError(body.get("sub_msg") or body.get("msg") or "支付宝退款查询失败")
    if str(body.get("refund_status") or "") == "REFUND_SUCCESS":
        return "succeeded"
    if body.get("refund_amount"):
        # 退款请求存在但尚未终态:不可当失败回滚,留待下次收敛
        return "processing"
    return "failed"


def _wechat_refund_query(
    order: PaymentOrder,
    refund: PaymentRefund,
    cfg: payment_config.ProviderRuntimeConfig,
) -> str:
    """按 out_refund_no 查微信退款结果:succeeded/failed/processing,不确定时抛错。"""
    path = f"/v3/refund/domestic/refunds/{refund.refund_no}"
    headers = {
        "Authorization": _wechat_authorization("GET", path, "", cfg),
        "Accept": "application/json",
    }
    url = (cfg.public.get("gateway_url") or settings.wechat_pay_gateway_url).rstrip("/") + path
    with pinned_client(url, timeout=settings.gateway_timeout_seconds, follow_redirects=False) as client:
        resp = client.get(url, headers=headers)
    if resp.status_code == 404:
        # 渠道无此退款单:原退款调用从未到达渠道
        return "failed"
    if resp.status_code >= 400:
        raise PaymentError(f"微信退款查询失败:{resp.status_code} {resp.text[:160]}")
    status = str((resp.json() or {}).get("status") or "")
    if status == "SUCCESS":
        return "succeeded"
    if status == "PROCESSING":
        return "processing"
    if status in {"CLOSED", "ABNORMAL"}:
        # ABNORMAL 表示打款到账失败,需商户后台人工处理;资金未到用户手中,
        # 平台侧按失败回滚积分,商户侧余款原路留存。
        return "failed"
    raise PaymentError(f"微信退款状态无法识别:{status or '空'}")


def _provider_refund_query(db: Session, order: PaymentOrder, refund: PaymentRefund) -> str:
    """查询渠道退款终态。返回 succeeded/failed/processing;网络或响应不可判定时抛
    PaymentError,调用方绝不能在不确定时二次扣积分或二次打款。"""
    cfg = payment_config.runtime_or_env(db, order.provider)
    if not cfg.enabled or cfg.mode == "mock":
        # mock 渠道没有真实打款,pending 残留只可能是成功落账前崩溃 -> 按失败回滚
        return "failed"
    if order.provider == "alipay":
        return _alipay_refund_query(order, refund, cfg)
    if order.provider == "wechat":
        return _wechat_refund_query(order, refund, cfg)
    raise PaymentError("支付渠道非法")


def _resolve_pending_refunds(db: Session, order: PaymentOrder) -> None:
    """把该订单的 pending 退款单收敛为终态,再允许发起新退款。

    崩溃残留的 pending 意味着积分已扣、渠道结果未知:必须先向渠道按
    refund_no 查询——成功则补记 refunded_amount,失败则回滚积分,仍在
    处理中或查询失败则拒绝本次新退款,绝不在不确定时二次扣款/打款。
    """
    pending = list(
        db.execute(
            select(PaymentRefund)
            .where(
                PaymentRefund.order_id == order.id,
                PaymentRefund.status == REFUND_PENDING,
            )
            .with_for_update()
        ).scalars()
    )
    for refund in pending:
        # 查询失败(网络/不可判定)直接抛出:该单保持 pending,之前已收敛的
        # 单据均已各自提交,不受影响。
        outcome = _provider_refund_query(db, order, refund)
        if outcome == "processing":
            raise PaymentError(f"退款单 {refund.refund_no} 渠道仍在处理中,请稍后再试")
        try:
            if outcome == "succeeded":
                refund.status = REFUND_SUCCEEDED
                refund.error = None
                order.refunded_amount_cents = (
                    int(order.refunded_amount_cents or 0) + int(refund.amount_cents)
                )
                if int(order.refunded_amount_cents) >= int(order.amount_cents):
                    order.status = REFUNDED
                    order.refunded_at = _now()
            else:
                # 先补回积分再置终态:任一步失败则整单回滚保持 pending,
                # 绝不提交"已失败但积分未回滚"的中间态。
                if int(refund.credits_reclaimed or 0) > 0:
                    credits.refund_consumed(
                        db,
                        order.user_id,
                        int(refund.credits_reclaimed),
                        biz_type="payment",
                        biz_ref=order.id,
                        note=f"payment_refund_rollback {refund.refund_no}",
                        commit=False,
                    )
                refund.status = REFUND_FAILED
                refund.error = "待确认退款经渠道查询未成功,已回滚积分扣减"
            db.commit()
        except Exception:
            db.rollback()
            raise


def refund_order(
    db: Session,
    order_no: str,
    *,
    operator_id: int | None,
    reason: str,
    amount_cents: int | None = None,
) -> PaymentRefund:
    """Refund a paid order (full or partial) through the original channel.

    顺序是"先扣回积分,再调渠道":渠道打款不可逆,而积分扣减随时可以正向补回。
    渠道调用失败时补回已扣积分并把退款单标记 failed,保证账实一致。
    """
    if not refunds_enabled():
        raise PaymentError("渠道退款功能未开启(需配置 PAYMENT_REFUND_ENABLED=true 并完成真实商户联调)")
    reason = (reason or "").strip()
    if not reason:
        raise PaymentError("请填写退款原因")
    lock_key = f"payment:refund:{order_no}"
    lock_ttl = max(180, int(settings.gateway_timeout_seconds) + 60)
    lock_token = locks.acquire(lock_key, ttl=lock_ttl)
    if not lock_token:
        raise PaymentError("该订单退款正在处理中,请稍后重试")
    try:
        order = db.execute(
            select(PaymentOrder)
            .where(PaymentOrder.order_no == order_no)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if not order:
            raise PaymentError("订单不存在")
        if order.status not in (PAID, REFUNDED) or not order.paid_at:
            raise PaymentError("仅已支付订单可以退款")
        # 先把崩溃/异步残留的 pending 退款单收敛为终态(成功补记已退金额、
        # 失败回滚积分、处理中或查询失败则拒绝本次请求),再计算可退余额,
        # 否则重试会对同一笔钱二次扣积分、二次向渠道打款。
        _resolve_pending_refunds(db, order)
        remaining = int(order.amount_cents) - int(order.refunded_amount_cents or 0)
        if remaining <= 0:
            raise PaymentError("订单已全额退款")
        amount = int(amount_cents) if amount_cents is not None else remaining
        if amount <= 0:
            raise PaymentError("退款金额必须大于 0")
        if amount > remaining:
            raise PaymentError(f"退款金额超出可退余额(剩余可退 {remaining} 分)")
        reclaimed_before = _reclaimed_credits_before(db, order.id)
        credits_remaining = max(0, int(order.credits) - reclaimed_before)
        if amount == remaining:
            # 最后一笔退清:扣回剩余全部积分,避免比例取整留尾差
            credits_to_reclaim = credits_remaining
        else:
            credits_to_reclaim = min(
                credits_remaining,
                int(order.credits) * amount // int(order.amount_cents),
            )
        refund = PaymentRefund(
            refund_no=make_refund_no(order.provider),
            order_id=order.id,
            user_id=order.user_id,
            provider=order.provider,
            amount_cents=amount,
            credits_reclaimed=credits_to_reclaim,
            status=REFUND_PENDING,
            reason=reason[:255],
            operator_id=operator_id,
        )
        db.add(refund)
        if credits_to_reclaim > 0:
            try:
                credits.deduct(
                    db,
                    order.user_id,
                    credits_to_reclaim,
                    note=f"payment_refund {order.provider} {order.order_no} {refund.refund_no}",
                    biz_type="payment",
                    biz_ref=order.id,
                    commit=False,
                )
            except credits.InsufficientCredits as e:
                db.rollback()
                raise PaymentError(
                    f"用户可用积分不足,无法扣回本次退款对应的 {credits_to_reclaim} 积分;"
                    "请先与用户确认或改用更小金额的部分退款"
                ) from e
        db.commit()
        db.refresh(refund)
        try:
            result = _provider_refund(db, order, refund)
        except Exception as e:  # noqa: BLE001
            # 渠道失败:补回已扣积分并把退款单标记 failed
            if credits_to_reclaim > 0:
                credits.refund_consumed(
                    db,
                    order.user_id,
                    credits_to_reclaim,
                    biz_type="payment",
                    biz_ref=order.id,
                    note=f"payment_refund_rollback {refund.refund_no}",
                    commit=False,
                )
            refund.status = REFUND_FAILED
            refund.error = str(e)[:500]
            db.commit()
            raise PaymentError(f"渠道退款失败,已回滚积分扣减:{str(e)[:200]}") from e
        refund.provider_refund_no = str(result.get("provider_refund_no") or "") or None
        refund.raw = result.get("raw") or {}
        if result.get("processing"):
            # 渠道已受理但未到账(微信 PROCESSING):保持 pending,不计入已退
            # 金额;积分已扣不回滚(资金大概率在途),终态由下次退款操作或
            # 管理员触发的 _resolve_pending_refunds 查询收敛。
            db.commit()
            db.refresh(refund)
            return refund
        refund.status = REFUND_SUCCEEDED
        order.refunded_amount_cents = int(order.refunded_amount_cents or 0) + amount
        if int(order.refunded_amount_cents) >= int(order.amount_cents):
            order.status = REFUNDED
            order.refunded_at = _now()
        db.commit()
        db.refresh(refund)
        publish_user_event(
            order.user_id,
            "payment_refunded",
            {
                "order_no": order.order_no,
                "provider": order.provider,
                "refund_no": refund.refund_no,
                "amount_cents": int(refund.amount_cents),
                "credits_reclaimed": int(refund.credits_reclaimed or 0),
            },
        )
        return refund
    finally:
        locks.release(lock_key, lock_token)


def list_order_refunds(db: Session, order_id: int) -> list[PaymentRefund]:
    return list(
        db.execute(
            select(PaymentRefund)
            .where(PaymentRefund.order_id == order_id)
            .order_by(PaymentRefund.id.desc())
        ).scalars()
    )
