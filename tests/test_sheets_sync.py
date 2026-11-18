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
def _synced_event(session: Session, sub: m.Submission) -> m.Event:
    event = session.get_one(m.Event, sub.event_id)
    session.add(
        m.EventIntegration(
            tenant_id=event.tenant_id, event_id=event.id, provider="google_sheets", external_ids={"spreadsheet_id": "S"}
        )
    )
    session.commit()
    return event


def test_judge_edits_survive_reevaluation(session_factory: sessionmaker[Session], session: Session) -> None:
    from tests import fixtures_repos as repos
    from tests.fake_llm import fake
    from tests.test_m2_flow import deps, drive, submit

    sub = submit(session, "Team Alpha")
    drive(session_factory, deps(repos.ALPHA_RAG, fake()))
    event = _synced_event(session, sub)
    sheet = FakeSheet()
    sync_workbook(session, event, sheet.read, sheet.write)

    key = f"crit:{sub.id}:model_quality"
    assert sheet.cell("Team Detail", key, "Score / Max") == "9 / 20"
    sheet.edit("Team Detail", key, "Score / Max", "14 / 20")  # judge overrides the AI score
    sheet.edit("Team Detail", key, "Critic note", "Saw their eval notebook in person")
    sheet.tabs["Judge Reviews"].append(["Team Alpha", "Judge One", "Model quality and evaluation", 14, "strong demo"])

    # The DB changes (re-evaluation lowers the score again) and we sync twice.
    crit = session.query(m.CriterionScore).filter_by(criterion_id="model_quality").one()
    crit.score, crit.rationale = 8, "re-evaluated"
    session.commit()
    first = sync_workbook(session, event, sheet.read, sheet.write)
    second = sync_workbook(session, event, sheet.read, sheet.write)

    assert sheet.cell("Team Detail", key, "Score / Max") == "14 / 20"  # never overwritten
    assert sheet.cell("Team Detail", key, "Critic note") == "Saw their eval notebook in person"
    assert sheet.cell("Team Detail", key, "Reason") == "re-evaluated"  # untouched cells still refresh
    assert first.overrides == 2 and second.overrides == 0  # recorded once
    audit = session.query(m.AuditEvent).filter_by(action="sheet_override").all()
    assert {(a.detail["column"], a.detail["ai_value"], a.detail["human_value"]) for a in audit} == {
        ("Score / Max", "8 / 20", "14 / 20"),
        ("Critic note", "16 → 8: cited no evaluation evidence", "Saw their eval notebook in person"),
    }
    [review] = session.query(m.JudgeReview).all()  # picked by name in the dropdown, stored as the id
    assert (review.judge_label, review.criterion_id, review.human_scores, review.comments) == (
        "Judge One",
        "model_quality",
        {"model_quality": 14.0},
        "strong demo",
    )
    assert crit.score == 8  # the AI recommendation stays separate from the human one

    writes = len(sheet.writes)
    sync_workbook(session, event, sheet.read, sheet.write)
    assert len(sheet.writes) == writes  # nothing changed: no Sheets writes


def test_row_keys_keep_edits_with_their_team_when_ranks_change(
    session_factory: sessionmaker[Session], session: Session
) -> None:
    from tests import fixtures_repos as repos
    from tests.fake_llm import fake
    from tests.test_m2_flow import deps, drive, submit

    a, b = submit(session, "Team A"), submit(session, "Team B")
    drive(session_factory, deps(repos.ALPHA_RAG, fake()))
    ev_a, ev_b = (session.query(m.Evaluation).filter_by(submission_id=x.id).one() for x in (a, b))
    ev_a.total_score, ev_b.total_score = 70, 60
    event = _synced_event(session, a)
    sheet = FakeSheet()
    sync_workbook(session, event, sheet.read, sheet.write)
    assert [r[1] for r in sheet.tabs["Leaderboard"][1:]] == ["Team A", "Team B"]
    key_b = f"team:{b.team_id}"
    sheet.edit("Leaderboard", key_b, "Question to ask", "Ask B about latency")

    ev_b.total_score = 80  # B overtakes A: rows re-sort, ranks change
    session.commit()
    sync_workbook(session, event, sheet.read, sheet.write)
    rows = sheet.tabs["Leaderboard"][1:]
    assert [(r[0], r[1]) for r in rows] == [(1, "Team B"), (2, "Team A")]
    assert sheet.cell("Leaderboard", key_b, "Question to ask") == "Ask B about latency"  # followed its team
    assert sheet.cell("Leaderboard", f"team:{a.team_id}", "Question to ask") != "Ask B about latency"
    assert all(r[-1].startswith("team:") for r in rows)  # every row keeps its (hidden) key


def test_old_layout_migrates_without_duplicates(session_factory: sessionmaker[Session], session: Session) -> None:
    """A workbook written with the pre-2026-11-18 Leaderboard is remapped by column name, not appended."""
    from tests import fixtures_repos as repos
    from tests.fake_llm import fake
    from tests.test_m2_flow import deps, drive, submit

    sub = submit(session, "Team Alpha")
    drive(session_factory, deps(repos.ALPHA_RAG, fake()))
    event = _synced_event(session, sub)
    old_header = ["Team", "Version", "Commit", "Status", "Detected categories", "Score", "Confidence",
                  "Evaluated by", "Updated (UTC)", "Row key (system)"]  # fmt: skip
    key = f"team:{sub.team_id}"
    sheet = FakeSheet()
    sheet.tabs["Leaderboard"] = [old_header, ["Team Alpha", 1, "3f2c1ab", "READY", "", 57, "high", "x", "t", key]]
    sync_workbook(session, event, sheet.read, sheet.write)
    rows = sheet.tabs["Leaderboard"]
    assert rows[0] == TABS["Leaderboard"] and len(rows) == 2 and rows[1][-1] == key


def test_links_are_rich_text_and_https_only() -> None:
    from judgebench.google.workspace import Link, link, link_requests, links

    assert link("app", "javascript:alert(1)") == "app" and link("app", "http://x.io") == "app"
    cell = links([("app/rag.py#L3", "https://github.com/o/r/blob/s/app/rag.py#L3"), ("note", None)])
    assert isinstance(cell, Link) and cell.text == "app/rag.py#L3\nnote"
    [req] = link_requests(7, [["=1+1", cell]])
    upd = req["updateCells"]
    assert upd["start"] == {"sheetId": 7, "rowIndex": 1, "columnIndex": 1}
    assert upd["rows"][0]["values"][0]["textFormatRuns"] == [
        {"startIndex": 0, "format": {"link": {"uri": "https://github.com/o/r/blob/s/app/rag.py#L3"}}},
        {"startIndex": 13, "format": {}},
    ]
    assert "formulaValue" not in str(req)  # a link never becomes a formula


def test_layout_hides_and_protects_system_tabs_and_validates_reviews() -> None:
    from judgebench.google.layout import layout_requests

    meta = {"sheets": [{"properties": {"title": t, "sheetId": n}} for n, t in enumerate([*TABS, "Evaluations"])]}
    req = layout_requests(meta)
    props = {r["updateSheetProperties"]["properties"]["sheetId"]: r["updateSheetProperties"]["properties"]
             for r in req if "updateSheetProperties" in r}  # fmt: skip
    ids = {t: n for n, t in enumerate([*TABS, "Evaluations"])}
    assert {t for t, n in ids.items() if props[n]["hidden"]} == {"System", "Audit Log", "Lists", "Evaluations"}
    assert props[ids["Leaderboard"]]["index"] == 0 and all(
        p["gridProperties"]["frozenRowCount"] == 1 for p in props.values()
    )
    locked = [r["addProtectedRange"]["protectedRange"] for r in req if "addProtectedRange" in r]
    assert {p["range"]["sheetId"] for p in locked if not p["warningOnly"]} == {
        ids["System"],
        ids["Audit Log"],
        ids["Lists"],
    }
    assert ids["Judge Reviews"] not in {p["range"]["sheetId"] for p in locked}  # judges type freely here
    rules = [r["setDataValidation"]["rule"] for r in req if "setDataValidation" in r]
    assert [r["condition"]["type"] for r in rules] == ["ONE_OF_RANGE", "ONE_OF_RANGE", "ONE_OF_RANGE", "CUSTOM_FORMULA"]
    assert rules[2]["strict"] and rules[3]["strict"]  # criterion from the list, score within 0..max
    hidden_cols = [r for r in req if r.get("updateDimensionProperties", {}).get("properties") == {"hiddenByUser": True}]
    assert len(hidden_cols) == sum(h[-1] == "Row key (system)" for h in TABS.values())  # every key column hidden


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


@pytest.mark.db
def test_team_detail_and_leaderboard_for_judges(session_factory: sessionmaker[Session], session: Session) -> None:
    from judgebench.google.sheets_sync import _source, build_tabs
    from judgebench.google.workspace import Link
    from tests import fixtures_repos as repos
    from tests.fake_llm import fake
    from tests.test_m2_flow import deps, drive, submit

    sub = submit(session, "Team Alpha", demo_url="https://youtu.be/dQw4w9WgXcQ", project_title="DocQA")
    drive(session_factory, deps(repos.ALPHA_RAG, fake()))
    session.refresh(sub)
    tabs = build_tabs(session, sub.event_id)
    ev = session.query(m.Evaluation).filter_by(submission_id=sub.id).one()

    row = tabs["Team Detail"][f"crit:{sub.id}:model_quality"]
    assert row[2] == "9 / 20"  # post-critic score
    assert row[5] == "16 → 9: cited no evaluation evidence"  # the critic's change, with its reason
    assert tabs["Team Detail"][f"hdr:{sub.id}"][:3] == ["Team Alpha", "TEAM", f"{ev.total_score:g} / 100"]
    evidence_cells = [r[4] for k, r in tabs["Team Detail"].items() if k.startswith("crit:") and isinstance(r[4], Link)]
    assert any("github.com/acme/demo/actions/runs/7" in u for c in evidence_cells for *_, u in c.spans)
    code = session.query(m.Evidence).filter_by(submission_id=sub.id, key="rag.retrieval_on_request_path").one()
    ci_cfg = m.Evidence(source_type="code", source_locator=".github/workflows/test.yml", provenance={})
    sha = sub.commit_sha
    assert _source(ci_cfg, sub) == (
        ".github/workflows/test.yml",
        f"https://github.com/acme/demo/blob/{sha}/.github/workflows/test.yml",
    )
    assert _source(m.Evidence(source_type="code", source_locator="../etc/x", provenance={}), sub)[1] is None
    assert _source(code, sub) == (
        "app/rag.py#L12",
        f"https://github.com/acme/demo/blob/{sub.commit_sha}/app/rag.py#L12",
    )

    lb = tabs["Leaderboard"][f"team:{sub.team_id}"]
    assert lb[:4] == [1, "Team Alpha", "DocQA", ev.total_score]
    assert lb[5] >= 1 and "model_quality" in lb[6]  # flagged score -> flag count + biggest concern
    assert isinstance(lb[8], Link) and lb[8].text == "acme/demo" and str(lb[9]) == "Watch"
    assert all(not str(v).startswith(str(sub.id)) for v in lb)  # no internal IDs in judge-facing cells
    assert not any(str(e.id)[:8] in str(r) for e in session.query(m.Evidence) for r in tabs["Evidence"].values())


def test_layout_never_rewrites_machine_tab_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sync remaps old rows by the sheet's current header, so layout must leave machine headers alone."""
    from judgebench.google import layout

    calls: list[tuple[str, str, Any]] = []
    have = ["Leaderboard", "Evidence", "Judge Reviews", "Evaluations"]

    def fake_call(s: Any, method: str, url: str, **kw: Any) -> Any:
        calls.append((method, url, kw.get("json")))
        titles = have + ([t for t in TABS if t not in have] if any("addSheet" in str(c[2]) for c in calls) else [])
        return {"sheets": [{"properties": {"title": t, "sheetId": n}} for n, t in enumerate(titles)]}

    monkeypatch.setattr(layout, "_call", fake_call)
    layout.apply_layout(None, "S")  # type: ignore[arg-type]
    [values] = [j for m_, u, j in calls if u.endswith("values:batchUpdate")]
    written = {d["range"].split("!")[0].strip("'") for d in values["data"]}
    assert written == {"Judge Reviews", "Team Detail", "System", "Audit Log", "Lists"}  # new tabs + judge tab
    assert all(len(d["values"][0]) == 26 for d in values["data"])  # leftover header cells blanked
