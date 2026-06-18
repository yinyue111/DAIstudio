"""SSRF guard (RED LINE).

User-controlled URLs must never reach internal networks or cloud metadata.
We allow only http/https and reject any URL whose hostname resolves to a
private / loopback / link-local / reserved / multicast / CGNAT address. The
cloud metadata address (169.254.169.254) is link-local and therefore blocked.

A single DNS check is not enough on its own: the host can be re-resolved (DNS
rebinding) or a response can redirect to an internal target. So callers must
(a) re-validate every redirect hop with ``assert_safe_url`` and (b) pin the
connection to the IP returned by ``resolve_safe`` where possible. ``fetcher``
does both.
"""
from __future__ import annotations

import ipaddress
import socket
import threading
from contextlib import contextmanager
from urllib.parse import urlparse

ALLOWED_SCHEMES = {"http", "https"}
MAX_REDIRECTS = 5

# Carrier-grade NAT range — not flagged by is_private but still internal-ish.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_DNS_PIN_LOCK = threading.RLock()


class SsrfError(Exception):
    pass


def _classify_blocked(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # Normalise IPv4-mapped/compatible IPv6 (e.g. ::ffff:127.0.0.1) to v4 so the
    # classification below cannot be sidestepped via the v6 representation.
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if isinstance(addr, ipaddress.IPv4Address) and addr in _CGNAT:
        return True
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def _ip_is_blocked(ip: str) -> bool:
    return _classify_blocked(ipaddress.ip_address(ip))


def resolve_safe(host: str) -> list[str]:
    """Resolve ``host`` and raise SsrfError if ANY resolved IP is internal.

    Returns the list of resolved IPs so the caller can pin the socket to a
    vetted address (defends against the resolver returning a different,
    internal IP at connect time)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise SsrfError("域名解析失败")

    ips: list[str] = []
    for info in infos:
        ip = info[4][0]
        try:
            if _ip_is_blocked(ip):
                raise SsrfError("禁止访问内网/保留地址")
        except ValueError:
            raise SsrfError("非法的解析地址")
        if ip not in ips:
            ips.append(ip)
    return ips


def assert_safe_url(url: str) -> str:
    """Validate scheme + that every IP the host resolves to is public.

    Call this on the original URL AND on every redirect Location before
    following it."""
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise SsrfError("仅支持 http/https 链接")
    host = parsed.hostname
    if not host:
        raise SsrfError("无法解析链接的主机名")
    resolve_safe(host)
    return url


def assert_safe_user_asset_url(url: str | None) -> None:
    """Validate a user-supplied asset URL before forwarding it to the model
    gateway (reverse / image-edit / video first-frame) or persisting it.

    Our own ``/media`` and authenticated upload URLs are trusted only after storage URL normalisation;
    everything else must be http/https resolving to a public address — closing
    the SSRF hole where a pasted URL could point the gateway (or us) at
    internal/metadata endpoints. Raises SsrfError on violation."""
    if not url:
        return
    from ..config import settings
    base = settings.public_base_url.rstrip("/")
    if base and (url.startswith(base + "/media/") or url.startswith(base + "/api/uploads/")):
        from .storage import key_from_url

        if not key_from_url(url):
            raise SsrfError("非法的本机媒体链接")
        return
    assert_safe_url(url)


def local_storage_key_from_user_asset_url(url: str | None) -> str | None:
    """Return our storage key for a trusted local media/upload URL."""
    if not url:
        return None
    from ..config import settings
    from .storage import key_from_url

    base = settings.public_base_url.rstrip("/")
    if base and (url.startswith(base + "/media/") or url.startswith(base + "/api/uploads/")):
        return key_from_url(url)
    return None


@contextmanager
def pinned_safe_resolution(url_or_host: str):
    """Temporarily pin socket DNS resolution for ``url_or_host`` to vetted IPs.

    httpx resolves the hostname again when opening the TCP connection. Without
    pinning, a hostname can pass ``assert_safe_url`` and then rebind to an
    internal IP at connect time. This guard serialises the short connect/read
    window and makes socket.getaddrinfo return only the IPs that ``resolve_safe``
    already classified as public.
    """
    parsed = urlparse(url_or_host)
    host = parsed.hostname or url_or_host
    if not host:
        raise SsrfError("无法解析链接的主机名")
    host_key = host.rstrip(".").lower()
    safe_ips = resolve_safe(host)
    original_getaddrinfo = socket.getaddrinfo

    def pinned_getaddrinfo(node, port, family=0, type=0, proto=0, flags=0):
        node_text = node.decode() if isinstance(node, bytes) else str(node)
        if node_text.rstrip(".").lower() != host_key:
            return original_getaddrinfo(node, port, family, type, proto, flags)
        rows = []
        for ip in safe_ips:
            rows.extend(original_getaddrinfo(ip, port, family, type, proto, flags))
        return rows

    with _DNS_PIN_LOCK:
        socket.getaddrinfo = pinned_getaddrinfo
        try:
            yield safe_ips
        finally:
            socket.getaddrinfo = original_getaddrinfo
