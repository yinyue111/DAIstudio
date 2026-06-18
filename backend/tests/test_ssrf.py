"""SSRF guard (red line)."""
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
