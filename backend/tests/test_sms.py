import httpx

from app.config import settings
from app.redis_client import redis_client
from app.services import sms


def test_http_sms_provider_posts_code(monkeypatch):
    seen = {}

    class FakePinnedClient:
        def __init__(self, url, *, timeout):
            seen["pinned_url"] = url
            seen["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url, *, headers, json):
            seen.update({"url": url, "headers": headers, "json": json})
            return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(settings, "sms_provider", "http")
    monkeypatch.setattr(settings, "sms_http_url", "https://sms.example.com/send")
    monkeypatch.setattr(settings, "sms_http_api_key", "secret-token")
    monkeypatch.setattr(settings, "sms_sign_name", "sign")
    monkeypatch.setattr(settings, "sms_template_code", "tpl")
    monkeypatch.setattr(sms, "pinned_client", FakePinnedClient)

    sms._dispatch("13900009999", "123456")

    assert seen["pinned_url"] == "https://sms.example.com/send"
    assert seen["timeout"] == settings.sms_http_timeout_seconds
    assert seen["url"] == "https://sms.example.com/send"
    assert seen["headers"]["Authorization"] == "Bearer secret-token"
    assert seen["json"] == {
        "phone": "13900009999",
        "code": "123456",
        "sign_name": "sign",
        "template_code": "tpl",
    }


def test_http_sms_provider_requires_url(monkeypatch):
    monkeypatch.setattr(settings, "sms_provider", "http")
    monkeypatch.setattr(settings, "sms_http_url", "")
    try:
        sms._dispatch("13900009999", "123456")
    except sms.SmsError as e:
        assert "SMS_HTTP_URL" in str(e)
    else:
        raise AssertionError("missing SMS_HTTP_URL must fail")


def test_send_failure_rolls_back_code_and_rate_state(monkeypatch):
    phone = "13900009998"
    redis_client.delete(
        f"sms:code:{phone}",
        f"sms:cooldown:{phone}",
        f"sms:hourly:{phone}",
        f"sms:fail:{phone}",
    )
    monkeypatch.setattr(settings, "sms_provider", "http")
    monkeypatch.setattr(settings, "sms_http_url", "")

    try:
        sms.send_code(phone)
    except sms.SmsError:
        pass
    else:
        raise AssertionError("missing SMS_HTTP_URL must fail")

    assert sms.can_send(phone) == (True, 0)
    assert redis_client.get(f"sms:code:{phone}") is None
    assert redis_client.get(f"sms:hourly:{phone}") is None


def test_send_code_reserves_cooldown_before_dispatch(monkeypatch):
    phone = "13900009997"
    redis_client.delete(
        f"sms:code:{phone}",
        f"sms:cooldown:{phone}",
        f"sms:hourly:{phone}",
        f"sms:fail:{phone}",
    )

    def dispatch_with_concurrent_attempt(_phone, _code):
        try:
            sms.send_code(phone)
        except sms.SmsError as e:
            assert "请稍后再试" in str(e)
        else:
            raise AssertionError("concurrent send should hit reserved cooldown")

    monkeypatch.setattr(settings, "sms_provider", "mock")
    monkeypatch.setattr(sms, "_dispatch", dispatch_with_concurrent_attempt)

    code = sms.send_code(phone)
    assert len(code) == 6
    assert redis_client.get(f"sms:code:{phone}") == code
    assert int(redis_client.get(f"sms:hourly:{phone}") or 0) == 1


def test_implemented_sms_providers_are_explicit():
    assert sms.implemented_providers() == {"mock", "http"}
