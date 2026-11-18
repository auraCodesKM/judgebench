"""Event setup (idempotent, judge revocation) and the Leaderboard projection."""

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from judgebench.cli.setup import sync_judges, upsert_event
from judgebench.db import models as m
from judgebench.event_config import EventConfig, Judge
from judgebench.google.sheets_sync import sync_workbook
from judgebench.workflow import State, transition
from tests.conftest import make_submission, make_team
from tests.test_sheets_sync import FakeSheet

pytestmark = pytest.mark.db


def config(judges: list[Judge]) -> EventConfig:
    return EventConfig(
        slug="campus-2026",
        name="Campus Hack",
        organisation="Campus",
        telegram_chat_id=-100,
        deadline=datetime(2026, 11, 23, tzinfo=UTC),
        judges=judges,
    )


def test_rubric_weights_must_sum_to_100() -> None:
    from pydantic import ValidationError

    from judgebench.event_config import Criterion

    with pytest.raises(ValidationError):
        EventConfig(**{**config([]).model_dump(), "rubric": [Criterion(id="a", name="A", weight=50)]})


def test_setup_is_idempotent_and_revokes_removed_judges(session: Session) -> None:
    a, b = Judge(name="A", telegram_user_id=1), Judge(name="B", telegram_user_id=2)
    event = upsert_event(session, config([a, b]))
    sync_judges(session, event, config([a, b]))
    session.commit()
    again = upsert_event(session, config([a]))
    sync_judges(session, again, config([a]))
    session.commit()

    assert again.id == event.id and session.query(m.Event).count() == 1
    status = {j.telegram_user_id: j.status for j in session.query(m.JudgeMembership)}
    assert status == {1: "active", 2: "removed"}


def test_leaderboard_shows_latest_version_and_is_written_raw(session: Session) -> None:
    event = upsert_event(session, config([]))
    session.add(
        m.EventIntegration(
            tenant_id=event.tenant_id, event_id=event.id, provider="google_sheets", external_ids={"spreadsheet_id": "S"}
        )
    )
    team = make_team(session, event, '=HYPERLINK("http://x")')
    make_submission(session, team, "r1")
    v2 = make_submission(session, team, "r2", version=2)
    v2.commit_sha = "3f2c1ab" + "0" * 33
    session.flush()
    transition(session, v2.id, State.RECEIVED, State.PROFILED, message="x")
    session.add(
        m.RepositoryProfileRow(
            tenant_id=event.tenant_id,
            event_id=event.id,
            submission_id=v2.id,
            commit_sha=v2.commit_sha,
            profiler_version="0.1.0",
            categories={"llm_rag": 0.8, "agentic": 0.6},
            technologies=[],
            profile={},
        )
    )
    session.commit()

    sheet = FakeSheet()
    assert "Leaderboard" in sync_workbook(session, event, sheet.read, sheet.write).written_tabs
    rows = sheet.tabs["System"][1:]
    assert [r[:5] for r in rows] == [['=HYPERLINK("http://x")', 2, "3f2c1ab", "PROFILED", "Agentic, LLM/RAG"]]
    assert sheet.tabs["Leaderboard"][1][1] == '=HYPERLINK("http://x")'  # written RAW: text, never a formula
    assert sync_workbook(session, event, sheet.read, sheet.write).written_tabs == []  # unchanged -> no write
