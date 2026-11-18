"""DB -> judging workbook, preserving every judge edit (§6.3).

Machine tabs (Leaderboard, Evaluations, Evidence, Suggested Questions, Audit Log) carry a stable
row key in their last column. On each sync:

  current = what the Sheet shows now (read back)
  last    = what we wrote last time (stored in the DB, per cell)
  new     = what the DB says now

A cell is written only if it is empty or still equals `last`. Anything else was typed by a person
and is kept, forever, and recorded once as a `sheet_override` audit event with the AI value next
to the human one. Rows we no longer produce are dropped only if nobody edited them. Judge-added
rows are kept. The Judge Reviews tab is judge-owned: we only read it, into `judge_reviews`.

ponytail: read-merge-write is not atomic; an edit typed in the ~1 s between our read and write is
lost. Upgrade path: Sheets developer metadata + per-range conditional writes if judges hit it.
"""

import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from judgebench.agents.modes import display
from judgebench.db.models import (
    AuditEvent,
    CriterionScore,
    Evaluation,
    Event,
    EventIntegration,
    Evidence,
    JudgeReview,
    RepositoryProfileRow,
    Submission,
    Team,
)

Cell = str | int | float
Reader = Callable[[str, str], list[list[Any]]]  # (spreadsheet_id, tab) -> rows incl. header
Writer = Callable[[str, str, Sequence[Sequence[Cell]]], None]  # rows excl. header
CATEGORY_LABELS = {
    "traditional_ml": "ML",
    "llm_rag": "LLM/RAG",
    "agentic": "Agentic",
    "vision_multimodal": "Vision",
    "ai_infrastructure": "AI infra",
}
KEY_HEADER = "Row key (system)"
_NUM = re.compile(r"^-?\d+(\.\d+)?$")


def norm(v: Any) -> str:
    """Compare cells the way Sheets returns them: 9, 9.0 and "9" are the same value."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v).upper()
    if isinstance(v, int | float) or (isinstance(v, str) and _NUM.match(v.strip())):
        return f"{float(v):g}"
    return str(v)


@dataclass
class Merged:
    rows: dict[str, list[Cell]]
    written: dict[str, list[str]]  # the new `last` snapshot: our value per cell (not the human one)
    overrides: list[tuple[str, int, str, str]] = field(default_factory=list)  # (key, col, ai, human)


def merge(new: dict[str, list[Cell]], current: dict[str, list[Any]], last: dict[str, list[str]]) -> Merged:
    out = Merged({}, {})
    for key in [*new, *[k for k in current if k not in new]]:
        n, c, prev = new.get(key), current.get(key) or [], last.get(key, [])
        cur = [norm(c[i]) if i < len(c) else "" for i in range(max(len(c), len(n or [])))]
        mine = [prev[i] if i < len(prev) else None for i in range(len(cur))]
        if n is None:  # we no longer produce this row: drop it unless a person touched or added it
            if key not in last or any(v and v != mine[i] for i, v in enumerate(cur)):
                out.rows[key] = list(c)
                if key in last:
                    out.written[key] = prev
            continue
        row: list[Cell] = []
        written: list[str] = []
        for i, value in enumerate(n):
            if cur[i] in ("", mine[i], norm(value)):  # empty, untouched since our write, or already equal
                row.append(value)
                written.append(norm(value))
            else:  # a person changed it: keep theirs, keep remembering ours
                row.append(c[i])
                ours = mine[i]
                written.append(ours if ours is not None else norm(value))
                out.overrides.append((key, i, norm(value), cur[i]))
        out.rows[key] = row
        out.written[key] = written
    return out


# --- what the DB says -------------------------------------------------------------------


def _latest_subs(session: Session, event_id: uuid.UUID) -> list[tuple[str, Submission]]:
    latest = (
        select(Submission.team_id, func.max(Submission.version).label("v"))
        .where(Submission.event_id == event_id)
        .group_by(Submission.team_id)
        .subquery()
    )
    return [
        (t, s)
        for t, s in session.execute(
            select(Team.name, Submission)
            .join(Submission, Submission.team_id == Team.id)
            .join(latest, (latest.c.team_id == Submission.team_id) & (latest.c.v == Submission.version))
            .where(Team.event_id == event_id)
            .order_by(Team.name_key)
        ).all()
    ]


def _ready_eval(session: Session, sub: Submission) -> Evaluation | None:
    return session.scalars(
        select(Evaluation)
        .where(Evaluation.submission_id == sub.id, Evaluation.status == "ready")
        .order_by(Evaluation.created_at.desc())
        .limit(1)
    ).first()


def build_tabs(session: Session, event_id: uuid.UUID) -> dict[str, dict[str, list[Cell]]]:
    tabs: dict[str, dict[str, list[Cell]]] = {
        t: {} for t in ("Leaderboard", "Evaluations", "Evidence", "Suggested Questions", "Audit Log")
    }
    for team, sub in _latest_subs(session, event_id):
        cats = (
            session.scalars(
                select(RepositoryProfileRow.categories)
                .where(RepositoryProfileRow.submission_id == sub.id)
                .order_by(RepositoryProfileRow.created_at.desc())
                .limit(1)
            ).first()
            or {}
        )
        ev = _ready_eval(session, sub)
        tabs["Leaderboard"][f"team:{sub.team_id}"] = [
            team,
            sub.version,
            (sub.commit_sha or "")[:7],
            sub.state.value,
            ", ".join(CATEGORY_LABELS.get(c, c) for c in sorted(cats)),
            ev.total_score if ev and ev.total_score is not None else "",
            (ev.confidence or "") if ev else "",
            display(ev.model) if ev else "",
            sub.updated_at.strftime("%Y-%m-%d %H:%M"),
        ]
        short = {}
        attempt = sub.source_hashes.get("evidence_attempt")
        for e in session.scalars(
            select(Evidence)
            .where(Evidence.submission_id == sub.id, Evidence.stage_attempt == attempt)
            .order_by(Evidence.created_at, Evidence.key)
        ):
            short[str(e.id)] = str(e.id)[:8]
            tabs["Evidence"][f"ev:{e.id}"] = [
                team,
                str(e.id)[:8],
                e.claim or e.key,
                e.observation[:500],
                f"{e.source_type} @ {e.source_locator}"[:300],
                e.status,
                round(e.confidence, 2),
            ]
        if ev is None:
            continue
        for c in session.scalars(
            select(CriterionScore).where(CriterionScore.evaluation_id == ev.id).order_by(CriterionScore.criterion_id)
        ):
            tabs["Evaluations"][f"crit:{sub.id}:{c.criterion_id}"] = [
                team,
                c.criterion_id,
                c.score,
                c.max_score,
                c.rationale[:1000],
                ", ".join(short.get(i, i[:8]) for i in c.evidence_ids),
                round(c.confidence, 2),
                c.pre_critic_score if c.pre_critic_score is not None else "",
                c.critic_note or "",
            ]
        for n, q in enumerate(ev.suggested_questions):
            tabs["Suggested Questions"][f"q:{sub.id}:{n}"] = [team, str(q), "evaluator"]
        for c in session.scalars(
            select(CriterionScore).where(
                CriterionScore.evaluation_id == ev.id, CriterionScore.suggested_question.is_not(None)
            )
        ):
            tabs["Suggested Questions"][f"q:{sub.id}:{c.criterion_id}"] = [
                team,
                c.suggested_question or "",
                c.criterion_id,
            ]
    teams = {s.id: t for t, s in _latest_subs(session, event_id)}
    for a in session.scalars(
        select(AuditEvent)
        .where(AuditEvent.event_id == event_id, AuditEvent.action.like("state:%"))
        .order_by(AuditEvent.created_at.desc())
        .limit(500)
    ):
        tabs["Audit Log"][f"audit:{a.id}"] = [
            a.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            teams.get(a.submission_id, "") if a.submission_id else "",
            a.action,
            audit_text(a),
        ]
    return tabs


def audit_text(a: AuditEvent) -> str:
    """Readable Detail column. Raw IDs (e.g. the Form response ID) stay in the DB, not the Sheet."""
    d, state = a.detail or {}, a.action.removeprefix("state:")
    if state == "RECEIVED":
        return f"Submission received (v{d.get('version', '?')})" + (" after the deadline" if d.get("late") else "")
    text = (a.target or state).replace("->", " -> ")
    if state == "FAILED":
        text += f": {d.get('category', 'unknown error')}"
    elif state == "READY_FOR_REVIEW" and "total" in d:
        text += f": total {d['total']:g}, confidence {d.get('confidence', '?')}"
    elif state == "REQUIRES_HUMAN":
        text += ": sent to judges for manual review"
    return text


# --- sync -------------------------------------------------------------------------------


@dataclass
class SyncReport:
    written_tabs: list[str] = field(default_factory=list)
    overrides: int = 0
    reviews: int = 0


def _by_key(rows: list[list[Any]], header: list[str]) -> dict[str, list[Any]]:
    k = len(header) - 1
    return {str(r[k]): r[:k] for r in rows[1:] if len(r) > k and str(r[k]).strip()}


def sync_workbook(session: Session, event: Event, read: Reader, write: Writer) -> SyncReport:
    from judgebench import tracing

    with tracing.span("sheets.publish", event_id=event.id) as s:
        report = _sync(session, event, read, write)
        s.set_attributes({"jb.tabs_written": len(report.written_tabs), "jb.overrides": report.overrides})
        return report


def _sync(session: Session, event: Event, read: Reader, write: Writer) -> SyncReport:
    from judgebench.google.workspace import TABS

    integ = session.scalars(
        select(EventIntegration).where(
            EventIntegration.event_id == event.id, EventIntegration.provider == "google_sheets"
        )
    ).one()
    sid = integ.external_ids["spreadsheet_id"]
    snapshots: dict[str, Any] = dict(integ.cursor.get("tabs", {}))
    seen_overrides = set(integ.cursor.get("overrides", []))
    report = SyncReport()
    for tab, new in build_tabs(session, event.id).items():
        header = TABS[tab]
        sheet = read(sid, tab)
        current = _by_key(sheet, header)
        m = merge(new, current, snapshots.get(tab, {}))
        rows = [[*r, *[""] * (len(header) - 1 - len(r)), key] for key, r in m.rows.items()]
        before = [[norm(v) for v in [*current[k], *[""] * (len(header) - 1 - len(current[k])), k]] for k in current]
        if not sheet or [str(h) for h in sheet[0]] != header or [[norm(v) for v in r] for r in rows] != before:
            write(sid, tab, rows)
            report.written_tabs.append(tab)
        snapshots[tab] = m.written
        for key, col, ai, human in m.overrides:
            marker = f"{tab}|{key}|{col}|{human}"
            if marker in seen_overrides:
                continue
            seen_overrides.add(marker)
            report.overrides += 1
            session.add(
                AuditEvent(
                    tenant_id=event.tenant_id,
                    event_id=event.id,
                    actor="sheet:judge",
                    action="sheet_override",
                    target=f"{tab}:{key}",
                    detail={"column": header[col], "ai_value": ai, "human_value": human},
                )
            )
    report.reviews = ingest_reviews(session, event, read(sid, "Judge Reviews"))
    integ.cursor = {**integ.cursor, "tabs": snapshots, "overrides": sorted(seen_overrides)}
    session.commit()
    return report


def ingest_reviews(session: Session, event: Event, rows: list[list[Any]]) -> int:
    """Judge Reviews tab -> judge_reviews (source=sheet). The Sheet is authoritative for these rows,
    so the event's sheet-sourced reviews are replaced by the current tab contents."""
    teams = {t.lower(): s for t, s in _latest_subs(session, event.id)}
    session.query(JudgeReview).filter(JudgeReview.event_id == event.id, JudgeReview.source == "sheet").delete()
    count = 0
    for r in rows[1:]:
        r = [*r, *[""] * 6][:6]
        team, judge, criterion, score, comment, reason = (str(v).strip() for v in r)
        sub = teams.get(team.lower())
        ev = _ready_eval(session, sub) if sub else None
        if ev is None or not (score or comment or reason):
            continue
        session.add(
            JudgeReview(
                tenant_id=event.tenant_id,
                event_id=event.id,
                evaluation_id=ev.id,
                judge_label=judge[:200],
                source="sheet",
                criterion_id=criterion[:60] or None,
                human_scores={criterion or "overall": float(score)} if _NUM.match(score) else {},
                comments=comment or None,
                override_reason=reason or None,
            )
        )
        count += 1
    return count
