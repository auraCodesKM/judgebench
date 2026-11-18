"""One trace per submission across every stage (§16.2); attributes are an allow-list."""

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy.orm import Session, sessionmaker

from judgebench import tracing
from judgebench.api.ingest import sync_event
from judgebench.db import models as m
from tests import fixtures_repos as repos
from tests.conftest import make_event
from tests.fake_llm import fake
from tests.test_ingest import QUESTIONS, response
from tests.test_m2_flow import deps, drive

pytestmark = pytest.mark.db
EXPORTER = InMemorySpanExporter()
tracing.add_exporter(EXPORTER)


def test_one_trace_covers_the_whole_submission(session_factory: sessionmaker[Session], session: Session) -> None:
    EXPORTER.clear()
    event = make_event(session, "campus")
    event.config = {
        "slug": "campus",
        "name": "Campus",
        "organisation": "Campus",
        "deadline": "2026-11-23T12:00:00+05:30",
        "telegram_chat_id": -100,
    }
    session.add(
        m.EventIntegration(
            tenant_id=event.tenant_id,
            event_id=event.id,
            provider="google_forms",
            external_ids={"form_id": "F", "questions": QUESTIONS},
            status="ready",
        )
    )
    session.commit()
    sync_event(session, event, lambda fid, since: [response("r1", "Team Alpha")])
    drive(session_factory, deps(repos.ALPHA_RAG, fake()))

    spans = EXPORTER.get_finished_spans()
    received = next(s for s in spans if s.name == "submission.received")
    tid = received.context.trace_id
    in_trace = {s.name for s in spans if s.context.trace_id == tid}
    for name in (
        "submission.received",
        "submission.profile",
        "submission.collect_evidence",
        "submission.evaluate",
        "submission.critique",
        "repository.fetch",
        "artefact.extract",
        "repository.profile",
        "evidence.collect",
        "behaviour.ci",
        "behaviour.live_check",
        "retrieval.index",
        "agent.claim_analyst",
        "agent.orchestrator",
        "agent.critic",
        "evaluation.persist",
    ):
        assert name in in_trace, name
    # ADK's own spans (agent, model call, tool call) join the same trace: the ReAct loop is visible in Cloud Trace.
    for name in (
        "invoke_agent claim_investigator",
        "generate_content fake-gemini",
        "execute_tool search_code",
        "invoke_agent rag_specialist",
    ):
        assert name in in_trace, name

    sub = session.query(m.Submission).one()
    stage = next(s for s in spans if s.name == "submission.evaluate")
    assert stage.attributes["jb.submission_id"] == str(sub.id) and stage.attributes["jb.stage"] == "EVALUATING"


def test_attributes_are_allow_listed() -> None:
    EXPORTER.clear()
    with tracing.span("x", submission_id="s1", prompt="SECRET PROMPT", source_code="def f(): pass", tokens=12):
        pass
    [s] = EXPORTER.get_finished_spans()
    assert dict(s.attributes or {}) == {"jb.submission_id": "s1", "jb.tokens": 12}
