"""Which model the agents use (LLM_MODE), and how each mode is labelled wherever results surface.

offline      FakeLlm with conservative heuristics. No network, $0. Labelled MOCK.
gemini_free  Gemini API free tier (AI Studio key, project without billing). $0, rate-limited, and
             Google may use the content to improve its products, so it is HARD-LIMITED to the
             operator's own demo-cast repos: any other repo is refused before and at the model call.
             Labelled "Gemini API free tier".
vertex       Vertex AI via ADC. Billed.
"""

import asyncio
import contextvars
import heapq
import re
import threading
import time
from collections import deque
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from typing import Any

from google.adk.models.google_llm import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from google.genai.errors import ClientError

from judgebench.agents.offline import OFFLINE_MODEL, FakeLlm, offline_model
from judgebench.config import settings
from judgebench.errors import StageError
from judgebench.worker.github import parse_repo_url

FREE_TAG = " (Gemini API free tier)"
# The operator's own synthetic demo cast. Nothing else may be sent to the free tier.
FREE_TIER_REPOS = frozenset(
    f"auracodeskm/{r}"
    for r in (
        "team-alpha-docqa",
        "team-beta-insight",
        "team-gamma-planner",
        "team-delta-vision",
        "team-epsilon-notes",
        "team-zeta-assistant",
    )
)
_repo: contextvars.ContextVar[str | None] = contextvars.ContextVar("llm_repo", default=None)
_priority: contextvars.ContextVar[float] = contextvars.ContextVar("llm_priority", default=0.0)


def free_tier_allowed(repo_url: str | None) -> bool:
    ref = parse_repo_url(repo_url or "")
    return ref is not None and ref.ref is None and f"{ref.owner}/{ref.repo}".lower() in FREE_TIER_REPOS


class FreeTierRefused(StageError):
    def __init__(self, repo_url: str | None) -> None:
        super().__init__(
            "free_tier_repo_not_allowed",
            False,
            f"LLM_MODE=gemini_free only evaluates the allow-listed demo-cast repos; {repo_url or '(none)'} "
            "is not one. Use LLM_MODE=offline for real submissions.",
        )


def require_allowed(repo_url: str | None) -> None:
    if settings().llm_mode == "gemini_free" and not free_tier_allowed(repo_url):
        raise FreeTierRefused(repo_url)


@contextmanager
def submission_repo(repo_url: str | None, priority: float = 0.0) -> Iterator[None]:
    """Every model call for a submission runs inside this; the free-tier model checks it. `priority`
    (the submission time) lets the rate limiter serve the earliest-submitted team first."""
    require_allowed(repo_url)
    token, ptoken = _repo.set(repo_url), _priority.set(priority)
    try:
        yield
    finally:
        _repo.reset(token)
        _priority.reset(ptoken)


# Free-tier limits per model: (requests/minute, requests/day). Flash-Lite RPM measured 2026-11-19 from a 429's
# quotaValue (GenerateRequestsPerMinutePerProjectPerModel-FreeTier = 15); 3.6 Flash: 5/min, 20/day.
FREE_LIMITS = {
    "gemini-3.5-flash-lite": (15, 500),
    "gemini-3.1-flash-lite": (15, 500),
    "gemini-3.6-flash": (5, 20),
    "gemini-3.8-flash": (5, 20),
}
RESERVE = 3  # slots per minute kept for the earliest active team
_DELAY = re.compile(r"'retryDelay': '(\d+(?:\.\d+)?)s'")


class RateLimiter:
    """Sliding 60 s window, shared by every worker thread in the process, one per model. Stays one request
    under the real RPM so the free tier never has to refuse us. Counts every request sent and every retry."""

    def __init__(self, rpm: int) -> None:
        self.rpm, self.sent, self.retried = max(rpm, 1), 0, 0
        self._calls: deque[float] = deque()
        self._waiting: list[tuple[float, int]] = []
        self._active: dict[float, float] = {}  # priority -> last time it asked
        self._seq = 0
        self._lock = threading.Lock()

    def acquire(self, priority: float = 0.0) -> None:
        """Lowest `priority` first (the earliest-submitted team), then first come first served."""
        with self._lock:
            self._seq += 1
            me = (priority, self._seq)
            heapq.heappush(self._waiting, me)
            self._active[priority] = time.monotonic()
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= 60:
                    self._calls.popleft()
                # The earliest-submitted active team may use every slot; later teams leave RESERVE free for it,
                # so a burst of new arrivals can't stall the team the audience is watching.
                active = [p for p, seen in self._active.items() if now - seen < 30]
                limit = self.rpm if not active or priority <= min(active) else max(self.rpm - RESERVE, 1)
                if self._waiting[0] == me and len(self._calls) < limit:
                    heapq.heappop(self._waiting)
                    self._calls.append(now)
                    self.sent += 1
                    return
                wait = 0.05 if self._waiting[0] != me else 60 - (now - self._calls[0]) + 0.05
            time.sleep(min(wait, 1.0))


_limiters: dict[str, RateLimiter] = {}
_limiters_lock = threading.Lock()


def limiter(model: str) -> RateLimiter:
    with _limiters_lock:
        if model not in _limiters:
            rpm = settings().gemini_free_rpm or FREE_LIMITS.get(model, (10, 0))[0]
            _limiters[model] = RateLimiter(rpm - 1)  # just under the real limit
        return _limiters[model]


def limiter_stats() -> dict[str, tuple[int, int]]:
    """{model: (requests sent, of which retries after a per-minute 429)}."""
    return {m: (lim.sent, lim.retried) for m, lim in _limiters.items()}


class FreeGemini(Gemini):
    """Gemini API free tier. Refuses (fails closed) unless the current submission is allow-listed. Every
    request goes through the model's rate limiter; automatic function calling is off (no hidden calls);
    a per-minute 429 waits for the server's retryDelay and retries (counted); a per-day 429 is raised."""

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        if not free_tier_allowed(_repo.get()):
            raise FreeTierRefused(_repo.get())
        llm_request.config = llm_request.config or types.GenerateContentConfig()
        llm_request.config.automatic_function_calling = types.AutomaticFunctionCallingConfig(disable=True)
        lim = limiter(self.model)
        for attempt in range(5):
            lim.acquire(_priority.get())
            try:
                async for r in super().generate_content_async(llm_request, stream):
                    yield r
                return
            except ClientError as exc:
                if exc.code != 429 or "PerDay" in str(exc) or attempt == 4:
                    raise
                lim.retried += 1
                m = _DELAY.search(str(exc))
                await asyncio.sleep(float(m.group(1)) if m else 10.0)


# Server errors only: 429s are handled above, through the limiter, so every attempt is counted.
FREE_RETRY = types.HttpRetryOptions(
    attempts=3, initial_delay=5, max_delay=30, exp_base=2, jitter=0.5, http_status_codes=[500, 503]
)


def configured(name: str, free_model: str | None = None) -> "str | FakeLlm | FreeGemini":
    """`free_model`: a per-team override of GEMINI_FREE_MODEL (gemini_free only; never a fallback)."""
    cfg = settings()
    if cfg.llm_mode == "vertex":
        return name
    if cfg.llm_mode == "gemini_free":
        if not cfg.gemini_api_key:
            raise RuntimeError("LLM_MODE=gemini_free needs GEMINI_API_KEY (see M0_SETUP.md item 9)")
        return FreeGemini(  # one free-tier model for every agent: GEMINI_FREE_MODEL, not GEMINI_MODEL (Vertex)
            model=free_model or cfg.gemini_free_model,
            client_kwargs={"api_key": cfg.gemini_api_key, "vertexai": False},
            retry_options=FREE_RETRY,
        )
    return offline_model()


def model_label(model: Any) -> str:
    """Name persisted on evaluations/usage: the mode is part of it, so labels survive mode changes."""
    name = model if isinstance(model, str) else str(getattr(model, "model", "unknown"))
    return name + FREE_TAG if isinstance(model, FreeGemini) else name


def is_mock(model: str | None) -> bool:
    return model == OFFLINE_MODEL


def is_free(model: str | None) -> bool:
    return bool(model and model.endswith(FREE_TAG))


def display(model: str | None) -> str:
    """How an evaluation's model is shown to judges (Telegram, Sheet, reports)."""
    if is_mock(model):
        return "MOCK (offline, no model)"
    if is_free(model):
        return "Gemini API free tier: " + (model or "").removesuffix(FREE_TAG)
    return model or ""
