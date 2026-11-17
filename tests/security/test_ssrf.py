"""§14.2 SSRF rows: metadata, private ranges, redirect, DNS rebinding. Rule 2 of §14.1."""

import socket
from collections.abc import Callable

import pytest

from judgebench.probe.guard import Blocked, GuardedClient, ProbeLimits, RawResponse

PUBLIC_IP = "93.184.215.14"


class FakeNet:
    """Fake resolver + sender. Records every address we actually *connect* to."""

    def __init__(self, dns: dict[str, list[str] | Callable[[], list[str]]],
                 responses: dict[str, RawResponse] | None = None) -> None:
        self.dns = dns
        self.responses = responses or {}
        self.resolve_calls: list[str] = []
        self.connected: list[tuple[str, str, dict[str, str]]] = []

    def resolve(self, host: str, port: int) -> list[str]:
        self.resolve_calls.append(host)
        entry = self.dns.get(host)
        if entry is None:
            # IP literals and unknown names: real getaddrinfo (numeric → no network).
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM,
                                       flags=socket.AI_NUMERICHOST)
            return [str(i[4][0]) for i in infos]
        return entry() if callable(entry) else entry

    def send(self, *, host: str, ip: str, port: int, method: str, target: str,
             headers: dict[str, str], body: bytes | None, timeout: float,
             max_bytes: int) -> RawResponse:
        self.connected.append((host, ip, headers))
        return self.responses.get(host, RawResponse(200, {"content-type": "text/plain"}, b"ok", False))

    def client(self, **limits: int) -> GuardedClient:
        return GuardedClient(resolve=self.resolve, send=self.send, limits=ProbeLimits(**limits))


def assert_blocked(net: FakeNet, url: str) -> None:
    with pytest.raises(Blocked):
        net.client().request("GET", url)
    assert all(ip == PUBLIC_IP for _, ip, _ in net.connected), net.connected


# --- metadata -------------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/computeMetadata/v1/",
    "https://169.254.169.254/computeMetadata/v1/",
    "https://metadata.google.internal/computeMetadata/v1/",
    "https://METADATA.GOOGLE.INTERNAL./computeMetadata/v1/",
    "https://metadata/computeMetadata/v1/",
])
def test_metadata_blocked_before_connecting(url: str) -> None:
    net = FakeNet({"metadata.google.internal": ["169.254.169.254"],
                   "metadata.google.internal.": ["169.254.169.254"],
                   "metadata": ["169.254.169.254"]})
    assert_blocked(net, url)
    assert net.connected == []


# --- private ranges ---------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "https://10.0.0.1/", "https://10.255.1.2/health", "https://127.0.0.1/", "https://[::1]/",
    "https://172.16.0.1/", "https://192.168.1.1/", "https://100.64.0.1/",  # CGNAT
    "https://224.0.0.1/",  # multicast
    "https://0.0.0.0/", "https://[::ffff:127.0.0.1]/", "https://[fe80::1]/", "https://[fc00::1]/",
    "https://2130706433/",  # 127.0.0.1 as an integer
])
def test_private_ranges_blocked(url: str) -> None:
    net = FakeNet({})
    assert_blocked(net, url)
    assert net.connected == []


@pytest.mark.parametrize("url", [
    "http://example.com/",          # not HTTPS
    "https://example.com:8443/",    # non-standard port
    "https://user:pw@example.com/",  # credentials in URL
    "ftp://example.com/",
    "file:///etc/passwd",
])
def test_scheme_port_and_userinfo_rules(url: str) -> None:
    net = FakeNet({"example.com": [PUBLIC_IP]})
    assert_blocked(net, url)
    assert net.connected == []


def test_hostname_resolving_to_private_blocked() -> None:
    net = FakeNet({"internal.example": ["10.1.2.3"]})
    assert_blocked(net, "https://internal.example/")
    assert net.connected == []


# --- redirect ---------------------------------------------------------------------------


def test_redirect_to_private_address_blocked() -> None:
    net = FakeNet(
        {"public.example": [PUBLIC_IP], "internal.example": ["10.0.0.7"]},
        {"public.example": RawResponse(302, {"location": "https://internal.example/admin"}, b"", False)},
    )
    assert_blocked(net, "https://public.example/")
    assert [h for h, _, _ in net.connected] == ["public.example"]


@pytest.mark.parametrize("location", [
    "http://169.254.169.254/computeMetadata/v1/",
    "https://metadata.google.internal/",
    "https://127.0.0.1:443/",
    "http://public.example/",  # downgrade to http
])
def test_redirect_targets_rechecked(location: str) -> None:
    net = FakeNet({"public.example": [PUBLIC_IP], "metadata.google.internal": ["169.254.169.254"]},
                  {"public.example": RawResponse(301, {"location": location}, b"", False)})
    assert_blocked(net, "https://public.example/")
    assert len(net.connected) == 1


def test_redirect_hop_limit() -> None:
    net = FakeNet({"loop.example": [PUBLIC_IP]},
                  {"loop.example": RawResponse(302, {"location": "https://loop.example/"}, b"", False)})
    with pytest.raises(Blocked, match="redirect"):
        net.client(max_redirects=3).request("GET", "https://loop.example/")
    assert len(net.connected) == 4  # original + 3 hops


# --- DNS rebinding ----------------------------------------------------------------------


def test_dns_rebinding_uses_pinned_address() -> None:
    answers = iter([[PUBLIC_IP], ["127.0.0.1"], ["127.0.0.1"]])
    net = FakeNet({"rebind.example": lambda: next(answers)})
    result = net.client().request("GET", "https://rebind.example/")

    assert result.status == 200
    assert [ip for _, ip, _ in net.connected] == [PUBLIC_IP]
    assert net.resolve_calls == ["rebind.example"]  # resolved exactly once


def test_dns_answer_with_any_private_address_blocked() -> None:
    net = FakeNet({"mixed.example": [PUBLIC_IP, "10.0.0.1"]})
    assert_blocked(net, "https://mixed.example/")
    assert net.connected == []


# --- request hygiene and caps -----------------------------------------------------------


def test_no_cookies_no_credentials_fixed_user_agent() -> None:
    net = FakeNet({"a.example": [PUBLIC_IP], "b.example": [PUBLIC_IP]},
                  {"a.example": RawResponse(302, {"location": "https://b.example/",
                                                   "set-cookie": "s=1"}, b"", False)})
    net.client().request("GET", "https://a.example/")
    for _, _, headers in net.connected:
        lowered = {k.lower() for k in headers}
        assert "cookie" not in lowered and "authorization" not in lowered
        assert headers["User-Agent"]


def test_per_host_and_total_request_caps() -> None:
    net = FakeNet({"a.example": [PUBLIC_IP], "b.example": [PUBLIC_IP]})
    client = net.client(max_requests_total=3, max_requests_per_host=2)
    client.request("GET", "https://a.example/")
    client.request("GET", "https://a.example/")
    with pytest.raises(Blocked, match="per-host"):
        client.request("GET", "https://a.example/")
    client.request("GET", "https://b.example/")
    with pytest.raises(Blocked, match="total"):
        client.request("GET", "https://b.example/")


def test_response_size_cap_is_passed_to_transport() -> None:
    seen: list[int] = []
    net = FakeNet({"a.example": [PUBLIC_IP]})
    original = net.send

    def spy(**kw):  # type: ignore[no-untyped-def]
        seen.append(kw["max_bytes"])
        return original(**kw)

    client = GuardedClient(resolve=net.resolve, send=spy, limits=ProbeLimits(max_response_bytes=1234))
    client.request("GET", "https://a.example/")
    assert seen == [1234]


# --- probe service (real resolver, no network needed for blocked cases) -------------


def test_probe_service_blocks_metadata_and_private() -> None:
    from fastapi.testclient import TestClient

    from judgebench.probe.app import app

    resp = TestClient(app).post("/probe", json={"requests": [
        {"url": "http://169.254.169.254/computeMetadata/v1/"},
        {"url": "https://metadata.google.internal/"},
        {"url": "https://127.0.0.1/"},
        {"url": "https://[::1]/"},
    ]})
    assert resp.status_code == 200
    assert all(o["blocked"] and o["status"] is None for o in resp.json())
