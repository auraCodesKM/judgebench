"""Which model the agents use (LLM_MODE), and how each mode is labelled wherever results surface.

offline      FakeLlm with conservative heuristics. No network, $0. Labelled MOCK.
gemini_free  Gemini API free tier (AI Studio key, project without billing). $0, rate-limited, and
             Google may use the content to improve its products, so it is HARD-LIMITED to the
             operator's own demo-cast repos: any other repo is refused before and at the model call.
             Labelled "Gemini API free tier".
vertex       Vertex AI via ADC. Billed.
"""

import contextvars
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from typing import Any

from google.adk.models.google_llm import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

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
def submission_repo(repo_url: str | None) -> Iterator[None]:
    """Every model call for a submission runs inside this; the free-tier model checks it."""
    require_allowed(repo_url)
    token = _repo.set(repo_url)
    try:
        yield
    finally:
        _repo.reset(token)


class FreeGemini(Gemini):
    """Gemini API free tier. Refuses (fails closed) unless the current submission is allow-listed."""

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        if not free_tier_allowed(_repo.get()):
            raise FreeTierRefused(_repo.get())
        async for r in super().generate_content_async(llm_request, stream):
            yield r


# 429 = free-tier rate limit; back off exponentially (10 s .. 120 s) before the stage gives up and retries.
FREE_RETRY = types.HttpRetryOptions(
    attempts=6, initial_delay=10, max_delay=120, exp_base=2, jitter=0.5, http_status_codes=[429, 500, 503]
)


def configured(name: str) -> "str | FakeLlm | FreeGemini":
    cfg = settings()
    if cfg.llm_mode == "vertex":
        return name
    if cfg.llm_mode == "gemini_free":
        if not cfg.gemini_api_key:
            raise RuntimeError("LLM_MODE=gemini_free needs GEMINI_API_KEY (see M0_SETUP.md item 9)")
        return FreeGemini(  # one free-tier model for every agent: GEMINI_FREE_MODEL, not GEMINI_MODEL (Vertex)
            model=cfg.gemini_free_model,
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
