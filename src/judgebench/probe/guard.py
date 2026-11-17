"""The one guarded HTTP client for participant-supplied URLs (§14.1 rule 2: SSRF protection).

- HTTPS on port 443 only; no credentials in URLs; no cookies, no auth headers, fixed User-Agent.
- Resolve DNS once per host, reject the host if *any* answer is non-global, then connect to
  that pinned IP (TLS SNI + cert check still use the hostname). DNS rebinding can't swap it.
- Redirects are never followed by a library: each Location is re-validated and re-resolved
  under the same rules, with a hop limit.
- Caps: per-client total requests, per-host requests, timeout, response bytes.

Stdlib only (http.client + ssl), so the probe image needs no HTTP library.
"""

import http.client
import ipaddress
import socket
import ssl
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urljoin, urlsplit

BLOCKED_HOSTS = {"metadata.google.internal", "metadata", "metadata.goog", "localhost"}
BLOCKED_SUFFIXES = (".internal", ".local", ".localhost", ".localdomain", ".home.arpa")
REDIRECTS = {301, 302, 303, 307, 308}
METHODS = {"GET", "HEAD", "POST"}


class Blocked(Exception):
    """Request refused by policy before (or instead of) connecting."""


@dataclass(frozen=True)
class ProbeLimits:
    max_redirects: int = 3
    max_requests_total: int = 6
    max_requests_per_host: int = 5
    timeout_seconds: float = 10.0
    max_response_bytes: int = 256 * 1024
    max_request_body_bytes: int = 16 * 1024


@dataclass(frozen=True)
class RawResponse:
    status: int
    headers: dict[str, str]  # lower-cased names
    body: bytes
    truncated: bool


@dataclass(frozen=True)
class ProbeResult:
    url: str
    status: int
    content_type: str | None
    body: bytes
    truncated: bool
    latency_ms: int
    pinned_ip: str
    hops: list[str] = field(default_factory=list)


Resolver = Callable[[str, int], list[str]]


class Sender(Protocol):
    def __call__(
        self,
        *,
        host: str,
        ip: str,
        port: int,
        method: str,
        target: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
        max_bytes: int,
    ) -> RawResponse: ...


def check_address(addr: str) -> None:
    ip = ipaddress.ip_address(addr.split("%", 1)[0])  # drop IPv6 zone id
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip.sixtofour or ip.teredo:
            raise Blocked(f"tunnelled IPv6 address {addr}")
    # is_global is False for private, loopback, link-local (incl. 169.254.169.254), CGNAT
    # (100.64/10), unspecified and reserved ranges. Multicast must be checked separately.
    if not ip.is_global or ip.is_multicast:
        raise Blocked(f"non-public address {addr}")


def system_resolve(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(i[4][0]) for i in infos))


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connects to a pre-validated IP; SNI and certificate check use the hostname."""

    def __init__(self, host: str, ip: str, port: int, timeout: float) -> None:
        self._tls = ssl.create_default_context()
        super().__init__(host, port, timeout=timeout, context=self._tls)
        self._pinned_ip = ip

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._tls.wrap_socket(sock, server_hostname=self.host)


def pinned_send(
    *,
    host: str,
    ip: str,
    port: int,
    method: str,
    target: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
    max_bytes: int,
) -> RawResponse:
    conn = _PinnedHTTPSConnection(host, ip, port, timeout)
    deadline = time.monotonic() + timeout
    try:
        conn.request(method, target, body=body, headers=headers)
        resp = conn.getresponse()
        hdrs: dict[str, str] = {}
        for k, v in resp.getheaders():
            hdrs[k.lower()] = f"{hdrs[k.lower()]}, {v}" if k.lower() in hdrs else v
        chunks: list[bytes] = []
        size = 0
        while size <= max_bytes and time.monotonic() < deadline:  # slow-drip guard
            chunk = resp.read(min(65536, max_bytes + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        data = b"".join(chunks)
        return RawResponse(resp.status, hdrs, data[:max_bytes], len(data) > max_bytes)
    finally:
        conn.close()


class GuardedClient:
    """One instance per submission: request caps are per instance."""

    def __init__(
        self,
        *,
        resolve: Resolver = system_resolve,
        send: Sender = pinned_send,
        limits: ProbeLimits = ProbeLimits(),
        user_agent: str = "probe/0.1",
    ) -> None:  # noqa: B008
        self._resolve, self._send, self.limits, self._ua = resolve, send, limits, user_agent
        self._pins: dict[str, str] = {}
        self._per_host: Counter[str] = Counter()
        self._total = 0

    def _validate(self, url: str) -> tuple[str, str]:
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise Blocked(f"scheme {parts.scheme!r} not allowed (HTTPS only)")
        if parts.username is not None or parts.password is not None:
            raise Blocked("credentials in URL")
        try:
            port = parts.port
        except ValueError as exc:
            raise Blocked("invalid port") from exc
        if port not in (None, 443):
            raise Blocked(f"port {port} not allowed")
        host = (parts.hostname or "").rstrip(".").lower()
        if not host or host in BLOCKED_HOSTS or host.endswith(BLOCKED_SUFFIXES):
            raise Blocked(f"host {host!r} not allowed")
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        return host, target

    def _pin(self, host: str) -> str:
        if host not in self._pins:
            try:
                answers = self._resolve(host, 443)
            except (OSError, UnicodeError) as exc:
                raise Blocked(f"cannot resolve {host!r}") from exc
            if not answers:
                raise Blocked(f"no addresses for {host!r}")
            for addr in answers:  # every answer must be public, not just the first
                check_address(addr)
            self._pins[host] = answers[0]
        return self._pins[host]

    def _count(self, host: str) -> None:
        if self._total >= self.limits.max_requests_total:
            raise Blocked("total request cap reached")
        if self._per_host[host] >= self.limits.max_requests_per_host:
            raise Blocked(f"per-host request cap reached for {host!r}")
        self._total += 1
        self._per_host[host] += 1

    def request(
        self, method: str, url: str, *, body: bytes | None = None, content_type: str | None = None
    ) -> ProbeResult:
        method = method.upper()
        if method not in METHODS:
            raise Blocked(f"method {method} not allowed")
        if body is not None and len(body) > self.limits.max_request_body_bytes:
            raise Blocked("request body too large")
        hops: list[str] = []
        start = time.monotonic()
        for _ in range(self.limits.max_redirects + 1):
            host, target = self._validate(url)
            ip = self._pin(host)
            self._count(host)
            headers = {"User-Agent": self._ua, "Accept": "*/*", "Connection": "close"}
            if body is not None and content_type:
                headers["Content-Type"] = content_type
            raw = self._send(
                host=host,
                ip=ip,
                port=443,
                method=method,
                target=target,
                headers=headers,
                body=body,
                timeout=self.limits.timeout_seconds,
                max_bytes=self.limits.max_response_bytes,
            )
            hops.append(url)
            location = raw.headers.get("location")
            if raw.status in REDIRECTS and location:
                url = urljoin(url, location)
                if raw.status == 303 or (raw.status in (301, 302) and method == "POST"):
                    method, body = "GET", None
                continue
            return ProbeResult(
                url=url,
                status=raw.status,
                content_type=raw.headers.get("content-type"),
                body=raw.body,
                truncated=raw.truncated,
                pinned_ip=ip,
                hops=hops,
                latency_ms=int((time.monotonic() - start) * 1000),
            )
        raise Blocked(f"too many redirects (> {self.limits.max_redirects})")
