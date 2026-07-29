"""资金侧运营能力:管理端查单/补单、积分扣减冲正、渠道退款、发票与账单导出。"""
from datetime import datetime, timezone

from app.db import SessionLocal
from app.models import AuditLog, CreditTransaction, PaymentOrder, User
from app.models.payment import PaymentRefund
from app.services import payments
from app.services.config_store import set_setting


def _set_payment_enabled(enabled: bool = True):
    db = SessionLocal()
    try:
        set_setting(db, "payment_enabled", enabled)
    finally:
        db.close()


def _paid_order(client, headers, provider="alipay", package_id="starter"):
    order = client.post("/api/payments/orders", json={
        "provider": provider,
        "package_id": package_id,
    }, headers=headers).json()
    paid = client.post(f"/api/payments/orders/{order['order_no']}/mock-pay", headers=headers)
    assert paid.status_code == 200, paid.text
    return paid.json()


def _balance(client, headers):
    return client.get("/api/me", headers=headers).json()["balance_credits"]


def _enable_refunds(monkeypatch):
    # payment_refund_enabled 配置项由 Integrate 阶段统一加入 config.py;
    # 服务侧通过 refunds_enabled() 读取,测试直接替换该函数。
    monkeypatch.setattr(payments, "refunds_enabled", lambda: True)


# --- 管理端订单查询 ---


def test_admin_order_search_by_phone_order_no_and_pagination(client, make_user, auth):
    _set_payment_enabled(True)
    make_user("13700000001", balance=100)
    make_user("13700000099", balance=100, admin=True)
    h = auth("13700000001")
    admin_h = auth("13700000099")
    orders = [_paid_order(client, h) for _ in range(3)]

    r = client.get("/api/admin/payments/orders", params={"phone": "13700000001"}, headers=admin_h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] >= 3
    assert all(item["phone"] == "13700000001" for item in body["items"])
    assert {"order_no", "status", "refunded_amount_cents", "invoice_status"} <= set(body["items"][0])

    r = client.get("/api/admin/payments/orders",
                   params={"order_no": orders[0]["order_no"]}, headers=admin_h)
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["order_no"] == orders[0]["order_no"]

    # 渠道单号查询(mock 支付的流水号是 mock_<order_no>)
    r = client.get("/api/admin/payments/orders",
                   params={"provider_trade_no": f"mock_{orders[1]['order_no']}"}, headers=admin_h)
    assert r.json()["total"] == 1

    # 分页
    page1 = client.get("/api/admin/payments/orders",
                       params={"phone": "13700000001", "limit": 2, "offset": 0}, headers=admin_h).json()
    page2 = client.get("/api/admin/payments/orders",
                       params={"phone": "13700000001", "limit": 2, "offset": 2}, headers=admin_h).json()
    assert len(page1["items"]) == 2
    assert page1["items"][0]["id"] > page1["items"][1]["id"]
    assert {i["order_no"] for i in page1["items"]}.isdisjoint({i["order_no"] for i in page2["items"]})

    # 时间范围:未来起点查不到
    r = client.get("/api/admin/payments/orders",
                   params={"phone": "13700000001", "created_from": "2999-01-01"}, headers=admin_h)
    assert r.json()["total"] == 0


def test_admin_order_search_requires_admin(client, make_user, auth):
    _set_payment_enabled(True)
    make_user("13700000002", balance=100)
    h = auth("13700000002")
    r = client.get("/api/admin/payments/orders", headers=h)
    assert r.status_code == 403


# --- 单笔主动查渠道补单 ---


def test_admin_sync_order_credits_missing_callback(client, make_user, auth, monkeypatch):
    _set_payment_enabled(True)
    make_user("13700000003", balance=100)
    make_user("13700000098", balance=100, admin=True)
    h = auth("13700000003")
    admin_h = auth("13700000098")
    order = client.post("/api/payments/orders", json={
        "provider": "alipay", "package_id": "starter",
    }, headers=h).json()

    monkeypatch.setattr("app.services.payments._query_provider_order", lambda db, row: {
        "status": payments.PAID,
        "provider_trade_no": "ali_admin_sync_paid",
        "raw": {"trade_status": "TRADE_SUCCESS"},
    })
    r = client.post(f"/api/admin/payments/orders/{order['order_no']}/sync", headers=admin_h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "paid"
    assert body["order"]["status"] == "paid"
    assert body["order"]["provider_trade_no"] == "ali_admin_sync_paid"
    assert _balance(client, h) == 200

    # 重复补单幂等:不会二次入账
    r = client.post(f"/api/admin/payments/orders/{order['order_no']}/sync", headers=admin_h)
    assert r.status_code == 200
    assert r.json()["outcome"] == "noop"
    assert _balance(client, h) == 200

    # 人工补单必须写 audit 日志
    db = SessionLocal()
    try:
        row = (
            db.query(AuditLog)
            .filter(AuditLog.action == "admin_sync_payment_order")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert row is not None
        assert row.detail["order_no"] == order["order_no"]
    finally:
        db.close()


def test_admin_sync_order_unknown_order_404(client, make_user, auth):
    _set_payment_enabled(True)
    make_user("13700000097", balance=100, admin=True)
    admin_h = auth("13700000097")
    r = client.post("/api/admin/payments/orders/NO_SUCH_ORDER/sync", headers=admin_h)
    assert r.status_code == 404


# --- 管理端积分扣减 / 冲正 ---


def test_admin_quota_deduct_flow(client, make_user, auth):
    uid = make_user("13700000004", balance=300)
    make_user("13700000096", balance=100, admin=True)
    admin_h = auth("13700000096")

    r = client.post("/api/admin/quota/deduct", json={
        "user_id": uid, "amount": 120, "note": "批量发放金额填错冲正",
        "idempotency_key": "deduct-test-0001",
    }, headers=admin_h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deducted_amount"] == 120
    assert body["balance_credits"] == 180

    # 幂等重放:同键同参数不再扣减
    r = client.post("/api/admin/quota/deduct", json={
        "user_id": uid, "amount": 120, "note": "批量发放金额填错冲正",
        "idempotency_key": "deduct-test-0001",
    }, headers=admin_h)
    assert r.status_code == 200
    assert r.json()["deducted_amount"] == 0
    assert r.json()["balance_credits"] == 180

    # 同键不同参数拒绝
    r = client.post("/api/admin/quota/deduct", json={
        "user_id": uid, "amount": 50, "note": "另一笔",
        "idempotency_key": "deduct-test-0001",
    }, headers=admin_h)
    assert r.status_code == 409

    # 余额不足默认拒绝
    r = client.post("/api/admin/quota/deduct", json={
        "user_id": uid, "amount": 999, "note": "超额冲正",
        "idempotency_key": "deduct-test-0002",
    }, headers=admin_h)
    assert r.status_code == 400
    assert "余额不足" in r.json()["detail"]

    # allow_partial 扣到 0 为止
    r = client.post("/api/admin/quota/deduct", json={
        "user_id": uid, "amount": 999, "note": "允许部分冲正", "allow_partial": True,
        "idempotency_key": "deduct-test-0003",
    }, headers=admin_h)
    assert r.status_code == 200
    assert r.json()["deducted_amount"] == 180
    assert r.json()["balance_credits"] == 0

    # 全部走 credit_transactions 流水
    db = SessionLocal()
    try:
        rows = (
            db.query(CreditTransaction)
            .filter(CreditTransaction.user_id == uid, CreditTransaction.type == "consume")
            .order_by(CreditTransaction.id)
            .all()
        )
        assert [int(r.balance_delta) for r in rows] == [-120, -180]
        audit_row = (
            db.query(AuditLog)
            .filter(AuditLog.action == "deduct_quota", AuditLog.biz_id == uid)
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert audit_row is not None
    finally:
        db.close()


def test_admin_quota_deduct_limits_and_permissions(client, make_user, auth):
    uid = make_user("13700000005", balance=100)
    make_user("13700000095", balance=100, admin=True)
    admin_h = auth("13700000095")
    h = auth("13700000005")

    # 超过单次上限
    r = client.post("/api/admin/quota/deduct", json={
        "user_id": uid, "amount": 100001, "note": "超限",
        "idempotency_key": "deduct-limit-0001",
    }, headers=admin_h)
    assert r.status_code == 400
    assert "单次扣减额度" in r.json()["detail"]

    # 非管理员禁止
    r = client.post("/api/admin/quota/deduct", json={
        "user_id": uid, "amount": 10, "note": "越权",
        "idempotency_key": "deduct-perm-0001",
    }, headers=h)
    assert r.status_code == 403

    # 原因必填
    r = client.post("/api/admin/quota/deduct", json={
        "user_id": uid, "amount": 10, "note": "   ",
        "idempotency_key": "deduct-note-0001",
    }, headers=admin_h)
    assert r.status_code == 422

    # 用户不存在
    r = client.post("/api/admin/quota/deduct", json={
        "user_id": 99999999, "amount": 10, "note": "不存在",
        "idempotency_key": "deduct-user-0001",
    }, headers=admin_h)
    assert r.status_code == 404


# --- 渠道退款 ---


def test_refund_disabled_by_default(client, make_user, auth):
    _set_payment_enabled(True)
    make_user("13700000006", balance=100)
    make_user("13700000094", balance=100, admin=True)
    h = auth("13700000006")
    admin_h = auth("13700000094")
    order = _paid_order(client, h)
    r = client.post(f"/api/admin/payments/orders/{order['order_no']}/refund",
                    json={"reason": "用户申请退款"}, headers=admin_h)
    assert r.status_code == 400
    assert "退款功能未开启" in r.json()["detail"]


def test_full_refund_reclaims_credits_and_marks_refunded(client, make_user, auth, monkeypatch):
    _set_payment_enabled(True)
    _enable_refunds(monkeypatch)
    make_user("13700000007", balance=100)
    make_user("13700000093", balance=100, admin=True)
    h = auth("13700000007")
    admin_h = auth("13700000093")
    order = _paid_order(client, h)  # starter: 990 分 / 100 积分
    assert _balance(client, h) == 200

    r = client.post(f"/api/admin/payments/orders/{order['order_no']}/refund",
                    json={"reason": "用户申请全额退款"}, headers=admin_h)
    assert r.status_code == 200, r.text
    refund = r.json()
    assert refund["status"] == "succeeded"
    assert refund["amount_cents"] == 990
    assert refund["credits_reclaimed"] == 100
    assert refund["provider_refund_no"].startswith("mock_refund_")
    assert _balance(client, h) == 100

    detail = client.get(f"/api/payments/orders/{order['order_no']}", headers=h).json()
    assert detail["status"] == "refunded"
    assert detail["refunded_amount_cents"] == 990

    # 已退清不能再退
    r = client.post(f"/api/admin/payments/orders/{order['order_no']}/refund",
                    json={"reason": "重复退款"}, headers=admin_h)
    assert r.status_code == 400
    assert "已全额退款" in r.json()["detail"] or "仅已支付订单" in r.json()["detail"]

    # 退款流水与 audit
    db = SessionLocal()
    try:
        tx = (
            db.query(CreditTransaction)
            .filter(
                CreditTransaction.biz_type == "payment",
                CreditTransaction.type == "consume",
                CreditTransaction.biz_ref == order["id"],
            )
            .one()
        )
        assert int(tx.balance_delta) == -100
        audit_row = (
            db.query(AuditLog)
            .filter(AuditLog.action == "admin_refund_payment_order", AuditLog.biz_id == order["id"])
            .first()
        )
        assert audit_row is not None
    finally:
        db.close()


def test_partial_refund_is_proportional_then_final_clears_rest(client, make_user, auth, monkeypatch):
    _set_payment_enabled(True)
    _enable_refunds(monkeypatch)
    make_user("13700000008", balance=100)
    make_user("13700000092", balance=100, admin=True)
    h = auth("13700000008")
    admin_h = auth("13700000092")
    order = _paid_order(client, h, package_id="creator")  # 2990 分 / 330 积分
    assert _balance(client, h) == 430

    r = client.post(f"/api/admin/payments/orders/{order['order_no']}/refund",
                    json={"amount_cents": 1000, "reason": "部分退款"}, headers=admin_h)
    assert r.status_code == 200, r.text
    assert r.json()["credits_reclaimed"] == 330 * 1000 // 2990  # 110
    assert _balance(client, h) == 430 - 110

    detail = client.get(f"/api/payments/orders/{order['order_no']}", headers=h).json()
    assert detail["status"] == "paid"  # 部分退款保持 paid
    assert detail["refunded_amount_cents"] == 1000

    # 超出剩余可退金额拒绝
    r = client.post(f"/api/admin/payments/orders/{order['order_no']}/refund",
                    json={"amount_cents": 2000, "reason": "超额"}, headers=admin_h)
    assert r.status_code == 400
    assert "超出可退余额" in r.json()["detail"]

    # 退清剩余:积分扣回不留尾差
    r = client.post(f"/api/admin/payments/orders/{order['order_no']}/refund",
                    json={"amount_cents": 1990, "reason": "退清"}, headers=admin_h)
    assert r.status_code == 200
    assert r.json()["credits_reclaimed"] == 330 - 110
    assert _balance(client, h) == 100
    detail = client.get(f"/api/payments/orders/{order['order_no']}", headers=h).json()
    assert detail["status"] == "refunded"
    assert detail["refunded_amount_cents"] == 2990


def test_refund_rejected_when_user_balance_insufficient(client, make_user, auth, monkeypatch):
    _set_payment_enabled(True)
    _enable_refunds(monkeypatch)
    uid = make_user("13700000009", balance=0)
    make_user("13700000091", balance=100, admin=True)
    h = auth("13700000009")
    admin_h = auth("13700000091")
    order = _paid_order(client, h)  # +100 积分
    # 用户把积分花掉一部分(用管理端冲正模拟消耗)
    r = client.post("/api/admin/quota/deduct", json={
        "user_id": uid, "amount": 60, "note": "模拟消耗",
        "idempotency_key": "refund-prep-0001",
    }, headers=admin_h)
    assert r.status_code == 200
    assert _balance(client, h) == 40

    r = client.post(f"/api/admin/payments/orders/{order['order_no']}/refund",
                    json={"reason": "余额不足退款"}, headers=admin_h)
    assert r.status_code == 400
    assert "积分不足" in r.json()["detail"]
    # 订单与余额均不变,不留退款记录
    assert _balance(client, h) == 40
    detail = client.get(f"/api/payments/orders/{order['order_no']}", headers=h).json()
    assert detail["status"] == "paid"
    assert detail["refunded_amount_cents"] == 0
    db = SessionLocal()
    try:
        assert (
            db.query(PaymentRefund)
            .join(PaymentOrder, PaymentOrder.id == PaymentRefund.order_id)
            .filter(PaymentOrder.order_no == order["order_no"])
            .count()
            == 0
        )
    finally:
        db.close()


def test_refund_channel_failure_rolls_back_credits(client, make_user, auth, monkeypatch):
    _set_payment_enabled(True)
    _enable_refunds(monkeypatch)
    make_user("13700000010", balance=100)
    make_user("13700000090", balance=100, admin=True)
    h = auth("13700000010")
    admin_h = auth("13700000090")
    order = _paid_order(client, h)
    assert _balance(client, h) == 200

    def _boom(db, o, refund):
        raise payments.PaymentError("渠道联调失败")

    monkeypatch.setattr("app.services.payments._provider_refund", _boom)
    r = client.post(f"/api/admin/payments/orders/{order['order_no']}/refund",
                    json={"reason": "渠道失败演练"}, headers=admin_h)
    assert r.status_code == 400
    assert "已回滚积分扣减" in r.json()["detail"]
    # 积分补回,订单未进入退款态,退款单标记 failed 供人工排查
    assert _balance(client, h) == 200
    detail = client.get(f"/api/payments/orders/{order['order_no']}", headers=h).json()
    assert detail["status"] == "paid"
    assert detail["refunded_amount_cents"] == 0
    db = SessionLocal()
    try:
        refund_row = (
            db.query(PaymentRefund)
            .join(PaymentOrder, PaymentOrder.id == PaymentRefund.order_id)
            .filter(PaymentOrder.order_no == order["order_no"])
            .one()
        )
        assert refund_row.status == "failed"
        assert "渠道联调失败" in (refund_row.error or "")
    finally:
        db.close()


# --- 发票 ---


def test_invoice_request_and_admin_processing(client, make_user, auth):
    _set_payment_enabled(True)
    make_user("13700000011", balance=100)
    make_user("13700000089", balance=100, admin=True)
    h = auth("13700000011")
    admin_h = auth("13700000089")
    order = _paid_order(client, h)

    # 企业抬头必须带税号
    r = client.post(f"/api/payments/orders/{order['order_no']}/invoice", json={
        "invoice_type": "company", "title": "某某科技有限公司",
    }, headers=h)
    assert r.status_code == 400
    assert "税号" in r.json()["detail"]

    r = client.post(f"/api/payments/orders/{order['order_no']}/invoice", json={
        "invoice_type": "company", "title": "某某科技有限公司",
        "tax_no": "91330100MA27XXXX0X", "email": "billing@example.com",
    }, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["invoice_status"] == "requested"

    # 重复申请拦截
    r = client.post(f"/api/payments/orders/{order['order_no']}/invoice", json={
        "invoice_type": "personal", "title": "张三",
    }, headers=h)
    assert r.status_code == 400

    # 用户可见自己的申请
    mine = client.get("/api/payments/invoices", headers=h).json()
    assert any(item["order_no"] == order["order_no"] for item in mine)

    # 管理端查看申请
    r = client.get("/api/admin/payments/invoices", params={"status": "requested"}, headers=admin_h)
    assert r.status_code == 200
    items = r.json()["items"]
    target = next(item for item in items if item["order_no"] == order["order_no"])
    assert target["invoice_title"] == "某某科技有限公司"
    assert target["invoice_tax_no"] == "91330100MA27XXXX0X"

    # 管理端标记已开票
    r = client.put(f"/api/admin/payments/invoices/{order['order_no']}",
                   json={"status": "issued", "note": "已开电子普票"}, headers=admin_h)
    assert r.status_code == 200
    assert r.json()["order"]["invoice_status"] == "issued"
    detail = client.get(f"/api/payments/orders/{order['order_no']}", headers=h).json()
    assert detail["invoice_status"] == "issued"


def test_invoice_requires_paid_order(client, make_user, auth):
    _set_payment_enabled(True)
    make_user("13700000012", balance=100)
    h = auth("13700000012")
    order = client.post("/api/payments/orders", json={
        "provider": "alipay", "package_id": "starter",
    }, headers=h).json()
    r = client.post(f"/api/payments/orders/{order['order_no']}/invoice", json={
        "invoice_type": "personal", "title": "张三",
    }, headers=h)
    assert r.status_code == 400
    assert "仅已支付订单" in r.json()["detail"]


# --- 用户侧订单分页与账单导出 ---


def test_my_orders_supports_offset_and_cursor(client, make_user, auth):
    _set_payment_enabled(True)
    make_user("13700000013", balance=100)
    h = auth("13700000013")
    orders = [_paid_order(client, h) for _ in range(3)]
    ids = sorted((o["id"] for o in orders), reverse=True)

    page1 = client.get("/api/payments/orders", params={"limit": 2}, headers=h).json()
    assert [o["id"] for o in page1] == ids[:2]

    # offset 兜底
    page2 = client.get("/api/payments/orders", params={"limit": 2, "offset": 2}, headers=h).json()
    assert [o["id"] for o in page2] == ids[2:3]

    # cursor:传上一页最后一条 id,只取更早订单
    page2c = client.get("/api/payments/orders",
                        params={"limit": 2, "cursor": page1[-1]["id"]}, headers=h).json()
    assert [o["id"] for o in page2c] == ids[2:3]


def test_billing_csv_export(client, make_user, auth):
    _set_payment_enabled(True)
    make_user("13700000014", balance=100)
    h = auth("13700000014")
    _paid_order(client, h)
    r = client.get("/api/payments/billing/export", headers=h)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers.get("content-disposition", "")
    body = r.text
    assert "时间" in body and "余额变动" in body
    assert "grant" in body  # 充值入账流水
    # CSV 公式注入防护:恶意备注以单引号转义(csv_cell)
    assert "=cmd" not in body.splitlines()[0]


def test_refunded_status_visible_in_admin_search(client, make_user, auth, monkeypatch):
    _set_payment_enabled(True)
    _enable_refunds(monkeypatch)
    make_user("13700000015", balance=100)
    make_user("13700000088", balance=100, admin=True)
    h = auth("13700000015")
    admin_h = auth("13700000088")
    order = _paid_order(client, h)
    r = client.post(f"/api/admin/payments/orders/{order['order_no']}/refund",
                    json={"reason": "状态检索用例"}, headers=admin_h)
    assert r.status_code == 200
    r = client.get("/api/admin/payments/orders",
                   params={"phone": "13700000015", "status": "refunded"}, headers=admin_h)
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["order_no"] == order["order_no"]


def _utcnow():
    return datetime.now(timezone.utc)


def test_admin_order_dict_includes_user_fields():
    db = SessionLocal()
    try:
        row = db.query(PaymentOrder).order_by(PaymentOrder.id.desc()).first()
        if row is None:
            return
        phone = db.query(User.phone).filter(User.id == row.user_id).scalar()
        data = payments.admin_order_dict(row, phone)
        assert data["phone"] == phone
        assert data["user_id"] == row.user_id
    finally:
        db.close()


# --- pending 退款单恢复:崩溃残留绝不二次扣积分/二次打款 ---


def _inject_crashed_pending_refund(order, *, amount_cents, credits_reclaimed):
    """模拟 refund_order 在 db.commit() 后、渠道调用完成前崩溃的残留现场:
    退款单停在 pending,积分已扣,order.refunded_amount_cents 未增加。"""
    from app.services import credits as credits_service

    db = SessionLocal()
    try:
        row = db.query(PaymentOrder).filter(PaymentOrder.order_no == order["order_no"]).one()
        refund = PaymentRefund(
            refund_no=payments.make_refund_no(order["provider"]),
            order_id=row.id,
            user_id=row.user_id,
            provider=order["provider"],
            amount_cents=amount_cents,
            credits_reclaimed=credits_reclaimed,
            status="pending",
            reason="crash residue",
            operator_id=None,
        )
        db.add(refund)
        if credits_reclaimed > 0:
            credits_service.deduct(
                db,
                row.user_id,
                credits_reclaimed,
                note=f"payment_refund {order['provider']} {order['order_no']} {refund.refund_no}",
                biz_type="payment",
                biz_ref=row.id,
                commit=False,
            )
        db.commit()
        return refund.refund_no
    finally:
        db.close()


def test_pending_refund_recovery_failed_rolls_back_and_allows_clean_retry(
    client, make_user, auth, monkeypatch
):
    _set_payment_enabled(True)
    _enable_refunds(monkeypatch)
    make_user("13700000021", balance=100)
    make_user("13700000094", balance=100, admin=True)
    h = auth("13700000021")
    admin_h = auth("13700000094")
    order = _paid_order(client, h)  # starter: 990 分 / 100 积分
    assert _balance(client, h) == 200

    residual_no = _inject_crashed_pending_refund(order, amount_cents=990, credits_reclaimed=100)
    assert _balance(client, h) == 100  # 崩溃残留:积分已扣

    # 渠道查无此退款 -> 残留单转 failed 并回滚积分,本次新退款正常执行
    monkeypatch.setattr(payments, "_provider_refund_query", lambda db, o, r: "failed")
    r = client.post(
        f"/api/admin/payments/orders/{order['order_no']}/refund",
        json={"reason": "崩溃后重试全额退款"},
        headers=admin_h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "succeeded"
    # 关键恒等:仅扣一次 100 积分,绝无双扣
    assert _balance(client, h) == 100
    detail = client.get(f"/api/payments/orders/{order['order_no']}", headers=h).json()
    assert detail["refunded_amount_cents"] == 990
    db = SessionLocal()
    try:
        residual = db.query(PaymentRefund).filter(PaymentRefund.refund_no == residual_no).one()
        assert residual.status == "failed"
        assert "回滚" in (residual.error or "")
    finally:
        db.close()


def test_pending_refund_recovery_succeeded_counts_refunded_amount(
    client, make_user, auth, monkeypatch
):
    _set_payment_enabled(True)
    _enable_refunds(monkeypatch)
    make_user("13700000022", balance=100)
    make_user("13700000095", balance=100, admin=True)
    h = auth("13700000022")
    admin_h = auth("13700000095")
    order = _paid_order(client, h)
    _inject_crashed_pending_refund(order, amount_cents=990, credits_reclaimed=100)

    # 渠道确认已打款 -> 残留单补记为 succeeded,再退会被余额校验拦下
    monkeypatch.setattr(payments, "_provider_refund_query", lambda db, o, r: "succeeded")
    r = client.post(
        f"/api/admin/payments/orders/{order['order_no']}/refund",
        json={"reason": "重试"},
        headers=admin_h,
    )
    assert r.status_code == 400, r.text
    assert "已全额退款" in r.json()["detail"]
    assert _balance(client, h) == 100  # 首次扣的积分保持扣减,无重复动账
    detail = client.get(f"/api/payments/orders/{order['order_no']}", headers=h).json()
    assert detail["status"] == "refunded"
    assert detail["refunded_amount_cents"] == 990


def test_pending_refund_processing_blocks_new_refund(client, make_user, auth, monkeypatch):
    _set_payment_enabled(True)
    _enable_refunds(monkeypatch)
    make_user("13700000023", balance=100)
    make_user("13700000096", balance=100, admin=True)
    h = auth("13700000023")
    admin_h = auth("13700000096")
    order = _paid_order(client, h)
    _inject_crashed_pending_refund(order, amount_cents=500, credits_reclaimed=50)

    monkeypatch.setattr(payments, "_provider_refund_query", lambda db, o, r: "processing")
    r = client.post(
        f"/api/admin/payments/orders/{order['order_no']}/refund",
        json={"reason": "重试"},
        headers=admin_h,
    )
    assert r.status_code == 400, r.text
    assert "处理中" in r.json()["detail"]
    assert _balance(client, h) == 150  # 不确定期间绝不二次动账


def test_wechat_processing_refund_stays_pending_until_query_confirms(
    client, make_user, auth, monkeypatch
):
    _set_payment_enabled(True)
    _enable_refunds(monkeypatch)
    make_user("13700000024", balance=100)
    make_user("13700000097", balance=100, admin=True)
    h = auth("13700000024")
    admin_h = auth("13700000097")
    order = _paid_order(client, h)

    # 渠道受理但未到账:退款单保持 pending,不计入已退金额
    monkeypatch.setattr(
        payments,
        "_provider_refund",
        lambda db, o, r: {"provider_refund_no": "wx_rf_1", "raw": {"status": "PROCESSING"}, "processing": True},
    )
    r = client.post(
        f"/api/admin/payments/orders/{order['order_no']}/refund",
        json={"reason": "微信异步退款"},
        headers=admin_h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending"
    assert _balance(client, h) == 100  # 积分已扣(资金在途)
    detail = client.get(f"/api/payments/orders/{order['order_no']}", headers=h).json()
    assert detail["refunded_amount_cents"] == 0

    # 渠道终态到达 -> 下一次操作把 pending 收敛为 succeeded 并补记已退金额
    monkeypatch.setattr(payments, "_provider_refund_query", lambda db, o, r: "succeeded")
    r = client.post(
        f"/api/admin/payments/orders/{order['order_no']}/refund",
        json={"reason": "重试"},
        headers=admin_h,
    )
    assert r.status_code == 400
    assert "已全额退款" in r.json()["detail"]
    detail = client.get(f"/api/payments/orders/{order['order_no']}", headers=h).json()
    assert detail["status"] == "refunded"
    assert detail["refunded_amount_cents"] == 990
