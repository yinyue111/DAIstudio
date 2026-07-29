"""免登录自助找回密码链路测试。

覆盖:正常重置(旧密码/旧 token 失效)、验证码错误、验证码过期、
发码冷却与提交限流、未注册手机号不泄露存在性、用途隔离。
"""
from app.redis_client import redis_client
from app.routers import auth as auth_router
from app.services import sms

# 每个测试用独立手机号:DB 跨测试持久,make_user 对已存在用户不会重置密码
OLD_PASSWORD = "oldpass123"
NEW_PASSWORD = "newpass456"


def _send_reset_code(client, phone):
    r = client.post("/api/auth/sms/send", json={"phone": phone, "purpose": "reset"})
    assert r.status_code == 200, r.text
    return r.json()


def test_reset_password_success_and_revokes_old_tokens(client, make_user):
    make_user("13900007001", password=OLD_PASSWORD)
    login = client.post(
        "/api/auth/login", json={"phone": "13900007001", "password": OLD_PASSWORD}
    )
    assert login.status_code == 200, login.text
    old_token = login.json()["access_token"]
    client.cookies.clear()  # 重置是免登录操作,不携带旧会话 cookie

    sent = _send_reset_code(client, "13900007001")
    assert sent["ok"] is True
    code = sent["code"]  # debug + mock 渠道下回显验证码

    r = client.post(
        "/api/auth/password/reset",
        json={"phone": "13900007001", "sms_code": code, "new_password": NEW_PASSWORD},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}

    # 旧 token 因 token_version 提升而失效
    me = client.get("/api/me", headers={"Authorization": f"Bearer {old_token}"})
    assert me.status_code == 401, me.text

    # 旧密码不能再登录,新密码可以
    old_login = client.post(
        "/api/auth/login", json={"phone": "13900007001", "password": OLD_PASSWORD}
    )
    assert old_login.status_code == 401
    new_login = client.post(
        "/api/auth/login", json={"phone": "13900007001", "password": NEW_PASSWORD}
    )
    assert new_login.status_code == 200, new_login.text
    client.cookies.clear()

    # 验证码一次性消费:同一验证码不能重复使用
    replay = client.post(
        "/api/auth/password/reset",
        json={"phone": "13900007001", "sms_code": code, "new_password": "another789"},
    )
    assert replay.status_code == 400
    assert "验证码不存在或已过期" in replay.text


def test_reset_password_wrong_code(client, make_user):
    make_user("13900007002", password=OLD_PASSWORD)
    sent = _send_reset_code(client, "13900007002")
    wrong = "000000" if sent["code"] != "000000" else "111111"

    r = client.post(
        "/api/auth/password/reset",
        json={"phone": "13900007002", "sms_code": wrong, "new_password": NEW_PASSWORD},
    )
    assert r.status_code == 400
    assert "验证码错误" in r.text

    # 旧密码仍然有效
    login = client.post(
        "/api/auth/login", json={"phone": "13900007002", "password": OLD_PASSWORD}
    )
    assert login.status_code == 200, login.text


def test_reset_password_expired_code(client, make_user):
    make_user("13900007003", password=OLD_PASSWORD)
    sent = _send_reset_code(client, "13900007003")

    # 模拟验证码过期(TTL 到期后 Redis 键消失)
    redis_client.delete("sms:reset:code:13900007003")

    r = client.post(
        "/api/auth/password/reset",
        json={"phone": "13900007003", "sms_code": sent["code"], "new_password": NEW_PASSWORD},
    )
    assert r.status_code == 400
    assert "验证码不存在或已过期" in r.text


def test_reset_sms_send_cooldown(client, make_user):
    make_user("13900007004", password=OLD_PASSWORD)
    _send_reset_code(client, "13900007004")

    again = client.post(
        "/api/auth/sms/send", json={"phone": "13900007004", "purpose": "reset"}
    )
    assert again.status_code == 429, again.text
    detail = again.json()["detail"]
    assert "请稍后再试" in detail["message"]
    assert detail["retry_after"] > 0


def test_reset_attempt_rate_limit_per_phone(client, make_user, monkeypatch):
    make_user("13900007005", password=OLD_PASSWORD)
    monkeypatch.setattr(auth_router, "RESET_ATTEMPT_LIMIT", 3)

    last = None
    for _ in range(4):
        last = client.post(
            "/api/auth/password/reset",
            json={"phone": "13900007005", "sms_code": "123456", "new_password": NEW_PASSWORD},
        )
    assert last.status_code == 429
    assert "操作过于频繁" in last.text


def test_reset_attempt_rate_limit_per_ip(client, make_user, monkeypatch):
    make_user("13900007006", password=OLD_PASSWORD)
    monkeypatch.setattr(auth_router, "RESET_ATTEMPT_IP_LIMIT", 2)

    last = None
    for i in range(3):
        # 每次换手机号,确保是 IP 维度而非手机号维度触发
        last = client.post(
            "/api/auth/password/reset",
            json={"phone": f"1390000710{i}", "sms_code": "123456", "new_password": NEW_PASSWORD},
        )
    assert last.status_code == 429
    assert "操作过于频繁" in last.text


def test_reset_sms_ip_rate_limit(client, monkeypatch):
    monkeypatch.setattr(auth_router, "RESET_SMS_IP_LIMIT", 2)

    last = None
    for i in range(3):
        last = client.post(
            "/api/auth/sms/send", json={"phone": f"1390000720{i}", "purpose": "reset"}
        )
    assert last.status_code == 429
    assert "验证码发送过于频繁" in last.text


def test_reset_does_not_leak_unregistered_phone(client, make_user):
    make_user("13900007007", password=OLD_PASSWORD)
    unregistered = "13900007999"

    # 发码响应结构一致(未注册不真正发码,dev 回显除外)
    registered_resp = _send_reset_code(client, "13900007007")
    unregistered_resp = _send_reset_code(client, unregistered)
    assert unregistered_resp["ok"] is True
    assert unregistered_resp["retry_after"] == registered_resp["retry_after"]
    assert "code" not in unregistered_resp
    assert redis_client.get(f"sms:reset:code:{unregistered}") is None

    # 未注册手机号提交重置:报错与"验证码过期"完全一致,不暴露注册状态
    r = client.post(
        "/api/auth/password/reset",
        json={"phone": unregistered, "sms_code": "123456", "new_password": NEW_PASSWORD},
    )
    assert r.status_code == 400
    assert "验证码不存在或已过期" in r.text

    # 未注册手机号同样受发码冷却约束(节奏差异也不可探测)
    again = client.post(
        "/api/auth/sms/send", json={"phone": unregistered, "purpose": "reset"}
    )
    assert again.status_code == 429


def test_register_code_cannot_be_used_for_reset(client, make_user):
    make_user("13900007008", password=OLD_PASSWORD)
    # 直接通过服务层生成 register 用途验证码(register 用途走旧键位)
    register_code = sms.send_code("13900007008")

    r = client.post(
        "/api/auth/password/reset",
        json={"phone": "13900007008", "sms_code": register_code, "new_password": NEW_PASSWORD},
    )
    assert r.status_code == 400
    assert "验证码不存在或已过期" in r.text


def test_register_purpose_semantics_unchanged(client, make_user):
    """register 用途:已注册手机号仍返回 ok 但不发码(防注册探测)。"""
    from app.db import SessionLocal
    from app.services.config_store import set_setting

    db = SessionLocal()
    try:
        set_setting(db, "sms_auth_enabled", "true")
        db.commit()
    finally:
        db.close()

    make_user("13900007009", password=OLD_PASSWORD)
    r = client.post("/api/auth/sms/send", json={"phone": "13900007009"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert "code" not in body
    assert redis_client.get("sms:code:13900007009") is None


def test_features_exposes_password_reset_flag(client):
    r = client.get("/api/auth/features")
    assert r.status_code == 200
    assert r.json()["password_reset_enabled"] is True  # debug + mock 渠道可用
