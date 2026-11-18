"""Live check planning + interpretation; the HTTP itself is the probe service (tested in security/)."""

from pathlib import Path
from typing import Any

import pytest

from judgebench.evidence import CheckStatus, EvidenceStatus
from judgebench.worker.live_check import live_check_findings, parse_sample
from tests import fixtures_repos as repos
from tests.test_ci import write

APP = "https://alpha.example.app/"


class FakeProbe:
    def __init__(self, *outcomes: dict[str, Any]) -> None:
        self.outcomes, self.sent = list(outcomes), []  # type: ignore[var-annotated]

    def probe(self, requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.sent.append(requests)
        return self.outcomes[: len(requests)]


@pytest.mark.parametrize(
    "text,method,url,body",
    [
        (
            'curl -X POST https://alpha.example.app/ask -H "Content-Type: application/json" -d \'{"q": "hi"}\'',
            "POST",
            "https://alpha.example.app/ask",
            '{"q": "hi"}',
        ),
        (
            '{"method": "POST", "path": "/ask", "body": {"q": "hi"}, "expect": ["sources"]}',
            "POST",
            "https://alpha.example.app/ask",
            '{"q": "hi"}',
        ),
        ("GET /health\nexpect: ok", "GET", "https://alpha.example.app/health", None),
    ],
)
def test_sample_formats(text: str, method: str, url: str, body: str | None) -> None:
    s = parse_sample(text, APP)
    assert s.request is not None
    assert (s.request["method"], s.request["url"], s.request.get("body")) == (method, url, body)


@pytest.mark.parametrize(
    "text",
    [
        "curl https://169.254.169.254/computeMetadata/v1/",  # different host: never planned
        "POST https://evil.example/steal",
        "Just ask it about cats and it answers nicely",
    ],
)
def test_unusable_or_cross_host_samples_are_not_sent(text: str) -> None:
    assert parse_sample(text, APP).request is None


def _probe() -> "FakeProbe":
    return FakeProbe(
        {"status": 200, "latency_ms": 180, "content_type": "text/html"},
        {
            "status": 200,
            "latency_ms": 2100,
            "content_type": "application/json",
            "body_excerpt": '{"answer": "x", "sources": ["a.pdf"]}',
        },
    )


SAMPLE = '{"method":"POST","path":"/ask","body":{"q":"hi"},"expect":["sources"]}'


def test_sample_query_verified_needs_the_route_in_code(tmp_path: Path) -> None:
    """§7.5: behaviour + matching code evidence = VERIFIED; behaviour alone = PARTIALLY_VERIFIED."""
    (tmp_path / "other.py").write_text("print('no routes here')\n")
    findings, status, _ = live_check_findings(_probe(), APP, SAMPLE, tmp_path)
    assert status == CheckStatus.PARTIALLY_VERIFIED
    assert findings[1].status == EvidenceStatus.OBSERVED and findings[1].behavioural
    assert findings[1].observation.endswith("; route not found in code")


def test_sample_query_success_is_behavioural(tmp_path: Path) -> None:
    write(tmp_path, repos.ALPHA_RAG)
    probe = FakeProbe(
        {"status": 200, "latency_ms": 180, "content_type": "text/html"},
        {
            "status": 200,
            "latency_ms": 2100,
            "content_type": "application/json",
            "body_excerpt": '{"answer": "x", "sources": ["a.pdf"]}',
        },
    )
    findings, status, details = live_check_findings(probe, APP, SAMPLE, tmp_path)
    assert status == CheckStatus.VERIFIED and findings[1].status == EvidenceStatus.VERIFIED
    assert findings[1].observation.endswith("; route found in code")
    assert [f.behavioural for f in findings] == [False, True]
    assert details["summary"] == "/ 200 in 180 ms; sample query 200 in 2.1 s"
    assert len(probe.sent[0]) == 2


def test_unreachable_app_is_not_verifiable_not_penalised() -> None:
    findings, status, details = live_check_findings(FakeProbe({"error": "ConnectionRefusedError"}), APP, None)
    assert status == CheckStatus.NOT_VERIFIABLE
    assert "unreachable, marked NOT_VERIFIABLE, score not reduced" in details["summary"]
    assert {f.key for f in findings} == {"live.unreachable", "live.sample_query"}


def test_blocked_metadata_url_reported() -> None:
    probe = FakeProbe({"blocked": "non-public address 169.254.169.254"})
    findings, status, details = live_check_findings(probe, "https://169.254.169.254/", None)
    assert status == CheckStatus.NOT_VERIFIABLE and findings[0].key == "live.blocked"


def test_wrong_answer_is_check_failed() -> None:
    probe = FakeProbe({"status": 200, "latency_ms": 90}, {"status": 200, "latency_ms": 900, "body_excerpt": "error"})
    findings, status, _ = live_check_findings(probe, APP, "GET /ask?q=hi\nexpect: sources")
    assert status == CheckStatus.CHECK_FAILED
    assert findings[1].status == EvidenceStatus.CONTRADICTED and not findings[1].behavioural


def test_no_deployed_url() -> None:
    _, status, details = live_check_findings(FakeProbe(), None, None)
    assert status == CheckStatus.NOT_VERIFIABLE and "no deployed app" in details["summary"]
