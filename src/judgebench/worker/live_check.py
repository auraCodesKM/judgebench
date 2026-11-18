"""Live check of the team's deployed app (§7.5 source 2). The worker only *plans* requests; every
byte goes through the probe service's guarded client (§14.1 rule 2). At most two requests per
submission: the deployed URL itself, and the team's sample request if it is machine-readable.

The sample request is parsed deterministically (curl command, JSON, or "METHOD /path" lines),
never by an LLM, and must target the same host as the deployed URL.
"""

import json
import shlex
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from judgebench.errors import StageError
from judgebench.evidence import CheckStatus, EvidenceStatus, Finding, SourceType


class ProbeClient(Protocol):
    def probe(self, requests: list[dict[str, Any]]) -> list[dict[str, Any]]: ...


class HttpProbeClient:
    """Calls the probe service. On Cloud Run it authenticates with an ID token for the probe URL."""

    def __init__(self, base_url: str) -> None:
        self._url = base_url.rstrip("/")

    def probe(self, requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
        headers = {}
        if self._url.startswith("https://"):
            from google.auth.transport.requests import Request
            from google.oauth2 import id_token

            headers["Authorization"] = f"Bearer {id_token.fetch_id_token(Request(), self._url)}"  # type: ignore[no-untyped-call]
        try:
            resp = httpx.post(f"{self._url}/probe", json={"requests": requests}, headers=headers, timeout=60)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise StageError("probe_unavailable", True, type(exc).__name__) from exc
        return list(resp.json())


@dataclass
class Sample:
    request: dict[str, Any] | None
    expect: list[str] = field(default_factory=list)
    note: str = ""


def parse_sample(text: str | None, deployed_url: str) -> Sample:
    text = (text or "").strip()
    if not text:
        return Sample(None, note="no sample request provided")
    expect = [
        ln.split(":", 1)[1].strip() for ln in text.splitlines() if ln.lower().startswith(("expect:", "expected:"))
    ]
    expect = [e for e in expect if e][:3]
    method, target, body, ctype = "GET", "", None, None
    try:
        if text.lstrip().startswith("{"):
            obj = json.loads(text)
            method = str(obj.get("method", "GET")).upper()
            target = str(obj.get("url") or obj.get("path") or "")
            raw = obj.get("body")
            body = json.dumps(raw) if isinstance(raw, dict | list) else (str(raw) if raw is not None else None)
            ctype = "application/json" if isinstance(raw, dict | list) else None
            expect = [str(e) for e in (obj.get("expect") or expect)][:3]
        elif text.startswith("curl"):
            tokens = shlex.split(text.split("\n")[0] if "\\\n" not in text else text.replace("\\\n", " "))
            it = iter(tokens[1:])
            for tok in it:
                if tok in ("-X", "--request"):
                    method = next(it, "GET").upper()
                elif tok in ("-d", "--data", "--data-raw", "--json"):
                    body, method = next(it, ""), "POST" if method == "GET" else method
                    ctype = ctype or (
                        "application/json" if tok == "--json" or body.lstrip().startswith(("{", "[")) else None
                    )
                elif tok in ("-H", "--header"):
                    h = next(it, "")
                    if h.lower().startswith("content-type:"):
                        ctype = h.split(":", 1)[1].strip()
                elif tok.startswith(("http://", "https://", "/")):
                    target = tok
        else:
            first, *rest = text.splitlines()
            parts = first.split(maxsplit=1)
            if len(parts) == 2 and parts[0].upper() in {"GET", "POST", "HEAD"}:
                method, target = parts[0].upper(), parts[1].strip()
                body_lines = [ln for ln in rest if not ln.lower().startswith(("expect:", "expected:"))]
                body = "\n".join(body_lines).strip() or None
                ctype = "application/json" if body and body.startswith(("{", "[")) else None
    except (ValueError, StopIteration):
        return Sample(None, note="sample request could not be parsed")
    if not target or method not in {"GET", "POST", "HEAD"}:
        return Sample(None, note="sample request is not machine-readable (use curl, JSON or 'POST /path')")
    url = urljoin(deployed_url, target)
    if urlsplit(url).hostname != urlsplit(deployed_url).hostname:
        return Sample(None, note="sample request targets a different host than the deployed app; not sent")
    req: dict[str, Any] = {"method": method, "url": url}
    if body is not None and method == "POST":
        req |= {"body": body[:16_000], "content_type": ctype or "text/plain"}
    return Sample(req, expect)


def _path(url: str) -> str:
    p = urlsplit(url)
    return (p.path or "/") + (f"?{p.query}" if p.query else "")


def live_check_findings(
    probe: ProbeClient, deployed_url: str | None, sample_text: str | None
) -> tuple[list[Finding], CheckStatus, dict[str, Any]]:
    def finding(
        key: str,
        obs: str,
        status: EvidenceStatus,
        locator: str,
        behavioural: bool = False,
        limitations: list[str] | None = None,
    ) -> Finding:
        return Finding(
            key=key,
            observation=obs,
            source_type=SourceType.LIVE_CHECK,
            source_locator=locator,
            status=status,
            confidence=0.9,
            verification_method="live_check",
            criterion="practical_impact",
            behavioural=behavioural,
            limitations=limitations or [],
        )

    if not deployed_url:
        f = finding("live.deployed_app", "No deployed app URL submitted", EvidenceStatus.MISSING, "form:deployed_url")
        return [f], CheckStatus.NOT_VERIFIABLE, {"summary": "no deployed app, marked NOT_VERIFIABLE, score not reduced"}

    sample = parse_sample(sample_text, deployed_url)
    requests = [{"method": "GET", "url": deployed_url}] + ([sample.request] if sample.request else [])
    outcomes = probe.probe(requests)
    findings: list[Finding] = []
    parts: list[str] = []
    status = CheckStatus.NOT_VERIFIABLE
    for i, (req, out) in enumerate(zip(requests, outcomes, strict=False)):
        label = "sample query" if i else _path(req["url"])
        loc = f"live:{req['method']} {_path(req['url'])}"
        if out.get("blocked"):
            findings.append(
                finding("live.blocked", f"Request to {loc[5:]} not sent: {out['blocked']}", EvidenceStatus.MISSING, loc)
            )
            parts.append(f"{label} blocked ({out['blocked']}), marked NOT_VERIFIABLE, score not reduced")
            continue
        if out.get("error") or out.get("status") is None:
            findings.append(
                finding("live.unreachable", f"{loc[5:]} unreachable ({out.get('error')})", EvidenceStatus.MISSING, loc)
            )
            parts.append(f"{label} unreachable, marked NOT_VERIFIABLE, score not reduced")
            continue
        code, ms = int(out["status"]), int(out.get("latency_ms") or 0)
        body = (out.get("body_excerpt") or "").lower()
        missing = [e for e in sample.expect if e.lower() not in body] if i else []
        ok = 200 <= code < 400 and not missing
        obs = f"{loc[5:]} -> {code} in {ms} ms ({out.get('content_type') or 'no content type'})"
        if missing:
            obs += f"; expected text not in response: {', '.join(missing)}"
        findings.append(
            finding(
                "live.sample_query" if i else "live.health",
                obs,
                EvidenceStatus.VERIFIED
                if (ok and i)
                else (EvidenceStatus.OBSERVED if ok else EvidenceStatus.CONTRADICTED),
                loc,
                behavioural=ok and i > 0,
                limitations=["response body truncated"] if out.get("truncated") else [],
            )
        )
        if ok and i:
            status = CheckStatus.VERIFIED
        elif ok and status is CheckStatus.NOT_VERIFIABLE:
            status = CheckStatus.PARTIALLY_VERIFIED
        elif not ok and status is not CheckStatus.VERIFIED:
            status = CheckStatus.CHECK_FAILED
        parts.append(f"{label} {code} in {ms / 1000:.1f} s" if ms >= 1000 else f"{label} {code} in {ms} ms")
    if sample.request is None:
        findings.append(
            finding("live.sample_query", sample.note.capitalize(), EvidenceStatus.MISSING, "form:sample_request")
        )
    details = {"summary": "; ".join(parts), "outcomes": outcomes, "sample_note": sample.note}
    return findings, status, details
