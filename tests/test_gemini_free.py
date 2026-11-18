"""LLM_MODE=gemini_free: the AI Studio free tier may use content to improve Google's products, so it
is hard-limited to the operator's demo-cast repos. Refused before the stage and again at the model."""

import asyncio
from typing import Any

import pytest
from google.adk.models.llm_request import LlmRequest
from sqlalchemy.orm import Session, sessionmaker

from judgebench import config
from judgebench.agents import modes
from judgebench.db import models as m
from judgebench.google.sheets_sync import build_tabs
from judgebench.states import State
from tests import fixtures_repos as repos
from tests.fake_llm import fake
from tests.test_m2_flow import deps, drive, lines, submit


@pytest.fixture
def free(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = config.Settings(**{**config.settings().__dict__, "llm_mode": "gemini_free", "gemini_api_key": "test-key"})
    monkeypatch.setattr(modes, "settings", lambda: cfg)


@pytest.mark.parametrize(
    "url,ok",
    [
        ("https://github.com/auraCodesKM/team-alpha-docqa", True),
        ("https://github.com/auracodeskm/TEAM-BETA-INSIGHT.git", True),
        ("https://github.com/auraCodesKM/team-alpha-docqa/tree/other", False),  # only the default branch
        ("https://github.com/auraCodesKM/some-other-repo", False),
        ("https://github.com/evil/team-alpha-docqa", False),
        ("https://gitlab.com/auraCodesKM/team-alpha-docqa", False),
        (None, False),
    ],
)
def test_allow_list(url: str | None, ok: bool) -> None:
    assert modes.free_tier_allowed(url) is ok


def test_free_model_fails_closed_without_an_allowed_repo(free: None) -> None:
    model = modes.configured("gemini-2.5-flash")  # GEMINI_MODEL is for Vertex; free tier uses GEMINI_FREE_MODEL
    assert isinstance(model, modes.FreeGemini)
    assert modes.model_label(model) == "gemini-3.5-flash-lite (Gemini API free tier)"

    async def call() -> Any:
        async for r in model.generate_content_async(LlmRequest(model="gemini-2.5-flash")):
            return r

    with pytest.raises(modes.FreeTierRefused):
        asyncio.run(call())  # no submission context at all
    with pytest.raises(modes.FreeTierRefused), modes.submission_repo("https://github.com/acme/demo"):
        pass


def test_free_mode_needs_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = config.Settings(**{**config.settings().__dict__, "llm_mode": "gemini_free", "gemini_api_key": None})
    monkeypatch.setattr(modes, "settings", lambda: cfg)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        modes.configured("gemini-2.5-flash")


@pytest.mark.db
def test_pipeline_refuses_other_repos(free: None, session_factory: sessionmaker[Session], session: Session) -> None:
    sub = submit(session, "Real Team")  # repo_url https://github.com/acme/demo
    llm = fake()
    drive(session_factory, deps(repos.ALPHA_RAG, llm))
    session.refresh(sub)
    assert sub.state == State.FAILED and sub.error_category == "free_tier_repo_not_allowed"
    assert not llm.seen  # no model call was made
    assert "Failed: free_tier_repo_not_allowed (not retryable)" in lines(session, sub)
    assert "only evaluates the allow-listed demo-cast repos" in (sub.error_detail or "")


@pytest.mark.db
def test_allowed_repo_is_labelled_free_tier(
    free: None, monkeypatch: pytest.MonkeyPatch, session_factory: sessionmaker[Session], session: Session
) -> None:
    sub = submit(session, "Team Alpha")
    sub.repo_url = "https://github.com/auraCodesKM/team-alpha-docqa"
    session.commit()
    llm = fake()
    monkeypatch.setattr("judgebench.agents.runtime.model_name", lambda _: "gemini-2.5-flash" + modes.FREE_TAG)
    drive(session_factory, deps(repos.ALPHA_RAG, llm))
    session.refresh(sub)
    assert sub.state == State.READY_FOR_REVIEW and llm.seen
    out = lines(session, sub)
    assert any(t.startswith("[Gemini API free tier: gemini-2.5-flash] Evaluation ready") for t in out)
    assert any("$0.00 (Gemini API free tier)" in t for t in out)
    [row] = build_tabs(session, sub.event_id)["Leaderboard"].values()
    assert "Gemini API free tier: gemini-2.5-flash" in row
    assert all(u.cost_usd == 0 for u in session.query(m.UsageRecord).filter_by(submission_id=sub.id))


@pytest.mark.db
def test_quota_exhaustion_is_named(session_factory: sessionmaker[Session], session: Session) -> None:
    from google.genai.errors import ClientError

    from tests.fake_llm import Seen

    def exhausted(s: Seen) -> Any:
        raise ClientError(
            429,
            {
                "error": {
                    "code": 429,
                    "message": "quota",
                    "status": "RESOURCE_EXHAUSTED",
                    "details": [
                        {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"},
                        {"retryDelay": "79654s"},
                    ],
                }
            },
        )

    sub = submit(session, "Team Q")
    drive(session_factory, deps(repos.ALPHA_RAG, fake(claim_analyst=exhausted)))
    session.refresh(sub)
    assert sub.error_category == "model_quota_exhausted"
    assert sub.error_detail == "GenerateRequestsPerDayPerProjectPerModel-FreeTier; retry after 79654s"
