"""Form polling ingestion: idempotent on response ID, versioned resubmissions (§6.2)."""

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.orm import Session

from judgebench.api.ingest import ingest_responses, parse_response, sync_event
from judgebench.db import models as m
from judgebench.states import State
from judgebench.worker.github import RepoRef, parse_repo_url
from tests.conftest import make_event

QUESTIONS = {"q1": "team_name", "q2": "repo_url", "q3": "categories", "q4": "deployed_url"}


def response(
    rid: str,
    team: str,
    repo: str = "https://github.com/acme/demo",
    ts: str = "2026-11-22T10:00:00Z",
    **extra: list[str],
) -> dict[str, Any]:
    def txt(*vals: str) -> dict[str, Any]:
        return {"textAnswers": {"answers": [{"value": v} for v in vals]}}

    answers = {"q1": txt(team), "q2": txt(repo), "q3": txt("LLM / RAG", "Agentic AI")}
    answers |= {k: txt(*v) for k, v in extra.items()}
    return {"responseId": rid, "createTime": ts, "lastSubmittedTime": ts, "answers": answers}


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/acme/demo", RepoRef("acme", "demo")),
        ("https://github.com/acme/demo.git", RepoRef("acme", "demo")),
        ("github.com/acme/demo/", RepoRef("acme", "demo")),
        ("http://www.github.com/acme/demo", RepoRef("acme", "demo")),
        ("https://github.com/acme/demo/tree/main", RepoRef("acme", "demo", "main")),
        ("https://github.com/acme/demo/commit/3f2c1ab", RepoRef("acme", "demo", "3f2c1ab")),
        ("https://gitlab.com/acme/demo", None),
        ("https://github.com/acme", None),
        ("https://github.com/acme/../etc", None),
        ("https://github.com.evil.com/acme/demo", None),
        ("https://github.com/acme/demo?x=https://169.254.169.254", None),
    ],
)
def test_parse_repo_url(url: str, expected: RepoRef | None) -> None:
    assert parse_repo_url(url) == expected


def test_parse_response_ignores_unknown_questions() -> None:
    raw = response("r1", "Alpha", q99=["injected"])
    parsed = parse_response(raw, QUESTIONS)
    assert parsed.answers == {
        "team_name": "Alpha",
        "repo_url": "https://github.com/acme/demo",
        "categories": ["LLM / RAG", "Agentic AI"],
    }
    assert parsed.submitted_at == datetime(2026, 11, 22, 10, tzinfo=UTC)


@pytest.fixture
def event(session: Session) -> m.Event:
    ev = make_event(session, "campus")
    ev.deadline = datetime(2026, 11, 23, tzinfo=UTC)
    session.commit()
    return ev


@pytest.mark.db
def test_ingest_is_idempotent_on_response_id(session: Session, event: m.Event) -> None:
    batch = [response("r1", "Alpha"), response("r2", "Beta")]
    assert len(ingest_responses(session, event, batch, QUESTIONS)) == 2
    session.commit()
    assert ingest_responses(session, event, batch + batch, QUESTIONS) == []  # redelivery
    session.commit()

    assert session.query(m.Submission).count() == 2
    assert session.query(m.Notification).filter_by(stage="RECEIVED").count() == 2
    assert all(s.state == State.RECEIVED for s in session.query(m.Submission))


@pytest.mark.db
def test_resubmission_creates_new_version_same_team(session: Session, event: m.Event) -> None:
    ingest_responses(
        session, event, [response("r1", "Alpha"), response("r2", "  ALPHA ", ts="2026-11-22T11:00:00Z")], QUESTIONS
    )
    session.commit()
    subs = session.query(m.Submission).order_by(m.Submission.version).all()
    assert [s.version for s in subs] == [1, 2]
    assert subs[0].team_id == subs[1].team_id
    assert session.query(m.Team).count() == 1


@pytest.mark.db
def test_invalid_repo_url_recorded_and_failed(session: Session, event: m.Event) -> None:
    ingest_responses(session, event, [response("r1", "Alpha", repo="https://evil.example/x")], QUESTIONS)
    session.commit()
    sub = session.query(m.Submission).one()
    assert sub.state == State.FAILED and sub.error_category == "invalid_repo_url"
    stages = [n.stage for n in session.query(m.Notification).order_by(m.Notification.id)]
    assert stages == ["RECEIVED", "FAILED"]


@pytest.mark.db
def test_late_submission_flagged_not_dropped(session: Session, event: m.Event) -> None:
    ingest_responses(session, event, [response("r1", "Alpha", ts="2026-11-24T00:00:00Z")], QUESTIONS)
    session.commit()
    note = session.query(m.Notification).one()
    assert "after the deadline" in note.text


@pytest.mark.db
def test_sync_event_advances_cursor_and_tolerates_overlap(session: Session, event: m.Event) -> None:
    session.add(
        m.EventIntegration(
            tenant_id=event.tenant_id,
            event_id=event.id,
            provider="google_forms",
            external_ids={"form_id": "F", "questions": QUESTIONS},
            status="ready",
            cursor={"reset_at": "2026-11-22T09:00:00Z"},
        )
    )
    session.commit()
    calls: list[str | None] = []
    feed = [response("r1", "Alpha", ts="2026-11-22T10:00:00Z")]

    def fetch(form_id: str, since: str | None) -> list[dict[str, Any]]:
        calls.append(since)
        return list(feed)  # ">=" cursor: the boundary response comes back again

    assert len(sync_event(session, event, fetch)) == 1
    feed.append(response("r2", "Beta", ts="2026-11-22T10:05:00Z"))
    assert len(sync_event(session, event, fetch)) == 1
    assert calls == [None, "2026-11-22T10:00:00Z"]
    assert session.query(m.Submission).count() == 2
    forms = session.query(m.EventIntegration).filter_by(provider="google_forms").one()
    assert forms.cursor["reset_at"] == "2026-11-22T09:00:00Z"  # other cursor keys survive ingestion
