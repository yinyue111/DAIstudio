"""SSRF guard (red line)."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.services.ssrf import SsrfError, _ip_is_blocked, assert_safe_url


def test_scheme_rejected():
    with pytest.raises(SsrfError):
        assert_safe_url("ftp://example.com/a")
    with pytest.raises(SsrfError):
        assert_safe_url("file:///etc/passwd")


def test_internal_literals_blocked():
    for u in [
        "http://127.0.0.1/",
        "http://10.0.0.1/x",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data",  # cloud metadata
    ]:
        with pytest.raises(SsrfError):
            assert_safe_url(u)


def test_public_literal_allowed():
    assert assert_safe_url("http://8.8.8.8/") == "http://8.8.8.8/"


def test_ip_classification():
    assert _ip_is_blocked("127.0.0.1")
    assert _ip_is_blocked("169.254.169.254")
    assert _ip_is_blocked("10.1.2.3")
    assert _ip_is_blocked("198.18.0.22")
    assert not _ip_is_blocked("1.1.1.1")


def test_ipv4_mapped_v6_blocked():
    # ::ffff:127.0.0.1 must not slip past via the v6 representation
    assert _ip_is_blocked("::ffff:127.0.0.1")
    assert _ip_is_blocked("::ffff:169.254.169.254")
    assert _ip_is_blocked("::ffff:10.0.0.1")


def test_cgnat_blocked():
    assert _ip_is_blocked("100.64.0.1")
    assert _ip_is_blocked("100.127.255.254")
    assert not _ip_is_blocked("100.128.0.1")  # just outside 100.64.0.0/10


def test_fake_ip_dns_uses_public_doh_resolution(monkeypatch):
    from app.services import ssrf

    monkeypatch.setattr(
        ssrf.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("198.18.0.22", 0))],
    )
    monkeypatch.setattr(ssrf, "_resolve_via_public_doh", lambda _host: ["8.8.8.8"])

    assert ssrf.resolve_safe("public.example.com") == ["8.8.8.8"]


def test_fake_ip_dns_doh_private_answer_is_still_blocked(monkeypatch):
    from app.services import ssrf

    monkeypatch.setattr(
        ssrf.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("198.18.0.22", 0))],
    )
    monkeypatch.setattr(ssrf, "_resolve_via_public_doh", lambda _host: ["127.0.0.1"])

    with pytest.raises(SsrfError, match="禁止访问内网"):
        ssrf.resolve_safe("internal.example.com")


def test_mixed_fake_and_public_dns_answer_does_not_use_doh(monkeypatch):
    from app.services import ssrf

    monkeypatch.setattr(
        ssrf.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (2, 1, 6, "", ("198.18.0.22", 0)),
            (2, 1, 6, "", ("8.8.8.8", 0)),
        ],
    )
    monkeypatch.setattr(
        ssrf,
        "_resolve_via_public_doh",
        lambda _host: pytest.fail("mixed answers must fail closed before DoH"),
    )

    with pytest.raises(SsrfError, match="禁止访问内网"):
        ssrf.resolve_safe("mixed.example.com")


def test_fake_ip_doh_cache_coalesces_concurrent_resolution(monkeypatch):
    from app.services import ssrf

    host = "concurrent-cache.example.com"
    monkeypatch.setattr(
        ssrf.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("198.18.0.22", 0))],
    )
    calls = 0
    calls_lock = threading.Lock()

    def fake_query(_host):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.05)
        return ["8.8.8.8"]

    with ssrf._doh_cache_lock:
        ssrf._doh_cache.pop(host, None)
    monkeypatch.setattr(ssrf, "_query_public_doh", fake_query)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(ssrf.resolve_safe, [host] * 4))

    assert results == [["8.8.8.8"]] * 4
    assert calls == 1


def test_redirect_target_revalidated(monkeypatch):
    # a public host that resolves fine, but a redirect Location pointing at an
    # internal literal must be rejected when re-validated.
    from app.services import ssrf

    real = ssrf.socket.getaddrinfo

    def fake_getaddrinfo(host, *a, **k):
        if host == "evil.example.com":
            return [(2, 1, 6, "", ("8.8.8.8", 0))]
        return real(host, *a, **k)

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", fake_getaddrinfo)
    assert ssrf.assert_safe_url("http://evil.example.com/") == "http://evil.example.com/"
    with pytest.raises(SsrfError):
        ssrf.assert_safe_url("http://169.254.169.254/latest/meta-data")
