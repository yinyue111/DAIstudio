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
import typing
from collections.abc import Sequence
from urllib.parse import urlparse

import httpcore
import httpx
from httpx._transports.default import (
    DEFAULT_LIMITS,
    ResponseStream,
    create_ssl_context,
    map_httpcore_exceptions,
)

ALLOWED_SCHEMES = {"http", "https"}
MAX_REDIRECTS = 5

# Carrier-grade NAT range — not flagged by is_private but still internal-ish.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


class SsrfError(Exception):
    pass


def _host_key(host: str | bytes) -> str:
    text = host.decode() if isinstance(host, bytes) else str(host)
    return text.rstrip(".").lower()


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


class _PinnedNetworkBackend(httpcore.NetworkBackend):
    """httpcore backend that connects to vetted IPs while preserving Host/SNI.

    The request URL still contains the original hostname, so httpcore keeps the
    original Host header and TLS SNI/certificate verification. Only the TCP
    destination is swapped to an IP returned by ``resolve_safe``.
    """

    def __init__(
        self,
        pinned_hosts: dict[str, Sequence[str]],
        backend: httpcore.NetworkBackend | None = None,
    ) -> None:
        self._pinned_hosts = {
            _host_key(host): tuple(ips)
            for host, ips in pinned_hosts.items()
            if ips
        }
        self._backend = backend or httpcore.SyncBackend()
        self._lock = threading.Lock()
        self._next_index: dict[str, int] = {}

    def _ordered_ips(self, host_key: str) -> tuple[str, ...]:
        ips = self._pinned_hosts.get(host_key)
        if not ips:
            raise SsrfError("未固定 DNS 的主机被拒绝")
        with self._lock:
            start = self._next_index.get(host_key, 0) % len(ips)
            self._next_index[host_key] = start + 1
        return ips[start:] + ips[:start]

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[typing.Any] | None = None,
    ) -> httpcore.NetworkStream:
        last_exc: Exception | None = None
        for ip in self._ordered_ips(_host_key(host)):
            try:
                return self._backend.connect_tcp(
                    ip,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:  # noqa: BLE001 - try the next vetted IP
                last_exc = exc
        if last_exc is not None:
            raise last_exc
        raise SsrfError("域名没有可用的安全解析地址")

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: typing.Iterable[typing.Any] | None = None,
    ) -> httpcore.NetworkStream:
        raise SsrfError("禁止通过 SSRF 保护客户端访问 Unix socket")

    def sleep(self, seconds: float) -> None:
        self._backend.sleep(seconds)


class PinnedHTTPTransport(httpx.BaseTransport):
    """httpx transport that pins DNS without process-wide monkeypatching."""

    def __init__(
        self,
        pinned_hosts: dict[str, Sequence[str]],
        *,
        verify: httpx._types.VerifyTypes = True,
        cert: httpx._types.CertTypes | None = None,
        http1: bool = True,
        http2: bool = False,
        limits: httpx.Limits = DEFAULT_LIMITS,
        local_address: str | None = None,
        retries: int = 0,
        socket_options: typing.Iterable[typing.Any] | None = None,
    ) -> None:
        ssl_context = create_ssl_context(verify=verify, cert=cert, trust_env=False)
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl_context,
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=http1,
            http2=http2,
            retries=retries,
            local_address=local_address,
            network_backend=_PinnedNetworkBackend(pinned_hosts),
            socket_options=socket_options,
        )

    def __enter__(self) -> PinnedHTTPTransport:
        self._pool.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: typing.Any = None,
    ) -> None:
        with map_httpcore_exceptions():
            self._pool.__exit__(exc_type, exc_value, traceback)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        assert isinstance(request.stream, httpx.SyncByteStream)

        req = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        with map_httpcore_exceptions():
            resp = self._pool.handle_request(req)

        assert isinstance(resp.stream, typing.Iterable)
        return httpx.Response(
            status_code=resp.status,
            headers=resp.headers,
            stream=ResponseStream(resp.stream),
            extensions=resp.extensions,
        )

    def close(self) -> None:
        self._pool.close()


def pinned_transport_for_url(url_or_host: str) -> PinnedHTTPTransport:
    parsed = urlparse(url_or_host)
    host = parsed.hostname or url_or_host
    if not host:
        raise SsrfError("无法解析链接的主机名")
    return PinnedHTTPTransport({_host_key(host): resolve_safe(host)})


def pinned_client(url_or_host: str, **kwargs) -> httpx.Client:
    """Return an httpx client that connects only to vetted IPs for this host.

    Callers should create a fresh client for every redirect hop. The transport
    fails closed if asked to connect to a hostname that was not pre-resolved.
    """
    kwargs.setdefault("follow_redirects", False)
    kwargs.setdefault("trust_env", False)
    return httpx.Client(transport=pinned_transport_for_url(url_or_host), **kwargs)


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
