"""Probe service: the only component that contacts participant URLs.

Deployed as its own Cloud Run service with a service account that has no IAM roles, no
secrets and no DB access (§14.1). Invokable only by the worker's service account.
"""

import hashlib

from fastapi import FastAPI
from pydantic import BaseModel, Field

from judgebench.config import settings
from judgebench.probe.guard import Blocked, GuardedClient

app = FastAPI(title="probe", docs_url=None, redoc_url=None)

MAX_EXCERPT = 4096


class ProbeRequest(BaseModel):
    method: str = "GET"
    url: str = Field(max_length=2048)
    body: str | None = Field(default=None, max_length=16 * 1024)
    content_type: str | None = None


class ProbeBatch(BaseModel):
    """All live-check requests for one submission: caps apply across the batch."""

    requests: list[ProbeRequest] = Field(max_length=6)


class ProbeOutcome(BaseModel):
    url: str
    blocked: str | None = None
    error: str | None = None
    status: int | None = None
    latency_ms: int | None = None
    content_type: str | None = None
    body_excerpt: str | None = None
    body_sha256: str | None = None
    truncated: bool = False
    hops: list[str] = []


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/probe")
def probe(batch: ProbeBatch) -> list[ProbeOutcome]:
    # Sync endpoint: FastAPI runs it in a threadpool, so blocking sockets are fine.
    client = GuardedClient(user_agent=settings().probe_user_agent)
    outcomes = []
    for req in batch.requests:
        try:
            r = client.request(req.method, req.url,
                               body=req.body.encode() if req.body is not None else None,
                               content_type=req.content_type)
        except Blocked as exc:
            outcomes.append(ProbeOutcome(url=req.url, blocked=str(exc)))
            continue
        except (OSError, ValueError) as exc:  # timeouts, TLS failures, refused connections
            outcomes.append(ProbeOutcome(url=req.url, error=type(exc).__name__))
            continue
        outcomes.append(ProbeOutcome(
            url=r.url, status=r.status, latency_ms=r.latency_ms, content_type=r.content_type,
            body_excerpt=r.body[:MAX_EXCERPT].decode("utf-8", "replace"),
            body_sha256=hashlib.sha256(r.body).hexdigest(), truncated=r.truncated, hops=r.hops,
        ))
    return outcomes
