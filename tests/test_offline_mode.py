"""LLM_MODE=offline: the full pipeline runs with zero model calls, and every model-shaped output is
labelled MOCK (Telegram, Sheets, evaluation model), so nobody mistakes it for a real judgement."""

import pytest
from sqlalchemy.orm import Session, sessionmaker

from judgebench.agents.modes import configured
from judgebench.agents.offline import OFFLINE_MODEL, offline_model
from judgebench.db import models as m
from judgebench.google.sheets_sync import build_tabs
from judgebench.states import State
from tests import fixtures_repos as repos
from tests.test_m2_flow import deps, drive, lines, submit


def test_default_mode_is_offline() -> None:
    from judgebench import config

    assert config.settings().llm_mode == "offline" or pytest.skip("LLM_MODE set in the environment")
    assert getattr(configured("gemini-2.5-flash"), "model", None) == OFFLINE_MODEL


def test_bad_llm_mode_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    from judgebench import config

    monkeypatch.setenv("LLM_MODE", "vertexx")
    config.settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="LLM_MODE"):
            config.settings()
    finally:
        monkeypatch.delenv("LLM_MODE")
        config.settings.cache_clear()


@pytest.mark.db
def test_offline_pipeline_is_labelled_mock(session_factory: sessionmaker[Session], session: Session) -> None:
    sub = submit(session, "Team Alpha", demo_url="https://youtu.be/dQw4w9WgXcQ")
    drive(session_factory, deps(repos.ALPHA_RAG, offline_model()))
    session.refresh(sub)
    assert sub.state == State.READY_FOR_REVIEW

    ev = session.query(m.Evaluation).filter_by(submission_id=sub.id, status="ready").one()
    assert ev.model == OFFLINE_MODEL
    out = lines(session, sub)
    assert any(t.startswith("MOCK evaluation (offline, no model") for t in out)
    assert any(t.startswith("Cost: $0.00, offline mock") for t in out)
    # never pretends to have watched the video or verified a claim
    assert not session.query(m.Evidence).filter_by(submission_id=sub.id, source_type="demo", status="OBSERVED").all()
    assert {c.verification_status for c in session.query(m.Claim).filter_by(submission_id=sub.id)} <= {
        "UNVERIFIED",
        "MISSING_EVIDENCE",
        "NOT_VERIFIABLE",
        "CONTRADICTED",
    }
    assert all(
        c.rationale.startswith("[offline mock]") for c in session.query(m.CriterionScore).filter_by(evaluation_id=ev.id)
    )
    tabs = build_tabs(session, sub.event_id)
    [row] = tabs["Leaderboard"].values()
    assert "MOCK (offline, no model)" in row


def test_run_refuses_modes_unsafe_for_a_real_event() -> None:
    from judgebench.cli.main import run_mode_problem

    assert run_mode_problem("offline", False) is None
    assert "would FAIL" in (run_mode_problem("gemini_free", True) or "")
    assert "billed" in (run_mode_problem("vertex", False) or "")
    assert run_mode_problem("vertex", True) is None
