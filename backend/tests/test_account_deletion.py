"""账号自助注销（两阶段软注销）集成测试。

覆盖:注销后原手机号可重新注册、脱敏后原手机号查不到、财务记录仍在但已解绑、
二次注销幂等、各类前置阻断（冻结积分/进行中任务/待支付订单/剩余积分未确认）。
"""
from datetime import datetime, timedelta, timezone

from app.db import SessionLocal
from app.models import CreditTransaction, GenTask, PaymentOrder, User, UserDraft
from app.services import account_deletion

PHONE = "13600000001"
PASSWORD = "pass123456"


def _delete_payload(confirm=True, password=PASSWORD):
    return {"password": password, "confirm_forfeit_credits": confirm}


def _get_user(user_id):
    db = SessionLocal()
    try:
        return db.get(User, user_id)
    finally:
        db.close()


def test_preflight_reports_forfeit_confirmation(client, make_user, auth):
    uid = make_user(PHONE, balance=500)
    headers = auth(PHONE)
    r = client.get("/api/me/account-deletion/preflight", headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["can_delete"] is True
    assert data["requires_credit_forfeit_confirmation"] is True
    assert data["balance_credits"] == 500
    assert data["blockers"] == []
    assert uid is not None


def test_delete_requires_explicit_credit_forfeit(client, make_user, auth):
    make_user("13600000002", balance=300)
    headers = auth("13600000002")
    r = client.post("/api/me/account-deletion", headers=headers,
                    json=_delete_payload(confirm=False))
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "confirm_forfeit_credits_required"
    assert detail["balance_credits"] == 300
    # 未确认时账号必须原样保留
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.phone == "13600000002").first()
        assert u is not None and u.status == "active" and u.deleted_at is None
    finally:
        db.close()


def test_delete_wrong_password_rejected(client, make_user, auth):
    make_user("13600000003", balance=0)
    headers = auth("13600000003")
    r = client.post("/api/me/account-deletion", headers=headers,
                    json=_delete_payload(password="wrong-password"))
    assert r.status_code == 400
    assert "密码不正确" in r.json()["detail"]


def test_admin_cannot_self_delete(client, make_user, auth):
    make_user("13600000004", admin=True)
    headers = auth("13600000004")
    r = client.post("/api/me/account-deletion", headers=headers, json=_delete_payload())
    assert r.status_code == 400
    assert "管理员" in r.json()["detail"]


def test_delete_then_reregister_same_phone(client, make_user, auth):
    phone = "13600000005"
    uid = make_user(phone, balance=250)
    headers = auth(phone)
    # 留一条草稿,注销后应被删除
    r = client.put("/api/me/drafts/studio", headers=headers,
                   json={"payload": {"secret": "personal-workspace"}})
    assert r.status_code == 200

    r = client.post("/api/me/account-deletion", headers=headers, json=_delete_payload())
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["already_deleted"] is False
    assert data["forfeited_credits"] == 250
    assert data["phone_masked"] == "136****0005"

    # 旧 token 立即失效
    r = client.get("/api/me", headers=headers)
    assert r.status_code in (401, 403)
    # 旧手机号+旧密码不能再登录
    r = client.post("/api/auth/login", json={"phone": phone, "password": PASSWORD})
    assert r.status_code == 401

    db = SessionLocal()
    try:
        # 脱敏后原手机号在 users 表查不到
        assert db.query(User).filter(User.phone == phone).first() is None
        old = db.get(User, uid)
        assert old.status == "disabled"
        assert old.deleted_at is not None
        assert old.password_hash is None
        assert old.nickname is None and old.avatar is None and old.department is None
        assert old.phone != phone
        assert account_deletion.is_anonymized_phone(old.phone)
        assert old.phone.startswith("del0005")  # 保留末四位
        assert old.balance_credits == 0
        # 草稿已删除
        assert db.query(UserDraft).filter(UserDraft.user_id == uid).count() == 0
        # 财务记录仍在（放弃积分的 consume 台账）,但通过 user 行已找不到自然人
        txs = db.query(CreditTransaction).filter(
            CreditTransaction.user_id == uid,
            CreditTransaction.biz_type == "account_deletion",
        ).all()
        assert len(txs) == 1
        assert txs[0].type == "consume"
        assert txs[0].change == -250
        assert txs[0].balance_after == 0
    finally:
        db.close()

    # 原手机号即刻可重新注册,并得到全新账号
    r = client.post("/api/auth/register",
                    json={"phone": phone, "password": "newpass123456"})
    assert r.status_code == 200, r.text
    db = SessionLocal()
    try:
        fresh = db.query(User).filter(User.phone == phone).first()
        assert fresh is not None
        assert fresh.id != uid
        assert fresh.status == "active"
        assert fresh.balance_credits == 0  # 新账号不继承旧积分
    finally:
        db.close()


def test_delete_is_idempotent(make_user):
    phone = "13600000006"
    uid = make_user(phone, balance=0)
    db = SessionLocal()
    try:
        first = account_deletion.delete_account(db, uid, confirm_forfeit_credits=True)
        assert first["ok"] is True and first["already_deleted"] is False
        second = account_deletion.delete_account(db, uid, confirm_forfeit_credits=True)
        assert second["ok"] is True and second["already_deleted"] is True
        assert second["forfeited_credits"] == 0
        # 二次调用不追加台账
        n = db.query(CreditTransaction).filter(
            CreditTransaction.user_id == uid,
            CreditTransaction.biz_type == "account_deletion",
        ).count()
        assert n == 0  # 余额为 0,本就无放弃台账
    finally:
        db.close()


def test_frozen_credits_block_deletion(client, make_user, auth):
    phone = "13600000007"
    uid = make_user(phone, balance=100)
    db = SessionLocal()
    try:
        db.get(User, uid).frozen_credits = 50
        db.commit()
    finally:
        db.close()
    headers = auth(phone)
    r = client.post("/api/me/account-deletion", headers=headers, json=_delete_payload())
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "account_deletion_blocked"
    assert any(b["code"] == "frozen_credits" for b in detail["blockers"])


def test_active_generation_task_blocks_deletion(client, make_user, auth):
    phone = "13600000008"
    uid = make_user(phone, balance=0)
    db = SessionLocal()
    try:
        db.add(GenTask(user_id=uid, category="image", status="queued"))
        db.commit()
    finally:
        db.close()
    headers = auth(phone)
    r = client.post("/api/me/account-deletion", headers=headers, json=_delete_payload())
    assert r.status_code == 409, r.text
    blockers = r.json()["detail"]["blockers"]
    assert any(b["code"] == "active_generation_tasks" for b in blockers)


def test_pending_payment_order_blocks_deletion(client, make_user, auth):
    phone = "13600000009"
    uid = make_user(phone, balance=0)
    db = SessionLocal()
    try:
        db.add(PaymentOrder(
            order_no="test-del-0009",
            user_id=uid,
            provider="alipay",
            package_id="p1",
            amount_cents=100,
            credits=10,
            status="pending",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        ))
        db.commit()
    finally:
        db.close()
    headers = auth(phone)
    r = client.post("/api/me/account-deletion", headers=headers, json=_delete_payload())
    assert r.status_code == 409, r.text
    blockers = r.json()["detail"]["blockers"]
    assert any(b["code"] == "pending_payment_orders" for b in blockers)


def test_zero_balance_delete_needs_no_confirmation(client, make_user, auth):
    phone = "13600000010"
    uid = make_user(phone, balance=0)
    headers = auth(phone)
    r = client.post("/api/me/account-deletion", headers=headers,
                    json=_delete_payload(confirm=False))
    assert r.status_code == 200, r.text
    assert r.json()["forfeited_credits"] == 0
    assert _get_user(uid).deleted_at is not None
