"""content_safety 单测:文本禁词 kill-switch 锁行为 + 图像/视频机审接入层."""
import httpx
import pytest
from fastapi import HTTPException

from app.db import SessionLocal
from app.models import AuditLog
from app.services import content_safety as cs
from app.services import content_safety_providers as csp
from app.services.config_store import set_setting


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def restore_media_settings(db):
    yield
    set_setting(db, "media_moderation_enabled", False)
    set_setting(db, "media_moderation_fail_open", False)


@pytest.fixture()
def restore_text_settings(db):
    yield
    set_setting(db, "content_safety_enabled", False)
    set_setting(db, "content_safety_banned_terms", "")


# ---------------------------------------------------------------------------
# 文本禁词:锁住既有 kill-switch 语义(默认关闭 + 空词表刻意直通)
# ---------------------------------------------------------------------------


def test_text_gate_disabled_by_default_even_with_terms(db, restore_text_settings):
    set_setting(db, "content_safety_banned_terms", "locked-term")
    # 未开启总开关:即使配置了词表也必须直通
    cs.assert_text_allowed(db, "prompt mentioning locked-term")


def test_text_gate_enabled_with_empty_terms_is_a_noop(db, restore_text_settings):
    set_setting(db, "content_safety_enabled", True)
    set_setting(db, "content_safety_banned_terms", "")
    # 开启但空词表:刻意的 kill-switch 设计,必须直通
    cs.assert_text_allowed(db, "anything at all")


def test_text_gate_blocks_with_existing_message(db, restore_text_settings):
    set_setting(db, "content_safety_enabled", True)
    set_setting(db, "content_safety_banned_terms", "locked-term\nanother-term")
    with pytest.raises(HTTPException) as exc:
        cs.assert_text_allowed(db, {"final_text": "a locked-term poster"})
    assert exc.value.status_code == 400
    assert exc.value.detail == "内容安全拦截:提示词包含平台禁止内容"
    # 词表命中大小写不敏感、跨值拼接
    with pytest.raises(HTTPException):
        cs.assert_text_allowed(db, "x", "ANOTHER-TERM here")


# ---------------------------------------------------------------------------
# 媒体机审:默认关闭时零行为变化
# ---------------------------------------------------------------------------


def _forbid_provider(monkeypatch):
    def _boom(name=None):  # noqa: ARG001
        raise AssertionError("媒体审核关闭时不应构造 provider")

    monkeypatch.setattr(cs, "get_media_moderation_provider", _boom)


def test_media_moderation_disabled_by_default(db, monkeypatch):
    _forbid_provider(monkeypatch)
    result = cs.assert_media_allowed(
        db, media_type="image", scene="upload_image", data=b"fake-png"
    )
    assert result.decision == csp.DECISION_ALLOW
    assert result.provider == "disabled"


def test_upload_and_generated_wrappers_are_noop_when_disabled(db, monkeypatch):
    _forbid_provider(monkeypatch)
    up = cs.assert_upload_media_allowed(db, user_id=1, media_type="image", data=b"x")
    gen = cs.assert_generated_media_allowed(
        db, task_id=1, user_id=1, media_type="video", url="http://example.com/v.mp4"
    )
    assert up.decision == csp.DECISION_ALLOW
    assert gen.decision == csp.DECISION_ALLOW


# ---------------------------------------------------------------------------
# 媒体机审:开启后 拒绝/复审/放行 + 审计落地
# ---------------------------------------------------------------------------


class _FakeProvider(csp.MediaModerationProvider):
    name = "fake"

    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error
        self.calls = []

    def moderate(self, request):
        self.calls.append(request)
        if self._error is not None:
            raise self._error
        return self._result


def _use_provider(monkeypatch, provider):
    monkeypatch.setattr(cs, "get_media_moderation_provider", lambda name=None: provider)


def _last_moderation_audit(db):
    return (
        db.query(AuditLog)
        .filter(AuditLog.action == "media_moderation")
        .order_by(AuditLog.id.desc())
        .first()
    )


def test_media_reject_raises_400_and_lands_audit(db, monkeypatch, restore_media_settings):
    set_setting(db, "media_moderation_enabled", True)
    provider = _FakeProvider(
        result=csp.MediaModerationResult(
            decision=csp.DECISION_REJECT, reason="porn", confidence=0.97, provider="fake"
        )
    )
    _use_provider(monkeypatch, provider)
    with pytest.raises(cs.MediaModerationRejected) as exc:
        cs.assert_upload_media_allowed(
            db, user_id=42, media_type="image", data=b"bad-image", mime_type="image/png"
        )
    assert exc.value.status_code == 400
    assert "内容安全拦截" in exc.value.detail
    assert exc.value.decision == csp.DECISION_REJECT
    assert exc.value.reason == "porn"
    assert len(provider.calls) == 1
    assert provider.calls[0].media_type == "image"

    row = _last_moderation_audit(db)
    assert row is not None
    assert row.user_id == 42
    assert row.biz_type == "upload"
    assert row.detail["decision"] == "reject"
    assert row.detail["reason"] == "porn"
    assert row.detail["confidence"] == 0.97
    assert row.detail["sha256"]  # 媒体指纹落档,供人工复核


def test_media_review_decision_blocks_with_review_message(db, monkeypatch, restore_media_settings):
    set_setting(db, "media_moderation_enabled", True)
    provider = _FakeProvider(
        result=csp.MediaModerationResult(decision=csp.DECISION_REVIEW, reason="suspect", provider="fake")
    )
    _use_provider(monkeypatch, provider)
    with pytest.raises(cs.MediaModerationRejected) as exc:
        cs.assert_generated_media_allowed(
            db, task_id=7, user_id=42, media_type="video", url="http://example.com/v.mp4"
        )
    assert exc.value.status_code == 400
    assert "需人工复审" in exc.value.detail
    assert exc.value.decision == csp.DECISION_REVIEW
    row = _last_moderation_audit(db)
    assert row.biz_type == "gen_task"
    assert row.biz_id == 7
    assert row.detail["decision"] == "review"


def test_media_allow_returns_result_and_lands_audit(db, monkeypatch, restore_media_settings):
    set_setting(db, "media_moderation_enabled", True)
    provider = _FakeProvider(
        result=csp.MediaModerationResult(decision=csp.DECISION_ALLOW, reason="normal", confidence=0.99, provider="fake")
    )
    _use_provider(monkeypatch, provider)
    result = cs.assert_media_allowed(
        db, media_type="image", scene="upload_image", data=b"good", user_id=1
    )
    assert result.decision == csp.DECISION_ALLOW
    row = _last_moderation_audit(db)
    assert row.detail["decision"] == "allow"
    assert row.detail["provider"] == "fake"


def test_audio_bypasses_provider_with_explicit_degraded_allow(
    db, monkeypatch, restore_media_settings
):
    """音频显式降级语义:审核开启时不送 provider、直接放行并落审计。

    provider 协议(MediaModerationRequest)只定义了 image/video;若把 audio
    送进 HTTP 骨架,未识别类型会归 review,导致音频上传全量被卡。
    """
    set_setting(db, "media_moderation_enabled", True)
    # audio 不应触达 provider:构造即失败
    _forbid_provider(monkeypatch)
    result = cs.assert_upload_media_allowed(
        db, user_id=42, media_type="audio", data=b"fake-m4a", mime_type="audio/mp4"
    )
    assert result.decision == csp.DECISION_ALLOW
    assert result.provider == "audio_unsupported"
    assert "音频" in result.reason

    row = _last_moderation_audit(db)
    assert row is not None
    assert row.user_id == 42
    assert row.biz_type == "upload"
    assert row.detail["media_type"] == "audio"
    assert row.detail["provider"] == "audio_unsupported"
    assert row.detail["decision"] == "allow"
    assert row.detail["sha256"]  # 媒体指纹照常落档,供人工抽查


# ---------------------------------------------------------------------------
# 失败策略:默认 fail-closed(503),可显式降级 fail-open(放行但留痕)
# ---------------------------------------------------------------------------


def test_provider_error_fails_closed_by_default(db, monkeypatch, restore_media_settings):
    set_setting(db, "media_moderation_enabled", True)
    provider = _FakeProvider(error=csp.MediaModerationError("审核网关调用失败:超时"))
    _use_provider(monkeypatch, provider)
    with pytest.raises(cs.MediaModerationUnavailable) as exc:
        cs.assert_media_allowed(db, media_type="image", scene="upload_image", data=b"x")
    assert exc.value.status_code == 503
    assert "暂不可用" in exc.value.detail
    row = _last_moderation_audit(db)
    assert row.detail["decision"] == "error"
    assert row.detail["fail_open"] is False


def test_provider_error_fail_open_degrades_with_audit(db, monkeypatch, restore_media_settings):
    set_setting(db, "media_moderation_enabled", True)
    set_setting(db, "media_moderation_fail_open", True)
    provider = _FakeProvider(error=csp.MediaModerationError("审核网关调用失败:超时"))
    _use_provider(monkeypatch, provider)
    result = cs.assert_media_allowed(db, media_type="image", scene="upload_image", data=b"x")
    assert result.decision == csp.DECISION_ALLOW
    assert result.provider == "degraded"
    assert "降级放行" in result.reason
    row = _last_moderation_audit(db)
    assert row.detail["decision"] == "error"
    assert row.detail["fail_open"] is True


def test_unexpected_provider_exception_also_fails_closed(db, monkeypatch, restore_media_settings):
    set_setting(db, "media_moderation_enabled", True)
    provider = _FakeProvider(error=RuntimeError("provider 内部 bug"))
    _use_provider(monkeypatch, provider)
    with pytest.raises(cs.MediaModerationUnavailable):
        cs.assert_media_allowed(db, media_type="image", scene="upload_image", data=b"x")


# ---------------------------------------------------------------------------
# provider 工厂与 noop
# ---------------------------------------------------------------------------


def test_factory_defaults_to_noop_and_rejects_unknown():
    assert isinstance(csp.get_media_moderation_provider(), csp.NoopModerationProvider)
    assert isinstance(csp.get_media_moderation_provider("noop"), csp.NoopModerationProvider)
    assert isinstance(csp.get_media_moderation_provider("http"), csp.HttpModerationProvider)
    with pytest.raises(csp.MediaModerationError, match="暂未实现"):
        csp.get_media_moderation_provider("some-vendor")


def test_noop_provider_always_allows():
    result = csp.NoopModerationProvider().moderate(
        csp.MediaModerationRequest(media_type="image", data=b"x")
    )
    assert result.decision == csp.DECISION_ALLOW


# ---------------------------------------------------------------------------
# HTTP provider 骨架:配置驱动的请求/响应映射
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, invalid_json=False):
        self.status_code = status_code
        self._payload = payload
        self._invalid_json = invalid_json

    def json(self):
        if self._invalid_json:
            raise ValueError("not json")
        return self._payload


class _FakeClient:
    def __init__(self, response=None, error=None, sink=None):
        self._response = response
        self._error = error
        self._sink = sink if sink is not None else {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, headers=None, json=None):
        self._sink["url"] = url
        self._sink["headers"] = headers
        self._sink["json"] = json
        if self._error is not None:
            raise self._error
        return self._response


def _http_provider(**kwargs):
    kwargs.setdefault("url", "http://moderation.example.com/scan")
    kwargs.setdefault("api_key", "test-key")
    kwargs.setdefault("timeout_seconds", 5)
    kwargs.setdefault("decision_field", "result.suggestion")
    kwargs.setdefault("reason_field", "result.label")
    kwargs.setdefault("confidence_field", "result.score")
    kwargs.setdefault("allow_values", "pass")
    kwargs.setdefault("reject_values", "block")
    kwargs.setdefault("review_values", "review")
    return csp.HttpModerationProvider(**kwargs)


def _patch_client(monkeypatch, client):
    monkeypatch.setattr(csp, "pinned_client", lambda url, **kw: client)


def test_http_provider_maps_configured_response_fields(monkeypatch):
    sink = {}
    client = _FakeClient(
        response=_FakeResponse(payload={"result": {"suggestion": "Block", "label": "porn", "score": "0.98"}}),
        sink=sink,
    )
    _patch_client(monkeypatch, client)
    result = _http_provider().moderate(
        csp.MediaModerationRequest(media_type="image", scene="upload_image", data=b"img", mime_type="image/png")
    )
    assert result.decision == csp.DECISION_REJECT
    assert result.reason == "porn"
    assert result.confidence == pytest.approx(0.98)
    assert result.provider == "http"
    # 请求形状:通用 JSON + Bearer 鉴权 + base64 内联字节
    assert sink["headers"]["Authorization"] == "Bearer test-key"
    assert sink["json"]["media_type"] == "image"
    assert sink["json"]["data_base64"]


def test_http_provider_allow_and_url_reference(monkeypatch):
    sink = {}
    client = _FakeClient(
        response=_FakeResponse(payload={"result": {"suggestion": "pass", "label": "", "score": 0.1}}),
        sink=sink,
    )
    _patch_client(monkeypatch, client)
    result = _http_provider().moderate(
        csp.MediaModerationRequest(media_type="video", url="http://example.com/v.mp4")
    )
    assert result.decision == csp.DECISION_ALLOW
    assert sink["json"]["url"] == "http://example.com/v.mp4"
    assert "data_base64" not in sink["json"]


def test_http_provider_unknown_decision_becomes_review(monkeypatch):
    client = _FakeClient(response=_FakeResponse(payload={"result": {"suggestion": "weird"}}))
    _patch_client(monkeypatch, client)
    result = _http_provider().moderate(csp.MediaModerationRequest(media_type="image", data=b"x"))
    assert result.decision == csp.DECISION_REVIEW
    assert "未识别的审核结论" in result.reason


def test_http_provider_error_paths(monkeypatch):
    provider = _http_provider()
    # 网络错误 / 超时
    _patch_client(monkeypatch, _FakeClient(error=httpx.ConnectTimeout("timeout")))
    with pytest.raises(csp.MediaModerationError, match="审核网关调用失败"):
        provider.moderate(csp.MediaModerationRequest(media_type="image", data=b"x"))
    # 非 2xx
    _patch_client(monkeypatch, _FakeClient(response=_FakeResponse(status_code=502)))
    with pytest.raises(csp.MediaModerationError, match="502"):
        provider.moderate(csp.MediaModerationRequest(media_type="image", data=b"x"))
    # 非 JSON 响应
    _patch_client(monkeypatch, _FakeClient(response=_FakeResponse(invalid_json=True)))
    with pytest.raises(csp.MediaModerationError, match="JSON"):
        provider.moderate(csp.MediaModerationRequest(media_type="image", data=b"x"))


def test_http_provider_requires_endpoint_and_media():
    with pytest.raises(csp.MediaModerationError, match="未配置"):
        _http_provider(url="").moderate(csp.MediaModerationRequest(media_type="image", data=b"x"))
    with pytest.raises(csp.MediaModerationError, match="缺少媒体地址或字节流"):
        _http_provider().moderate(csp.MediaModerationRequest(media_type="image"))


def test_http_provider_oversized_bytes_without_url_rejected():
    provider = _http_provider(max_inline_bytes=4)
    with pytest.raises(csp.MediaModerationError, match="内联审核上限"):
        provider.moderate(csp.MediaModerationRequest(media_type="video", data=b"12345"))


def test_http_provider_oversized_bytes_with_url_falls_back_to_url(monkeypatch):
    sink = {}
    client = _FakeClient(
        response=_FakeResponse(payload={"result": {"suggestion": "pass"}}), sink=sink
    )
    _patch_client(monkeypatch, client)
    provider = _http_provider(max_inline_bytes=4)
    result = provider.moderate(
        csp.MediaModerationRequest(media_type="video", data=b"12345", url="http://example.com/v.mp4")
    )
    assert result.decision == csp.DECISION_ALLOW
    assert "data_base64" not in sink["json"]
    assert sink["json"]["url"] == "http://example.com/v.mp4"


# ---------------------------------------------------------------------------
# 生成产出挂载点接线:图片生成在落库前真的会经过机审(端到端)
# ---------------------------------------------------------------------------


def _t2i_payload(request_id: str) -> dict:
    return {
        "client_request_id": request_id,
        "category": "image",
        "stage": "preview",
        "instruction": "a calm cat on a sofa",
        "params": {"n": 1, "size": "1024x1024"},
    }


def test_generated_image_moderation_reject_fails_task_and_refunds(
    client, make_user, auth, quote_and_generate, db, monkeypatch, restore_media_settings
):
    set_setting(db, "media_moderation_enabled", True)
    provider = _FakeProvider(
        result=csp.MediaModerationResult(
            decision=csp.DECISION_REJECT, reason="nsfw", confidence=0.99, provider="fake"
        )
    )
    _use_provider(monkeypatch, provider)
    make_user("13900000501", balance=1000)
    h = auth("13900000501")
    r = quote_and_generate(_t2i_payload("moderation-e2e-reject-1"), headers=h)
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    t = client.get(f"/api/tasks/{tid}", headers=h).json()
    assert t["status"] == "failed", t
    assert t["assets"] == []
    # 全部产出被拒 -> 生成失败结算,冻结积分退回
    me = client.get("/api/me", headers=h).json()
    assert me["balance_credits"] == 1000
    # 确认经过的是生成产出挂载点
    assert provider.calls, "机审 provider 未被调用"
    assert provider.calls[0].scene == "generation_output"
    assert provider.calls[0].media_type == "image"


def test_generated_image_moderation_allow_keeps_success(
    client, make_user, auth, quote_and_generate, db, monkeypatch, restore_media_settings
):
    set_setting(db, "media_moderation_enabled", True)
    provider = _FakeProvider(
        result=csp.MediaModerationResult(
            decision=csp.DECISION_ALLOW, reason="", confidence=0.9, provider="fake"
        )
    )
    _use_provider(monkeypatch, provider)
    make_user("13900000502", balance=1000)
    h = auth("13900000502")
    r = quote_and_generate(_t2i_payload("moderation-e2e-allow-1"), headers=h)
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    t = client.get(f"/api/tasks/{tid}", headers=h).json()
    assert t["status"] == "succeeded", t
    assert len(t["assets"]) == 1
    assert provider.calls and provider.calls[0].scene == "generation_output"
