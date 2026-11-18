"""M2 exit condition with fakes at every credentialed edge (GitHub, probe, Gemini): a submission
goes from Form to a criterion-level evaluation with evidence from code, CI, live app and demo, and
every stage is announced in Telegram."""

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from judgebench.db import models as m
from judgebench.states import State
from judgebench.worker.github import GitHub, RepoRef, Tarball
from judgebench.worker.stages import Deps, run_next
from tests import fixtures_repos as repos
from tests.conftest import make_event, make_team
from tests.fake_llm import FakeLlm, fake
from tests.security import tarballs as tb
from tests.test_ci import FakeReader
from tests.test_live_check import FakeProbe

pytestmark = pytest.mark.db
SHA = "3f2c1ab" + "0" * 33


class RepoGitHub(GitHub):
    def __init__(self, files: dict[str, str]) -> None:
        self.data = tb.tar_gz(files)
        self.downloads = 0

    def resolve_commit(self, ref: RepoRef, until: datetime) -> str:
        return SHA

    def download_tarball(self, ref: RepoRef, sha: str, dest: Path) -> Tarball:
        self.downloads += 1
        dest.write_bytes(self.data)
        return Tarball(dest, "abc", len(self.data), False)


def deps(
    files: dict[str, str], llm: FakeLlm, ci_runs: list[dict[str, Any]] | None = None, probe: FakeProbe | None = None
) -> Deps:
    runs = (
        ci_runs
        if ci_runs is not None
        else [{"id": 7, "name": "test", "path": ".github/workflows/test.yml", "conclusion": "success"}]
    )
    reader = FakeReader(runs, [{"id": 70, "name": "test", "conclusion": "success"}], ("pytest", 14))
    probe = probe or FakeProbe(
        {"status": 200, "latency_ms": 180, "content_type": "text/html"},
        {
            "status": 200,
            "latency_ms": 2100,
            "content_type": "application/json",
            "body_excerpt": '{"answer": "x", "sources": ["a.pdf"]}',
        },
    )
    return Deps(RepoGitHub(files), probe, llm, llm, ci_reader=lambda gh: reader)


def submit(session: Session, team: str, **answers: str) -> m.Submission:
    event = session.query(m.Event).first() or make_event(session, "campus")
    event.config = {
        "slug": "campus",
        "name": "Campus",
        "organisation": "Campus",
        "deadline": "2026-11-23T12:00:00+05:30",
        "telegram_chat_id": -100,
    }
    t = make_team(session, event, team)
    sub = m.Submission(
        tenant_id=t.tenant_id,
        event_id=t.event_id,
        team_id=t.id,
        form_response_id=team,
        version=1,
        repo_url="https://github.com/acme/demo",
        form_answers=answers,
        submitted_at=m.utcnow(),
        deployed_url=answers.get("deployed_url"),
        demo_url=answers.get("demo_url"),
    )
    session.add(sub)
    session.commit()
    return sub


def drive(factory: sessionmaker[Session], d: Deps, limit: int = 20) -> None:
    for _ in range(limit):
        if run_next(factory, d) is None:
            return
    raise AssertionError("pipeline did not settle")


def lines(session: Session, sub: m.Submission) -> list[str]:
    return [n.text for n in session.query(m.Notification).filter_by(submission_id=sub.id).order_by(m.Notification.id)]


def test_honest_rag_team_end_to_end(session_factory: sessionmaker[Session], session: Session) -> None:
    sub = submit(
        session,
        "Team Alpha",
        deployed_url="https://alpha.example.app/",
        demo_url="https://youtu.be/dQw4w9WgXcQ",
        sample_request='{"method":"POST","path":"/ask","body":{"q":"hi"},"expect":["sources"]}',
    )
    llm = fake()
    drive(session_factory, deps(repos.ALPHA_RAG, llm))
    session.refresh(sub)
    assert sub.state == State.READY_FOR_REVIEW

    # evidence from all four sources, with provenance
    kinds = {e.source_type for e in session.query(m.Evidence).filter_by(submission_id=sub.id)}
    assert {"code", "ci", "live_check", "demo"} <= kinds
    wired = session.query(m.Evidence).filter_by(submission_id=sub.id, key="rag.retrieval_on_request_path").one()
    assert wired.provenance["commit_sha"] == SHA and wired.provenance["file_sha256"]
    assert {b.type: b.status for b in session.query(m.BehaviourCheck)} == {
        "ci": "VERIFIED",
        "live_check": "VERIFIED",
        "demo": "PARTIALLY_VERIFIED",
    }

    # criterion-level evaluation; the critic cut the uncited 16/20
    ev = session.query(m.Evaluation).filter_by(submission_id=sub.id).one()
    scores = {c.criterion_id: c for c in session.query(m.CriterionScore).filter_by(evaluation_id=ev.id)}
    assert len(scores) == 6 and ev.status == "ready" and ev.total_score == sum(c.score for c in scores.values())
    mq = scores["model_quality"]
    assert (mq.pre_critic_score, mq.score, mq.flagged) == (16, 9, True)

    # ReAct trace persisted: reason -> tool -> observe -> answer
    run = session.query(m.AgentRun).filter_by(submission_id=sub.id, agent_name="claim_investigator").one()
    assert run.trace[0]["tool"] == "search_code" and "app/rag.py" in run.trace[0]["observation"]
    assert run.llm_calls == 2

    # hierarchical delegation: only the profiled adapters' specialists ran
    agents = {s.agent for s in llm.seen}
    assert {"rag_specialist", "generic_specialist"} <= agents and "ml_specialist" not in agents

    # demo was sent as a video part; never VERIFIED from the demo alone
    assert any(s.has_video for s in llm.seen if s.agent == "demo_analyst")

    out = lines(session, sub)
    assert out[0].startswith("Profiled (commit 3f2c1ab)")
    assert 'CI: "test" passed on 3f2c1ab (pytest, 14 tests)' in out
    assert "Live check: / 200 in 180 ms; sample query 200 in 2.1 s" in out
    assert any(x.startswith("Demo check: claim") for x in out)
    assert any(x.startswith("Claims: ") and "extracted" in x for x in out)
    assert (
        'Critic: "Model quality and evaluation 16/20" cited no evaluation evidence -> revised to 9/20, flagged for judges'
        in out
    )
    assert any(x.startswith("Evaluation ready: ") and "/100 (confidence " in x for x in out)
    assert any(x.startswith("Cost: ") and "tokens, $" in x for x in out)


def test_inflated_claims_are_clamped_not_verified(session_factory: sessionmaker[Session], session: Session) -> None:
    sub = submit(session, "Team Beta", demo_url="https://www.youtube.com/watch?v=abcdefghijk")
    drive(session_factory, deps(repos.BETA_RAG_UNWIRED, fake(), ci_runs=[]))
    claims = {c.text: c.verification_status for c in session.query(m.Claim).filter_by(submission_id=sub.id)}
    # The fake investigator said VERIFIED for everything; with no CI and no live app, code forbids it.
    assert claims and "VERIFIED" not in claims.values()
    out = lines(session, sub)
    assert "CI: no CI configured, marked NOT_VERIFIABLE, score not reduced" in out
    assert "Live check: no deployed app, marked NOT_VERIFIABLE, score not reduced" in out
    assert any("contradicted at 1:42" in x for x in out)


def test_crash_mid_evaluation_resumes_without_redoing_evidence(
    session_factory: sessionmaker[Session], session: Session
) -> None:
    sub = submit(session, "Team Gamma")
    boom = {"n": 0}

    def flaky(s: Any) -> dict[str, Any]:
        boom["n"] += 1
        if boom["n"] == 1:
            raise RuntimeError("worker killed mid-evaluation")
        from tests.fake_llm import evaluator

        return evaluator(s)

    llm = fake(rubric_evaluator=flaky)
    d = deps(repos.GAMMA_MULTI_AGENT, llm)
    drive(session_factory, d)
    session.refresh(sub)
    assert sub.state == State.READY_FOR_REVIEW
    analyst_calls = [s for s in llm.seen if s.agent == "claim_analyst"]
    assert len(analyst_calls) == 1  # evidence stage was not repeated
    attempts = {e.stage_attempt for e in session.query(m.Evidence).filter_by(submission_id=sub.id)}
    assert len(attempts) == 1
    assert session.query(m.Evaluation).filter_by(submission_id=sub.id).count() == 1


def test_budget_exceeded_goes_to_human_review(
    session_factory: sessionmaker[Session], session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgebench.agents import runtime

    monkeypatch.setattr(runtime, "settings", lambda: type("S", (), {"submission_token_budget": 10, "app_name": "jb"})())
    sub = submit(session, "Team Delta")
    drive(session_factory, deps(repos.ML_CLASSIFIER, fake()))
    session.refresh(sub)
    assert sub.state == State.REQUIRES_HUMAN
    assert any("Token budget reached" in x for x in lines(session, sub))
