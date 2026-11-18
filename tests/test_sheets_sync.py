"""Judging workbook sync: a judge-edited cell is never overwritten (§6.3)."""

from collections.abc import Sequence
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from judgebench.db import models as m
from judgebench.google.sheets_sync import merge, sync_workbook
from judgebench.google.workspace import TABS


class FakeSheet:
    """In-memory workbook: read returns header + rows like the values API (UNFORMATTED_VALUE)."""

    def __init__(self) -> None:
        self.tabs: dict[str, list[list[Any]]] = {t: [list(h)] for t, h in TABS.items()}
        self.writes: list[str] = []

    def read(self, sid: str, tab: str) -> list[list[Any]]:
        return [list(r) for r in self.tabs[tab]]

    def write(self, sid: str, tab: str, rows: Sequence[Sequence[Any]]) -> None:
        self.writes.append(tab)
        self.tabs[tab] = [list(TABS[tab]), *[list(r) for r in rows]]

    def edit(self, tab: str, key: str, column: str, value: Any) -> None:
        row = next(r for r in self.tabs[tab][1:] if r[-1] == key)
        row[TABS[tab].index(column)] = value

    def cell(self, tab: str, key: str, column: str) -> Any:
        return next(r for r in self.tabs[tab][1:] if r[-1] == key)[TABS[tab].index(column)]


# --- pure merge -------------------------------------------------------------------------


def test_untouched_cells_update_and_edited_cells_survive() -> None:
    last = {"a": ["Alpha", "12"]}
    current = {"a": ["Alpha", 15]}  # judge changed 12 -> 15
    out = merge({"a": ["Alpha", 9]}, current, last)
    assert out.rows["a"] == ["Alpha", 15]
    assert out.overrides == [("a", 1, "9", "15")]
    assert out.written["a"] == ["Alpha", "12"]  # we keep remembering our value, so it stays "edited"
    again = merge({"a": ["Alpha", 7]}, {"a": ["Alpha", 15]}, out.written)
    assert again.rows["a"] == ["Alpha", 15]  # still never overwritten


def test_number_formats_are_not_mistaken_for_edits() -> None:
    out = merge({"a": [9.0, "x"]}, {"a": [9, "x"]}, {"a": ["9", "x"]})
    assert out.overrides == [] and out.rows["a"] == [9.0, "x"]


def test_cleared_cell_is_handed_back_to_the_system() -> None:
    assert merge({"a": [3]}, {"a": [""]}, {"a": ["1"]}).rows["a"] == [3]


def test_rows_we_drop_vs_rows_people_own() -> None:
    last = {"old": ["x"], "edited": ["y"]}
    current = {"old": ["x"], "edited": ["judge note"], "added-by-judge": ["mine"]}
    out = merge({}, current, last)
    assert "old" not in out.rows
    assert out.rows["edited"] == ["judge note"] and out.rows["added-by-judge"] == ["mine"]


# --- end to end over a fake workbook ---------------------------------------------------


@pytest.mark.db
def test_judge_edits_survive_reevaluation(session_factory: sessionmaker[Session], session: Session) -> None:
    from tests import fixtures_repos as repos
    from tests.fake_llm import fake
    from tests.test_m2_flow import deps, drive, submit

    sub = submit(session, "Team Alpha")
    drive(session_factory, deps(repos.ALPHA_RAG, fake()))
    event = session.get_one(m.Event, sub.event_id)
    session.add(
        m.EventIntegration(
            tenant_id=event.tenant_id, event_id=event.id, provider="google_sheets", external_ids={"spreadsheet_id": "S"}
        )
    )
    session.commit()
    sheet = FakeSheet()
    sync_workbook(session, event, sheet.read, sheet.write)

    key = f"crit:{sub.id}:model_quality"
    assert sheet.cell("Evaluations", key, "Score") == 9
    sheet.edit("Evaluations", key, "Score", 14)  # judge overrides the AI score
    sheet.edit("Evaluations", key, "Critic note", "Saw their eval notebook in person")
    sheet.tabs["Judge Reviews"].append(
        ["Team Alpha", "Judge One", "model_quality", 14, "strong demo", "eval shown live"]
    )

    # The DB changes (re-evaluation lowers the score again) and we sync twice.
    crit = session.query(m.CriterionScore).filter_by(criterion_id="model_quality").one()
    crit.score, crit.rationale = 8, "re-evaluated"
    session.commit()
    first = sync_workbook(session, event, sheet.read, sheet.write)
    second = sync_workbook(session, event, sheet.read, sheet.write)

    assert sheet.cell("Evaluations", key, "Score") == 14  # never overwritten
    assert sheet.cell("Evaluations", key, "Critic note") == "Saw their eval notebook in person"
    assert sheet.cell("Evaluations", key, "Rationale") == "re-evaluated"  # untouched cells still refresh
    assert first.overrides == 2 and second.overrides == 0  # recorded once
    audit = session.query(m.AuditEvent).filter_by(action="sheet_override").all()
    assert {(a.detail["column"], a.detail["ai_value"], a.detail["human_value"]) for a in audit} == {
        ("Score", "8", "14"),
        ("Critic note", "cited no evaluation evidence", "Saw their eval notebook in person"),
    }
    [review] = session.query(m.JudgeReview).all()
    assert (review.judge_label, review.human_scores, review.override_reason) == (
        "Judge One",
        {"model_quality": 14.0},
        "eval shown live",
    )
    assert crit.score == 8  # the AI recommendation stays separate from the human one

    writes = len(sheet.writes)
    sync_workbook(session, event, sheet.read, sheet.write)
    assert len(sheet.writes) == writes  # nothing changed: no Sheets writes


def test_audit_log_detail_is_readable() -> None:
    from judgebench.db.models import AuditEvent
    from judgebench.google.sheets_sync import audit_text

    def a(action: str, target: str | None, **detail: object) -> AuditEvent:
        return AuditEvent(actor="system", action=action, target=target, detail=detail)

    rid = "ACYDBNj8x2v0rRaw-Form-Response-Id"
    assert audit_text(a("state:RECEIVED", rid, version=2, late=False)) == "Submission received (v2)"
    assert rid not in audit_text(a("state:RECEIVED", rid, version=1, late=True))
    assert audit_text(a("state:RECEIVED", rid, version=1, late=True)) == "Submission received (v1) after the deadline"
    assert audit_text(a("state:PROFILED", "RECEIVED->PROFILED")) == "RECEIVED -> PROFILED"
    assert audit_text(a("state:FAILED", "EVALUATING->FAILED", category="free_tier_repo_not_allowed")) == (
        "EVALUATING -> FAILED: free_tier_repo_not_allowed"
    )
    assert audit_text(a("state:READY_FOR_REVIEW", "CRITIQUING->READY_FOR_REVIEW", total=57.0, confidence="low")) == (
        "CRITIQUING -> READY_FOR_REVIEW: total 57, confidence low"
    )
