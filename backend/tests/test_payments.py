from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.db import SessionLocal
from app.models import PaymentOrder, PaymentProviderConfig
from app.services import payment_config, payments
from app.services.config_store import set_setting
from app.services.user_events import read_user_events


def _set_payment_enabled(enabled: bool = True):
    db = SessionLocal()
    try:
        set_setting(db, "payment_enabled", enabled)
    finally:
        db.close()


def _clear_unpaid_payment_orders():
    db = SessionLocal()
    try:
        db.query(PaymentOrder).filter(PaymentOrder.paid_at.is_(None)).update(
            {PaymentOrder.status: payments.FAILED},
            synchronize_session=False,
        )
        db.commit()
    finally:
        db.close()


def test_mock_payments_only_allowed_on_local_urls(monkeypatch):
    monkeypatch.setattr(settings, "debug", True)
    monkeypatch.setattr(settings, "payment_mock_enabled", True)
    monkeypatch.setattr(settings, "public_base_url", "http://localhost:8000")
    monkeypatch.setattr(settings, "payment_frontend_base_url", "http://127.0.0.1:3000")
    assert payments.mock_payments_allowed() is True

    monkeypatch.setattr(settings, "public_base_url", "https://dream.aiwuq.cn")
    monkeypatch.setattr(settings, "payment_frontend_base_url", "https://dream.aiwuq.cn")
    assert payments.mock_payments_allowed() is False


def test_public_payment_disabled_by_default(client, make_user, auth):
    _set_payment_enabled(False)
    make_user("13900000219", balance=100)
    h = auth("13900000219")

    cfg = client.get("/api/payments/config", headers=h)
    assert cfg.status_code == 200, cfg.text
    assert cfg.json()["enabled"] is False
    assert cfg.json()["packages"] == []
    assert cfg.json()["providers"] == []
    assert client.get("/api/payments/packages", headers=h).json() == []

    r = client.post("/api/payments/orders", json={
        "provider": "alipay",
        "package_id": "starter",
    }, headers=h)
    assert r.status_code == 400
    assert "支付充值功能未开启" in r.text


def test_payment_packages_and_create_order(client, make_user, auth):
    _set_payment_enabled(True)
    make_user("13900000200", balance=100)
    h = auth("13900000200")

    pkgs = client.get("/api/payments/packages", headers=h)
    assert pkgs.status_code == 200
    assert {p["id"] for p in pkgs.json()} >= {"starter", "creator", "pro"}

    r = client.post("/api/payments/orders", json={
        "provider": "alipay",
        "package_id": "starter",
    }, headers=h)
    assert r.status_code == 200, r.text
    order = r.json()
    assert order["status"] == "pending"
    assert order["credits"] == 100
    assert order["amount_cents"] == 990
    assert order["code_url"]

    got = client.get(f"/api/payments/orders/{order['order_no']}", headers=h)
    assert got.status_code == 200
    assert got.json()["order_no"] == order["order_no"]


def test_mock_payment_marks_paid_and_grants_once(client, make_user, auth):
    _set_payment_enabled(True)
    uid = make_user("13900000201", balance=100)
    h = auth("13900000201")

    order = client.post("/api/payments/orders", json={
        "provider": "wechat",
        "package_id": "creator",
    }, headers=h).json()
    before = client.get("/api/me", headers=h).json()["balance_credits"]

    paid1 = client.post(f"/api/payments/orders/{order['order_no']}/mock-pay", headers=h)
    assert paid1.status_code == 200, paid1.text
    assert paid1.json()["status"] == "paid"
    assert client.get("/api/me", headers=h).json()["balance_credits"] == before + 330
    _, events = read_user_events(uid, "0", block_ms=1)
    assert any(
        e["type"] == "payment_paid" and e["payload"].get("order_no") == order["order_no"]
        for e in events
    )

    paid2 = client.post(f"/api/payments/orders/{order['order_no']}/mock-pay", headers=h)
    assert paid2.status_code == 200, paid2.text
    assert client.get("/api/me", headers=h).json()["balance_credits"] == before + 330


def test_paid_order_rejects_mismatched_provider_trade_no(client, make_user, auth):
    _set_payment_enabled(True)
    make_user("13900000242", balance=100)
    h = auth("13900000242")
    order = client.post("/api/payments/orders", json={
        "provider": "alipay",
        "package_id": "starter",
    }, headers=h).json()
    db = SessionLocal()
    try:
        payments.mark_paid(
            db,
            order["order_no"],
            provider="alipay",
            provider_trade_no="ali-original",
            allow_expired=True,
        )
        with pytest.raises(payments.PaymentError, match="流水号.*不匹配"):
            payments.mark_paid(
                db,
                order["order_no"],
                provider="alipay",
                provider_trade_no="ali-other",
                allow_expired=True,
            )
    finally:
        db.close()


def test_payment_order_requires_owner(client, make_user, auth):
    _set_payment_enabled(True)
    make_user("13900000202", balance=100)
    make_user("13900000203", balance=100)
    h1 = auth("13900000202")
    h2 = auth("13900000203")

    order = client.post("/api/payments/orders", json={
        "provider": "alipay",
        "package_id": "starter",
    }, headers=h1).json()

    assert client.get(f"/api/payments/orders/{order['order_no']}", headers=h2).status_code == 404
    assert client.post(f"/api/payments/orders/{order['order_no']}/mock-pay", headers=h2).status_code == 404


def test_create_order_rejects_bad_package(client, make_user, auth):
    _set_payment_enabled(True)
    make_user("13900000204", balance=100)
    h = auth("13900000204")

    r = client.post("/api/payments/orders", json={
        "provider": "alipay",
        "package_id": "missing",
    }, headers=h)
    assert r.status_code == 400
    assert "套餐" in r.text


def test_create_order_keeps_failed_order_when_gateway_fails(client, make_user, auth, monkeypatch):
    _set_payment_enabled(True)
    make_user("13900000214", balance=100)
    h = auth("13900000214")
    monkeypatch.setattr("app.services.payments._provider_code_url", lambda *_a, **_k: (_ for _ in ()).throw(
        payments.PaymentError("gateway down")
    ))

    r = client.post("/api/payments/orders", json={
        "provider": "alipay",
        "package_id": "starter",
    }, headers=h)
    assert r.status_code == 400
    db = SessionLocal()
    try:
        rows = db.query(PaymentOrder).filter(PaymentOrder.user_id.isnot(None)).all()
        assert any(row.status == payments.FAILED and row.raw.get("error") == "gateway down" for row in rows)
    finally:
        db.close()


def test_create_order_clears_orphan_pending_order_without_code_url(client, make_user, auth, monkeypatch):
    _set_payment_enabled(True)
    user_id = make_user("13900000243", balance=100)
    db = SessionLocal()
    try:
        orphan = PaymentOrder(
            order_no="ali-orphan-without-code-url",
            user_id=user_id,
            provider="alipay",
            package_id="starter",
            amount_cents=990,
            credits=100,
            status=payments.PENDING,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=20),
        )
        db.add(orphan)
        db.commit()
    finally:
        db.close()
    h = auth("13900000243")

    r = client.post("/api/payments/orders", json={
        "provider": "alipay",
        "package_id": "starter",
    }, headers=h)

    assert r.status_code == 200, r.text
    db = SessionLocal()
    try:
        orphan = db.query(PaymentOrder).filter(PaymentOrder.order_no == "ali-orphan-without-code-url").one()
        assert orphan.status == payments.FAILED
        assert "二维码未创建完成" in orphan.raw["error"]
        active = db.query(PaymentOrder).filter(
            PaymentOrder.user_id == user_id,
            PaymentOrder.status == payments.PENDING,
        ).all()
        assert len(active) == 1
        assert active[0].code_url
    finally:
        db.close()


def test_alipay_notify_requires_signature_even_when_mock_is_enabled(client, make_user, auth):
    _set_payment_enabled(True)
    make_user("13900000205", balance=100)
    h = auth("13900000205")

    order = client.post("/api/payments/orders", json={
        "provider": "alipay",
        "package_id": "starter",
    }, headers=h).json()
    before = client.get("/api/me", headers=h).json()["balance_credits"]

    unsigned = client.post("/api/payments/alipay/notify", data={
        "out_trade_no": order["order_no"],
        "trade_no": "ali_unsigned",
        "trade_status": "TRADE_SUCCESS",
        "total_amount": "9.90",
    })
    assert unsigned.status_code == 200
    assert unsigned.text == "fail"
    assert client.get("/api/me", headers=h).json()["balance_credits"] == before
    assert client.get(f"/api/payments/orders/{order['order_no']}", headers=h).json()["status"] == "pending"


def test_wechat_notify_requires_signature_even_when_mock_is_enabled(client, make_user, auth):
    _set_payment_enabled(True)
    make_user("13900000206", balance=100)
    h = auth("13900000206")

    order = client.post("/api/payments/orders", json={
        "provider": "wechat",
        "package_id": "creator",
    }, headers=h).json()
    before = client.get("/api/me", headers=h).json()["balance_credits"]

    unsigned = client.post("/api/payments/wechat/notify", json={
        "resource": {
            "out_trade_no": order["order_no"],
            "transaction_id": "wx_unsigned",
            "amount": {"total": 2990, "currency": "CNY"},
        },
    })
    assert unsigned.status_code == 401
    assert client.get("/api/me", headers=h).json()["balance_credits"] == before
    assert client.get(f"/api/payments/orders/{order['order_no']}", headers=h).json()["status"] == "pending"


def test_payment_notify_rejects_oversized_body_before_verify(client, monkeypatch):
    monkeypatch.setattr(settings, "payment_notify_max_body_bytes", 16)
    assert client.post(
        "/api/payments/alipay/notify",
        content=b"x" * 32,
        headers={"content-type": "application/x-www-form-urlencoded"},
    ).status_code == 413
    assert client.post(
        "/api/payments/wechat/notify",
        content=b"x" * 32,
        headers={"content-type": "application/json"},
    ).status_code == 413


def test_payment_notify_raw_payload_is_minimized():
    from app.routers import payments as payment_router

    ali = payment_router._compact_alipay_notify({
        "out_trade_no": "order-1",
        "trade_no": "trade-1",
        "total_amount": "9.90",
        "sign": "secret-signature",
        "buyer_logon_id": "buyer@example.com",
    })
    assert ali == {
        "out_trade_no": "order-1",
        "trade_no": "trade-1",
        "total_amount": "9.90",
    }

    wx = payment_router._compact_wechat_notify(
        {"id": "notify-1", "event_type": "TRANSACTION.SUCCESS", "resource": {"ciphertext": "secret"}},
        {
            "out_trade_no": "order-2",
            "transaction_id": "wx-1",
            "trade_state": "SUCCESS",
            "amount": {"total": 2990, "currency": "CNY"},
            "payer": {"openid": "secret-openid"},
        },
    )
    assert wx["out_trade_no"] == "order-2"
    assert wx["amount"] == {"total": 2990, "currency": "CNY"}
    assert "resource" not in wx
    assert "payer" not in wx


def test_public_payment_config_exposes_mock_ready_providers(client, make_user, auth):
    _set_payment_enabled(True)
    make_user("13900000211", balance=100)
    h = auth("13900000211")
    r = client.get("/api/payments/config", headers=h)
    assert r.status_code == 200, r.text
    providers = {p["provider"]: p for p in r.json()["providers"]}
    assert providers["alipay"]["ready"] is True
    assert providers["wechat"]["ready"] is True


def test_public_payment_config_hides_mock_when_not_allowed(client, make_user, auth, monkeypatch):
    _set_payment_enabled(True)
    monkeypatch.setattr(settings, "payment_mock_enabled", False)
    make_user("13900000220", balance=100)
    h = auth("13900000220")

    r = client.get("/api/payments/config", headers=h)
    assert r.status_code == 200, r.text
    providers = {p["provider"]: p for p in r.json()["providers"]}
    assert providers["alipay"]["enabled"] is False
    assert providers["alipay"]["ready"] is False
    assert providers["wechat"]["enabled"] is False
    assert providers["wechat"]["ready"] is False

    order = client.post("/api/payments/orders", json={
        "provider": "alipay",
        "package_id": "starter",
    }, headers=h)
    assert order.status_code == 400
    assert "支付渠道未启用" in order.text


def test_wechat_notify_rejects_non_success_even_with_valid_signature(client, make_user, auth, monkeypatch):
    _set_payment_enabled(True)
    make_user("13900000212", balance=100)
    h = auth("13900000212")

    order = client.post("/api/payments/orders", json={
        "provider": "wechat",
        "package_id": "creator",
    }, headers=h).json()
    before = client.get("/api/me", headers=h).json()["balance_credits"]
    monkeypatch.setattr("app.services.payments.wechat_signature_valid", lambda db, headers, body: True)

    r = client.post("/api/payments/wechat/notify", json={
        "event_type": "TRANSACTION.SUCCESS",
        "resource": {
            "out_trade_no": order["order_no"],
            "transaction_id": "wx_closed",
            "trade_state": "CLOSED",
            "success_time": "2026-06-18T12:00:00+08:00",
            "amount": {"total": 2990, "currency": "CNY"},
        },
    })
    assert r.status_code == 400
    assert client.get("/api/me", headers=h).json()["balance_credits"] == before
    assert client.get(f"/api/payments/orders/{order['order_no']}", headers=h).json()["status"] == "pending"


def test_alipay_notify_requires_trade_no_after_verified_signature(client, make_user, auth, monkeypatch):
    _set_payment_enabled(True)
    make_user("13900000236", balance=100)
    h = auth("13900000236")
    order = client.post("/api/payments/orders", json={
        "provider": "alipay",
        "package_id": "starter",
    }, headers=h).json()
    before = client.get("/api/me", headers=h).json()["balance_credits"]
    monkeypatch.setattr("app.services.payments._rsa_sha256_verify", lambda *args: True)
    monkeypatch.setattr(
        "app.services.payment_config.runtime_or_env",
        lambda db, provider: payment_config.ProviderRuntimeConfig(
            provider="alipay",
            enabled=True,
            mode="live",
            public={},
            secret={"public_key": "public"},
        ),
    )

    r = client.post("/api/payments/alipay/notify", data={
        "out_trade_no": order["order_no"],
        "trade_status": "TRADE_SUCCESS",
        "total_amount": "9.90",
        "sign": "verified",
    })
    assert r.status_code == 200
    assert r.text == "fail"
    assert client.get("/api/me", headers=h).json()["balance_credits"] == before
    assert client.get(f"/api/payments/orders/{order['order_no']}", headers=h).json()["status"] == "pending"


def test_wechat_notify_requires_transaction_id_after_verified_signature(client, make_user, auth, monkeypatch):
    _set_payment_enabled(True)
    make_user("13900000237", balance=100)
    h = auth("13900000237")
    order = client.post("/api/payments/orders", json={
        "provider": "wechat",
        "package_id": "creator",
    }, headers=h).json()
    before = client.get("/api/me", headers=h).json()["balance_credits"]
    monkeypatch.setattr("app.services.payments.wechat_signature_valid", lambda db, headers, body: True)

    r = client.post("/api/payments/wechat/notify", json={
        "event_type": "TRANSACTION.SUCCESS",
        "resource": {
            "out_trade_no": order["order_no"],
            "trade_state": "SUCCESS",
            "success_time": "2026-06-18T12:00:00+08:00",
            "amount": {"total": 2990, "currency": "CNY"},
        },
    })
    assert r.status_code == 400
    assert "第三方流水号" in r.text
    assert client.get("/api/me", headers=h).json()["balance_credits"] == before
    assert client.get(f"/api/payments/orders/{order['order_no']}", headers=h).json()["status"] == "pending"


def test_wechat_signature_requires_configured_platform_serial(monkeypatch):
    db = SessionLocal()
    try:
        monkeypatch.setattr(
            "app.services.payment_config.runtime_or_env",
            lambda db, provider: payment_config.ProviderRuntimeConfig(
                provider="wechat",
                enabled=True,
                mode="live",
                public={"platform_serial_no": "expected"},
                secret={"platform_cert_pem": "dummy"},
            ),
        )
        monkeypatch.setattr("app.services.payments._rsa_sha256_verify", lambda *args: True)
        headers = {
            "Wechatpay-Signature": "sig",
            "Wechatpay-Timestamp": "1",
            "Wechatpay-Nonce": "nonce",
        }
        assert payments.wechat_signature_valid(db, headers, b"{}") is False
    finally:
        db.close()


def test_alipay_notify_requires_configured_app_id(monkeypatch):
    db = SessionLocal()
    try:
        monkeypatch.setattr(
            "app.services.payment_config.runtime_or_env",
            lambda db, provider: payment_config.ProviderRuntimeConfig(
                provider="alipay",
                enabled=True,
                mode="live",
                public={"app_id": "expected-app"},
                secret={"public_key": "dummy"},
            ),
        )
        with pytest.raises(payments.PaymentError, match="缺少 app_id"):
            payments.verify_alipay_notify(db, {
                "out_trade_no": "order-1",
                "trade_status": "TRADE_SUCCESS",
                "total_amount": "9.90",
                "sign": "sig",
            })
    finally:
        db.close()


def test_alipay_notify_requires_configured_seller_id(monkeypatch):
    db = SessionLocal()
    try:
        monkeypatch.setattr(
            "app.services.payment_config.runtime_or_env",
            lambda db, provider: payment_config.ProviderRuntimeConfig(
                provider="alipay",
                enabled=True,
                mode="live",
                public={"app_id": "expected-app", "seller_id": "seller-001"},
                secret={"public_key": "dummy"},
            ),
        )
        with pytest.raises(payments.PaymentError, match="缺少 seller_id"):
            payments.verify_alipay_notify(db, {
                "out_trade_no": "order-1",
                "trade_status": "TRADE_SUCCESS",
                "total_amount": "9.90",
                "app_id": "expected-app",
                "sign": "sig",
            })
        with pytest.raises(payments.PaymentError, match="seller_id 不匹配"):
            payments.verify_alipay_notify(db, {
                "out_trade_no": "order-1",
                "trade_status": "TRADE_SUCCESS",
                "total_amount": "9.90",
                "app_id": "expected-app",
                "seller_id": "seller-002",
                "sign": "sig",
            })
    finally:
        db.close()


def test_wechat_notify_requires_configured_merchant_fields(monkeypatch):
    db = SessionLocal()
    try:
        monkeypatch.setattr(
            "app.services.payment_config.runtime_or_env",
            lambda db, provider: payment_config.ProviderRuntimeConfig(
                provider="wechat",
                enabled=True,
                mode="live",
                public={"appid": "wx-app", "mchid": "mch-id"},
                secret={},
            ),
        )
        with pytest.raises(payments.PaymentError, match="缺少 appid"):
            payments.ensure_wechat_merchant_matches(db, {"mchid": "mch-id"})
        with pytest.raises(payments.PaymentError, match="缺少 mchid"):
            payments.ensure_wechat_merchant_matches(db, {"appid": "wx-app"})
    finally:
        db.close()


def test_env_provider_fallback_requires_callback_credentials(client, monkeypatch):
    monkeypatch.setattr(settings, "alipay_app_id", "app")
    monkeypatch.setattr(settings, "alipay_private_key", "private")
    monkeypatch.setattr(settings, "alipay_public_key", "")
    db = SessionLocal()
    try:
        cfg = payment_config.runtime_or_env(db, "alipay")
        assert cfg.enabled is False

        monkeypatch.setattr(settings, "wechat_pay_appid", "wx")
        monkeypatch.setattr(settings, "wechat_pay_mchid", "mch")
        monkeypatch.setattr(settings, "wechat_pay_serial_no", "serial")
        monkeypatch.setattr(settings, "wechat_pay_private_key", "private")
        monkeypatch.setattr(settings, "wechat_pay_api_v3_key", "")
        monkeypatch.setattr(settings, "wechat_pay_platform_cert_pem", "cert")
        cfg = payment_config.runtime_or_env(db, "wechat")
        assert cfg.enabled is False
    finally:
        db.close()


def test_admin_payment_providers_expose_env_runtime_without_empty_form(client, make_user, auth, monkeypatch):
    make_user("13900000221", balance=100, admin=True)
    h = auth("13900000221")
    monkeypatch.setattr(settings, "public_base_url", "https://dream.aiwuq.cn")
    monkeypatch.setattr(settings, "alipay_app_id", "ali-app")
    monkeypatch.setattr(settings, "alipay_seller_id", "seller-1")
    monkeypatch.setattr(settings, "alipay_gateway_url", "https://openapi.alipay.com/gateway.do")
    monkeypatch.setattr(settings, "alipay_notify_url", "")
    monkeypatch.setattr(settings, "alipay_private_key", "private")
    monkeypatch.setattr(settings, "alipay_public_key", "public")

    r = client.get("/api/admin/payments/providers", headers=h)

    assert r.status_code == 200, r.text
    providers = {p["provider"]: p for p in r.json()}
    alipay = providers["alipay"]
    assert alipay["source"] == "env"
    assert alipay["ready"] is True
    assert alipay["public_config"]["app_id"] == "ali-app"
    assert alipay["public_config"]["seller_id"] == "seller-1"
    assert alipay["secret_config_masked"]["private_key"] == "已配置"
    assert alipay["secret_config_masked"]["public_key"] == "已配置"


def test_live_provider_requires_effective_https_notify_url(client, monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "http://localhost:8000")
    issues = payment_config.validate_provider(
        "alipay",
        {
            "app_id": "app",
            "gateway_url": "https://openapi.alipay.com/gateway.do",
            # notify_url intentionally omitted: validator must check the
            # effective PUBLIC_BASE_URL + notify path, not skip validation.
        },
        {"private_key": "private", "public_key": "public"},
        enabled=True,
        mode="live",
    )
    assert "支付回调地址必须使用 HTTPS" in issues


def test_mark_paid_rejects_wrong_provider_and_expired_orders(client, make_user, auth):
    _set_payment_enabled(True)
    make_user("13900000207", balance=100)
    h = auth("13900000207")
    order = client.post("/api/payments/orders", json={
        "provider": "alipay",
        "package_id": "starter",
    }, headers=h).json()

    db = SessionLocal()
    try:
        with pytest.raises(payments.PaymentError, match="渠道"):
            payments.mark_paid(db, order["order_no"], provider="wechat")

        row = db.query(PaymentOrder).filter(PaymentOrder.order_no == order["order_no"]).one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
        with pytest.raises(payments.PaymentError, match="过期"):
            payments.mark_paid(db, order["order_no"], provider="alipay")
        db.refresh(row)
        assert row.status == payments.CLOSED
    finally:
        db.close()

    assert client.get("/api/me", headers=h).json()["balance_credits"] == 100


def test_mark_paid_requires_provider_trade_no_before_crediting(client, make_user, auth):
    _set_payment_enabled(True)
    make_user("13900000235", balance=100)
    h = auth("13900000235")
    order = client.post("/api/payments/orders", json={
        "provider": "alipay",
        "package_id": "starter",
    }, headers=h).json()

    db = SessionLocal()
    try:
        with pytest.raises(payments.PaymentError, match="第三方流水号"):
            payments.mark_paid(
                db,
                order["order_no"],
                provider="alipay",
                provider_trade_no="  ",
                allow_expired=True,
            )
        row = db.query(PaymentOrder).filter(PaymentOrder.order_no == order["order_no"]).one()
        assert row.status == payments.PENDING
        assert row.provider_trade_no is None
    finally:
        db.close()

    assert client.get("/api/me", headers=h).json()["balance_credits"] == 100


def test_mark_paid_translates_duplicate_provider_trade_no_integrity_error(client, make_user, auth):
    _set_payment_enabled(True)
    make_user("13900000240", balance=100)
    h = auth("13900000240")
    order = client.post("/api/payments/orders", json={
        "provider": "alipay",
        "package_id": "starter",
    }, headers=h).json()

    db = SessionLocal()
    original_commit = db.commit
    calls = {"n": 0}

    def flaky_commit():
        calls["n"] += 1
        if calls["n"] == 1:
            raise payments.IntegrityError(
                "INSERT",
                {},
                Exception("UNIQUE constraint failed: uq_payment_orders_provider_trade_no"),
            )
        return original_commit()

    try:
        db.commit = flaky_commit
        with pytest.raises(payments.PaymentError, match="流水号已入账"):
            payments.mark_paid(
                db,
                order["order_no"],
                provider="alipay",
                provider_trade_no="ali_duplicate_race",
                allow_expired=True,
            )
        db.commit = original_commit
        row = db.query(PaymentOrder).filter(PaymentOrder.order_no == order["order_no"]).one()
        assert row.status == payments.PENDING
    finally:
        db.commit = original_commit
        db.close()


def test_verified_provider_notify_can_credit_expired_order(client, make_user, auth):
    _set_payment_enabled(True)
    make_user("13900000215", balance=100)
    h = auth("13900000215")
    order = client.post("/api/payments/orders", json={
        "provider": "alipay",
        "package_id": "starter",
    }, headers=h).json()

    db = SessionLocal()
    try:
        row = db.query(PaymentOrder).filter(PaymentOrder.order_no == order["order_no"]).one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
        paid, credited = payments.mark_paid(
            db,
            order["order_no"],
            provider="alipay",
            provider_trade_no="ali_delayed_success",
            raw={"verified_provider_notify": True},
            allow_expired=True,
        )
        db.refresh(row)
        assert paid.status == payments.PAID
        assert credited is True
        assert row.status == payments.PAID
    finally:
        db.close()

    assert client.get("/api/me", headers=h).json()["balance_credits"] == 200


def test_verified_provider_notify_can_credit_locally_closed_order(client, make_user, auth):
    _set_payment_enabled(True)
    make_user("13900000216", balance=100)
    h = auth("13900000216")
    order = client.post("/api/payments/orders", json={
        "provider": "alipay",
        "package_id": "starter",
    }, headers=h).json()

    db = SessionLocal()
    try:
        row = db.query(PaymentOrder).filter(PaymentOrder.order_no == order["order_no"]).one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    finally:
        db.close()

    closed = client.get(f"/api/payments/orders/{order['order_no']}", headers=h)
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == payments.CLOSED

    db = SessionLocal()
    try:
        paid, credited = payments.mark_paid(
            db,
            order["order_no"],
            provider="alipay",
            provider_trade_no="ali_delayed_after_local_close",
            raw={"verified_provider_notify": True},
            allow_expired=True,
        )
        assert paid.status == payments.PAID
        assert credited is True
    finally:
        db.close()

    assert client.get("/api/me", headers=h).json()["balance_credits"] == 200


def test_list_orders_reports_expired_pending_order_without_writing_on_get(client, make_user, auth):
    _set_payment_enabled(True)
    make_user("13900000217", balance=100)
    h = auth("13900000217")
    order = client.post("/api/payments/orders", json={
        "provider": "wechat",
        "package_id": "starter",
    }, headers=h).json()
    db = SessionLocal()
    try:
        row = db.query(PaymentOrder).filter(PaymentOrder.order_no == order["order_no"]).one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    finally:
        db.close()

    rows = client.get("/api/payments/orders", headers=h)

    assert rows.status_code == 200, rows.text
    found = next(o for o in rows.json() if o["order_no"] == order["order_no"])
    assert found["status"] == payments.CLOSED

    db = SessionLocal()
    try:
        persisted = db.query(PaymentOrder).filter(PaymentOrder.order_no == order["order_no"]).one()
        assert persisted.status == payments.PENDING
    finally:
        db.close()


def test_get_order_reports_expired_pending_order_without_writing_on_get(client, make_user, auth):
    _set_payment_enabled(True)
    make_user("13900000251", balance=100)
    h = auth("13900000251")
    order = client.post("/api/payments/orders", json={
        "provider": "wechat",
        "package_id": "starter",
    }, headers=h).json()
    db = SessionLocal()
    try:
        row = db.query(PaymentOrder).filter(PaymentOrder.order_no == order["order_no"]).one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    finally:
        db.close()

    detail = client.get(f"/api/payments/orders/{order['order_no']}", headers=h)

    assert detail.status_code == 200, detail.text
    assert detail.json()["status"] == payments.CLOSED
    db = SessionLocal()
    try:
        persisted = db.query(PaymentOrder).filter(PaymentOrder.order_no == order["order_no"]).one()
        assert persisted.status == payments.PENDING
    finally:
        db.close()


def test_admin_can_customize_payment_package(client, make_user, auth):
    _set_payment_enabled(True)
    make_user("13900000208", balance=100, admin=True)
    make_user("13900000209", balance=100)
    admin_h = auth("13900000208")
    user_h = auth("13900000209")

    r = client.post("/api/admin/payments/packages", json={
        "id": "custom_1",
        "title": "自定义包",
        "amount_cents": 1234,
        "credits": 567,
        "badge": "测试",
        "enabled": True,
        "sort_order": 1,
        "admin_password": "pass123456",
    }, headers=admin_h)
    assert r.status_code == 200, r.text

    pkgs = client.get("/api/payments/packages", headers=user_h).json()
    assert any(p["id"] == "custom_1" and p["amount_cents"] == 1234 for p in pkgs)

    order = client.post("/api/payments/orders", json={
        "provider": "alipay",
        "package_id": "custom_1",
    }, headers=user_h)
    assert order.status_code == 200, order.text
    assert order.json()["amount_cents"] == 1234
    assert order.json()["credits"] == 567


def test_admin_payment_package_allows_logged_in_admin_without_second_password(client, make_user, auth):
    make_user("13900000218", balance=100, admin=True)
    h = auth("13900000218")

    r = client.post("/api/admin/payments/packages", json={
        "id": "custom_no_password",
        "title": "无二次校验包",
        "amount_cents": 1234,
        "credits": 567,
        "enabled": True,
    }, headers=h)
    assert r.status_code == 200, r.text

    disabled = client.request(
        "DELETE",
        "/api/admin/payments/packages/custom_no_password",
        json={},
        headers=h,
    )
    assert disabled.status_code == 200, disabled.text


def test_admin_payment_package_rejects_extreme_values(client, make_user, auth):
    make_user("13900000239", balance=100, admin=True)
    h = auth("13900000239")

    huge_ratio = client.post("/api/admin/payments/packages", json={
        "id": "too_generous",
        "title": "异常比例",
        "amount_cents": 1,
        "credits": 100_001,
        "enabled": True,
        "admin_password": "pass123456",
    }, headers=h)
    assert huge_ratio.status_code == 422

    huge_amount = client.post("/api/admin/payments/packages", json={
        "id": "too_expensive",
        "title": "异常金额",
        "amount_cents": 100_000_001,
        "credits": 1,
        "enabled": True,
        "admin_password": "pass123456",
    }, headers=h)
    assert huge_amount.status_code == 422


def test_admin_payment_provider_config_is_masked_and_validated(client, make_user, auth):
    make_user("13900000210", balance=100, admin=True)
    h = auth("13900000210")

    bad = client.put("/api/admin/payments/providers/wechat", json={
        "provider": "wechat",
        "enabled": True,
        "mode": "live",
        "public_config": {"appid": "wx", "mchid": "mch", "serial_no": "serial"},
        "secret_config": {"api_v3_key": "too-short"},
        "admin_password": "pass123456",
    }, headers=h)
    assert bad.status_code == 400

    ok = client.put("/api/admin/payments/providers/alipay", json={
        "provider": "alipay",
        "enabled": True,
        "mode": "mock",
        "public_config": {"app_id": "ali-app"},
        "secret_config": {"private_key": "secret-private", "public_key": "secret-public"},
        "admin_password": "pass123456",
    }, headers=h)
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["public_config"]["app_id"] == "ali-app"
    assert body["secret_config_masked"]["private_key"] == "已配置"
    assert "secret-private" not in ok.text

    cfg = client.get("/api/admin/payments/config", headers=h)
    assert cfg.status_code == 200
    assert "secret-private" not in cfg.text


def test_admin_payment_provider_audit_records_before_after_without_secret(client, make_user, auth):
    make_user("13900009211", balance=100, admin=True)
    h = auth("13900009211")
    first = client.put("/api/admin/payments/providers/alipay", json={
        "provider": "alipay",
        "enabled": False,
        "mode": "mock",
        "public_config": {"app_id": "ali-old"},
        "secret_config": {"private_key": "old-private", "public_key": "old-public"},
        "admin_password": "pass123456",
    }, headers=h)
    assert first.status_code == 200, first.text

    changed = client.put("/api/admin/payments/providers/alipay", json={
        "provider": "alipay",
        "enabled": True,
        "mode": "mock",
        "public_config": {"app_id": "ali-new"},
        "secret_config": {"private_key": "new-private", "public_key": "__keep__"},
        "admin_password": "pass123456",
    }, headers=h)
    assert changed.status_code == 200, changed.text

    audit_log = client.get("/api/admin/audit?action=update_payment_provider", headers=h)
    assert audit_log.status_code == 200, audit_log.text
    for secret in ("old-private", "old-public", "new-private"):
        assert secret not in audit_log.text
    detail = audit_log.json()[0]["detail"]
    assert detail["provider"] == "alipay"
    assert detail["secret_keys_changed"] == ["private_key"]
    assert detail["before"]["enabled"] is False
    assert detail["before"]["public_config"]["app_id"] == "ali-old"
    assert detail["before"]["secret_configured"] == {
        "private_key": True,
        "public_key": True,
    }
    assert detail["after"]["enabled"] is True
    assert detail["after"]["public_config"]["app_id"] == "ali-new"
    assert detail["after"]["secret_configured"] == {
        "private_key": True,
        "public_key": True,
    }


def test_admin_payment_provider_rejects_secret_in_public_config(client, make_user, auth):
    make_user("13900000230", balance=100, admin=True)
    h = auth("13900000230")

    r = client.put("/api/admin/payments/providers/alipay", json={
        "provider": "alipay",
        "enabled": True,
        "mode": "mock",
        "public_config": {"app_id": "ali-app", "private_key": "leaked-secret"},
        "secret_config": {"private_key": "real-secret", "public_key": "real-public"},
        "admin_password": "pass123456",
    }, headers=h)
    assert r.status_code == 400
    assert "private_key" in r.text
    assert "leaked-secret" not in r.text

    providers = client.get("/api/admin/payments/providers", headers=h)
    assert providers.status_code == 200
    assert "leaked-secret" not in providers.text

    audit_log = client.get("/api/admin/audit?action=update_payment_provider", headers=h)
    assert audit_log.status_code == 200
    assert "leaked-secret" not in audit_log.text

    db = SessionLocal()
    try:
        row = db.get(PaymentProviderConfig, "alipay")
        assert not row or "leaked-secret" not in str(row.public_config or {})
    finally:
        db.close()


def test_saved_provider_config_is_used_at_runtime(client, make_user, auth):
    make_user("13900000223", balance=100, admin=True)
    h = auth("13900000223")

    r = client.put("/api/admin/payments/providers/alipay", json={
        "provider": "alipay",
        "enabled": True,
        "mode": "mock",
        "public_config": {"app_id": "runtime-app"},
        "secret_config": {"private_key": "runtime-private", "public_key": "runtime-public"},
        "admin_password": "pass123456",
    }, headers=h)
    assert r.status_code == 200, r.text

    db = SessionLocal()
    try:
        cfg = payment_config.runtime_or_env(db, "alipay")
        assert cfg.source == "db"
        assert cfg.enabled is True
        assert cfg.mode == "mock"
        assert cfg.public["app_id"] == "runtime-app"
        assert cfg.secret["private_key"] == "runtime-private"
        assert cfg.secret["public_key"] == "runtime-public"
    finally:
        db.close()


def test_saved_disabled_provider_shadows_ready_env(client, make_user, auth, monkeypatch):
    make_user("13900000227", balance=100, admin=True)
    h = auth("13900000227")
    monkeypatch.setattr(settings, "public_base_url", "https://dream.aiwuq.cn")
    monkeypatch.setattr(settings, "alipay_app_id", "env-app")
    monkeypatch.setattr(settings, "alipay_seller_id", "env-seller")
    monkeypatch.setattr(settings, "alipay_private_key", "env-private")
    monkeypatch.setattr(settings, "alipay_public_key", "env-public")

    r = client.put("/api/admin/payments/providers/alipay", json={
        "provider": "alipay",
        "enabled": False,
        "mode": "mock",
        "public_config": {},
        "secret_config": {},
        "admin_password": "pass123456",
    }, headers=h)
    assert r.status_code == 200, r.text
    assert "_admin_override" not in r.text

    db = SessionLocal()
    try:
        cfg = payment_config.runtime_or_env(db, "alipay")
        assert cfg.source == "db"
        assert cfg.enabled is False
    finally:
        db.close()


def test_bad_payment_config_secret_does_not_break_provider_listing(client, make_user, auth, monkeypatch):
    make_user("13900000228", balance=100, admin=True)
    h = auth("13900000228")

    r = client.put("/api/admin/payments/providers/alipay", json={
        "provider": "alipay",
        "enabled": True,
        "mode": "mock",
        "public_config": {"app_id": "ali-app"},
        "secret_config": {"private_key": "secret-private", "public_key": "secret-public"},
        "admin_password": "pass123456",
    }, headers=h)
    assert r.status_code == 200, r.text
    monkeypatch.setattr(settings, "payment_config_secret", "different-secret-32-bytes-minimum")

    listed = client.get("/api/admin/payments/providers", headers=h)
    assert listed.status_code == 200, listed.text
    alipay = next(p for p in listed.json() if p["provider"] == "alipay")
    assert alipay["ready"] is False
    assert any("解密失败" in issue for issue in alipay["issues"])


def test_seeded_empty_db_provider_does_not_shadow_ready_env(client, monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://dream.aiwuq.cn")
    monkeypatch.setattr(settings, "alipay_app_id", "env-app")
    monkeypatch.setattr(settings, "alipay_seller_id", "env-seller")
    monkeypatch.setattr(settings, "alipay_private_key", "env-private")
    monkeypatch.setattr(settings, "alipay_public_key", "env-public")

    db = SessionLocal()
    try:
        row = db.get(PaymentProviderConfig, "alipay")
        if not row:
            row = PaymentProviderConfig(provider="alipay")
            db.add(row)
        row.enabled = False
        row.mode = "mock"
        row.public_config = {}
        row.secret_config = {}
        db.commit()

        cfg = payment_config.runtime_or_env(db, "alipay")
        assert cfg.source == "env"
        assert cfg.enabled is True
        assert cfg.mode == "live"
        assert cfg.public["app_id"] == "env-app"
    finally:
        db.close()


def test_mark_paid_rejects_duplicate_provider_trade_no(client, make_user, auth):
    _set_payment_enabled(True)
    make_user("13900000229", balance=100)
    h = auth("13900000229")
    first = client.post("/api/payments/orders", json={
        "provider": "alipay",
        "package_id": "starter",
    }, headers=h).json()
    second = client.post("/api/payments/orders", json={
        "provider": "alipay",
        "package_id": "creator",
    }, headers=h).json()

    db = SessionLocal()
    try:
        payments.mark_paid(
            db,
            first["order_no"],
            provider="alipay",
            provider_trade_no="same-trade-no",
            allow_expired=True,
        )
        with pytest.raises(payments.PaymentError, match="流水号"):
            payments.mark_paid(
                db,
                second["order_no"],
                provider="alipay",
                provider_trade_no="same-trade-no",
                allow_expired=True,
            )
    finally:
        db.close()


def test_payment_reconcile_marks_paid_pending_order(client, make_user, auth, monkeypatch):
    _set_payment_enabled(True)
    _clear_unpaid_payment_orders()
    make_user("13900000231", balance=100)
    h = auth("13900000231")
    order = client.post("/api/payments/orders", json={
        "provider": "alipay",
        "package_id": "starter",
    }, headers=h).json()

    monkeypatch.setattr("app.services.payments._query_provider_order", lambda db, row: {
        "status": payments.PAID,
        "provider_trade_no": "ali_reconcile_paid",
        "raw": {"trade_status": "TRADE_SUCCESS"},
    })
    db = SessionLocal()
    try:
        stats = payments.reconcile_pending_orders(db)
        assert stats["checked"] == 1
        assert stats["paid"] == 1
        row = db.query(PaymentOrder).filter(PaymentOrder.order_no == order["order_no"]).one()
        assert row.status == payments.PAID
        assert row.provider_trade_no == "ali_reconcile_paid"
        assert row.raw["verified_provider_query"] is True

        stats2 = payments.reconcile_pending_orders(db)
        assert stats2["paid"] == 0
    finally:
        db.close()

    assert client.get("/api/me", headers=h).json()["balance_credits"] == 200


def test_payment_reconcile_can_credit_locally_closed_order(client, make_user, auth, monkeypatch):
    _set_payment_enabled(True)
    _clear_unpaid_payment_orders()
    make_user("13900000232", balance=100)
    h = auth("13900000232")
    order = client.post("/api/payments/orders", json={
        "provider": "wechat",
        "package_id": "creator",
    }, headers=h).json()
    db = SessionLocal()
    try:
        row = db.query(PaymentOrder).filter(PaymentOrder.order_no == order["order_no"]).one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    finally:
        db.close()

    assert client.get(f"/api/payments/orders/{order['order_no']}", headers=h).json()["status"] == payments.CLOSED

    monkeypatch.setattr("app.services.payments._query_provider_order", lambda db, row: {
        "status": payments.PAID,
        "provider_trade_no": "wx_reconcile_late_success",
        "raw": {"trade_state": "SUCCESS"},
    })
    db = SessionLocal()
    try:
        stats = payments.reconcile_pending_orders(db)
        assert stats["checked"] == 1
        assert stats["paid"] == 1
        row = db.query(PaymentOrder).filter(PaymentOrder.order_no == order["order_no"]).one()
        assert row.status == payments.PAID
    finally:
        db.close()

    assert client.get("/api/me", headers=h).json()["balance_credits"] == 430


def test_payment_reconcile_closes_provider_closed_pending_order(client, make_user, auth, monkeypatch):
    _set_payment_enabled(True)
    _clear_unpaid_payment_orders()
    make_user("13900000233", balance=100)
    h = auth("13900000233")
    order = client.post("/api/payments/orders", json={
        "provider": "alipay",
        "package_id": "starter",
    }, headers=h).json()
    monkeypatch.setattr("app.services.payments._query_provider_order", lambda db, row: {
        "status": payments.CLOSED,
        "raw": {"trade_status": "TRADE_CLOSED"},
    })

    db = SessionLocal()
    try:
        stats = payments.reconcile_pending_orders(db)
        assert stats["checked"] == 1
        assert stats["closed"] == 1
        row = db.query(PaymentOrder).filter(PaymentOrder.order_no == order["order_no"]).one()
        assert row.status == payments.CLOSED
        assert row.raw["query"]["trade_status"] == "TRADE_CLOSED"
    finally:
        db.close()

    assert client.get("/api/me", headers=h).json()["balance_credits"] == 100


def test_payment_reconcile_records_errors_without_crashing(client, make_user, auth, monkeypatch):
    _set_payment_enabled(True)
    _clear_unpaid_payment_orders()
    make_user("13900000234", balance=100)
    h = auth("13900000234")
    order = client.post("/api/payments/orders", json={
        "provider": "alipay",
        "package_id": "starter",
    }, headers=h).json()

    def _boom(db, row):
        raise payments.PaymentError("provider unavailable")

    monkeypatch.setattr("app.services.payments._query_provider_order", _boom)
    db = SessionLocal()
    try:
        stats = payments.reconcile_pending_orders(db)
        assert stats["checked"] == 0
        assert stats["errors"] == 1
        row = db.query(PaymentOrder).filter(PaymentOrder.order_no == order["order_no"]).one()
        assert row.status == payments.PENDING
    finally:
        db.close()


def test_admin_payment_provider_rejects_unofficial_gateway_url(client, make_user, auth):
    make_user("13900000213", balance=100, admin=True)
    h = auth("13900000213")

    r = client.put("/api/admin/payments/providers/alipay", json={
        "provider": "alipay",
        "enabled": True,
        "mode": "live",
        "public_config": {
            "app_id": "ali-app",
            "seller_id": "seller-001",
            "gateway_url": "http://127.0.0.1:9000/gateway.do",
        },
        "secret_config": {"private_key": "secret-private", "public_key": "secret-public"},
        "admin_password": "pass123456",
    }, headers=h)
    assert r.status_code == 400
    assert "官方 HTTPS" in r.text


def test_admin_payment_provider_rejects_external_notify_url(client, make_user, auth):
    make_user("13900000226", balance=100, admin=True)
    h = auth("13900000226")

    r = client.put("/api/admin/payments/providers/alipay", json={
        "provider": "alipay",
        "enabled": True,
        "mode": "live",
        "public_config": {
            "app_id": "ali-app",
            "seller_id": "seller-001",
            "gateway_url": "https://openapi.alipay.com/gateway.do",
            "notify_url": "https://evil.example.com/api/payments/alipay/notify",
        },
        "secret_config": {"private_key": "secret-private", "public_key": "secret-public"},
        "admin_password": "pass123456",
    }, headers=h)
    assert r.status_code == 400
    assert "同域" in r.text


def test_admin_payment_provider_allows_logged_in_admin_without_second_password(client, make_user, auth):
    make_user("13900000243", balance=100, admin=True)
    h = auth("13900000243")

    r = client.put("/api/admin/payments/providers/alipay", json={
        "provider": "alipay",
        "enabled": True,
        "mode": "mock",
        "public_config": {"app_id": "ali-app"},
        "secret_config": {"private_key": "secret-private", "public_key": "secret-public"},
    }, headers=h)
    assert r.status_code == 200, r.text


def test_alipay_live_provider_requires_seller_id():
    issues = payment_config.validate_provider(
        "alipay",
        {
            "app_id": "app",
            "gateway_url": "https://openapi.alipay.com/gateway.do",
            "notify_url": "https://dream.aiwuq.cn/api/payments/alipay/notify",
        },
        {"private_key": "private", "public_key": "public"},
        enabled=True,
        mode="live",
    )
    assert "支付宝商户 PID / seller_id 未配置" in issues


def test_wechat_live_provider_requires_platform_serial_no():
    issues = payment_config.validate_provider(
        "wechat",
        {
            "appid": "wx-app",
            "mchid": "mch",
            "serial_no": "merchant-serial",
            "gateway_url": "https://api.mch.weixin.qq.com",
            "notify_url": "https://dream.aiwuq.cn/api/payments/wechat/notify",
        },
        {
            "private_key": "private",
            "api_v3_key": "x" * 32,
            "platform_cert_pem": "cert",
        },
        enabled=True,
        mode="live",
    )
    assert "微信平台证书序列号 未配置" in issues
