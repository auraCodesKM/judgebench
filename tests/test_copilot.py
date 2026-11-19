"""Judge Copilot commands over a fully evaluated submission (fakes for Gemini/GitHub/probe)."""

import pytest
from sqlalchemy.orm import Session, sessionmaker

from judgebench.db import models as m
from judgebench.retrieval import HashingEmbedder
from judgebench.telegram.copilot import Copilot
from tests import fixtures_repos as repos
from tests.conftest import make_judge
from tests.fake_llm import fake
from tests.test_m2_flow import deps, drive, submit

pytestmark = pytest.mark.db
JUDGE = 4242


@pytest.fixture
def judged(session_factory: sessionmaker[Session], session: Session) -> m.Submission:
    sub = submit(session, "Team Alpha")
    drive(session_factory, deps(repos.ALPHA_RAG, fake()))
    make_judge(session, session.get_one(m.Event, sub.event_id), JUDGE)
    session.commit()
    session.expire_all()  # the pipeline ran in other sessions; production uses a fresh session per webhook
    return sub


def test_team_and_evidence_commands(session: Session, judged: m.Submission) -> None:
    bot = Copilot(HashingEmbedder(), fake())
    team = bot.handle(session, JUDGE, "/team team alpha")
    assert team.startswith("Team Alpha v1 (3f2c1ab): READY_FOR_REVIEW")
    assert "- model_quality: 9/20 [flagged]" in team
    ev = bot.handle(session, JUDGE, "/evidence Team Alpha")
    assert "[VERIFIED] ci:run/7" in ev and "app/rag.py#L" in ev


def test_ask_answers_from_scoped_retrieval_with_sources(session: Session, judged: m.Submission) -> None:
    llm = fake()
    reply = Copilot(HashingEmbedder(), llm).handle(session, JUDGE, "/ask Team Alpha where is similarity_search called?")
    assert "[S1]" in reply and "Sources: " in reply
    [seen] = [s for s in llm.seen if s.agent == "copilot"]
    assert "app/rag.py#L" in seen.text  # the code that answers it was retrieved for the model
    assert '<untrusted_data source="judge_question">' in seen.text  # judge text is fenced too
    q = session.query(m.JudgeQuery).one()
    assert q.evidence_refs and q.query.startswith("/ask")
    assert session.query(m.UsageRecord).filter_by(stage="COPILOT").count() == 1


def test_non_judge_is_refused_and_logged(session: Session, judged: m.Submission) -> None:
    reply = Copilot(HashingEmbedder(), fake()).handle(session, 999, "/teams")
    assert reply.startswith("You are not an active judge")
    assert session.query(m.AuditEvent).filter_by(action="access_denied").count() == 1


def test_internal_drain_fails_closed_on_cloud_run_without_auth_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    import judgebench.telegram.app as tg

    monkeypatch.setenv("K_SERVICE", "jb-telegram")
    monkeypatch.delenv("SCHEDULER_SA_EMAIL", raising=False)
    assert TestClient(tg.app).post("/internal/notifications/drain").status_code == 503
    monkeypatch.setenv("SCHEDULER_SA_EMAIL", "jb-scheduler@p.iam.gserviceaccount.com")
    monkeypatch.setenv("INTERNAL_AUDIENCE", "https://jb-telegram.example")
    r = TestClient(tg.app).post("/internal/notifications/drain", headers={"Authorization": "Bearer forged"})
    assert r.status_code == 401


@pytest.mark.db
def test_same_team_name_in_two_events_resolves_to_the_newest(session: Session) -> None:
    from datetime import timedelta

    from judgebench.db.scope import EventScope
    from tests.conftest import make_event, make_team

    old_ev, new_ev = make_event(session, "old"), make_event(session, "new")
    old, new = make_team(session, old_ev, "Team Alpha"), make_team(session, new_ev, "Team Alpha")
    session.flush()
    old.created_at = new.created_at - timedelta(days=2)
    session.flush()
    scope = EventScope(actor="t", event_ids=frozenset({old_ev.id, new_ev.id}))
    assert scope.find_team(session, "team alpha").event_id == new_ev.id  # type: ignore[union-attr]


@pytest.mark.db
def test_sources_never_show_internal_ids(session: Session) -> None:
    from judgebench.telegram.copilot import _source
    from tests.conftest import make_event, make_submission, make_team

    sub = make_submission(session, make_team(session, make_event(session, "src"), "Team S"))
    e = m.Evidence(tenant_id=sub.tenant_id, event_id=sub.event_id, submission_id=sub.id, key="generic.tests", stage_attempt="a",
                   observation="No test functions found", source_type="code", source_locator="repository", status="MISSING",
                   confidence=0.9, verification_method="static_analysis", evaluator_version="x")  # fmt: skip
    session.add(e)
    session.flush()
    assert _source(session, f"evidence:{e.id}") == "check: Tests"
    assert _source(session, "README.md#1") == "README.md" and _source(session, "app.py#L1-L9") == "app.py#L1-L9"
