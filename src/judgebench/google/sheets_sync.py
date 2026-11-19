"""DB -> judging workbook, preserving every judge edit (§6.3).

Judge-facing tabs: Leaderboard (one row per team, best first), Team Detail (per criterion, grouped by
team), Evidence (claim-relevant findings only), Judge Reviews (judges type here). Hidden + protected:
System, Audit Log, Lists (dropdown sources). Every machine tab carries a stable row key in its last,
hidden column, so edits follow their team when rows re-sort. On each sync:

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
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from judgebench.agents.modes import display
from judgebench.db.models import (
    AuditEvent,
    Claim,
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
from judgebench.google.workspace import JUDGE_EDITABLE, KEY, TABS, Link, link, links
from judgebench.judging import clip, dejargon, finding_name, sentence, top_concern
from judgebench.worker.github import parse_repo_url

Cell = str | int | float | Link
Reader = Callable[[str, str], list[list[Any]]]  # (spreadsheet_id, tab) -> rows incl. header
Writer = Callable[[str, str, Sequence[Sequence[Cell]]], None]  # rows excl. header
CATEGORY_LABELS = {
    "traditional_ml": "ML",
    "llm_rag": "LLM/RAG",
    "agentic": "Agentic",
    "vision_multimodal": "Vision",
    "ai_infrastructure": "AI infra",
}
_NUM = re.compile(r"^-?\d+(\.\d+)?$")


def norm(v: Any) -> str:
    """Compare cells the way Sheets returns them: 9, 9.0 and "9" are the same value."""
    if v is None:
        return ""
    if isinstance(v, Link):
        return v.text
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


def _rubric(event: Event) -> tuple[list[Any], list[str]]:
    """(criteria, judge names) from the event config; the default rubric if the config has none."""
    from judgebench.event_config import DEFAULT_RUBRIC, EventConfig

    try:
        cfg = EventConfig.model_validate(event.config)
        return list(cfg.rubric), [j.name for j in cfg.judges]
    except ValueError:
        return list(DEFAULT_RUBRIC), []


def _source(e: Evidence, sub: Submission) -> tuple[str, str | None]:
    """(label, https link) for an evidence item: GitHub file#line at the pinned commit, CI run, app, video."""
    ref = parse_repo_url(sub.repo_url or "")
    gh = f"https://github.com/{ref.owner}/{ref.repo}" if ref else None
    p = e.provenance or {}
    loc = e.source_locator or ""
    if p.get("path") and gh:
        a, b = p.get("line_start"), p.get("line_end")
        lines = f"#L{a}" + (f"-L{b}" if b and b != a else "") if a else ""
        return f"{p['path']}{lines}", f"{gh}/blob/{sub.commit_sha or 'HEAD'}/{p['path']}{lines}"
    if loc.startswith("ci:run/") and gh:
        return f"CI run {loc[7:]}", f"{gh}/actions/runs/{loc[7:]}"
    if e.source_type == "ci":
        return "CI", f"{gh}/actions" if gh else None
    if e.source_type == "live_check":
        return (loc[5:] if loc.startswith("live:") else "live app"), sub.deployed_url
    if e.source_type == "demo":
        at = loc.split("#", 1)[1] if loc.startswith("demo#") else ""
        return "demo video" + (f" at {at}" if at else ""), sub.demo_url
    if e.source_type == "form":
        return "submission form", None
    if gh and loc == "repository":
        return "repository", f"{gh}/tree/{sub.commit_sha or 'HEAD'}"
    if gh and re.fullmatch(r"[\w.\-/]+", loc) and ".." not in loc:  # a repo path without line numbers
        return loc, f"{gh}/blob/{sub.commit_sha or 'HEAD'}/{loc}"
    return loc[:120], None


def _concern(
    sub: Submission,
    ev: Evaluation | None,
    scores: list[CriterionScore],
    claims: list[Claim],
    evidence: list[Evidence],
    names: dict[str, str],
) -> str:
    if ev is None:
        if sub.state.value == "FAILED":
            return f"Not evaluated: {(sub.error_category or 'failed').replace('_', ' ')}"
        return f"Not evaluated yet ({sub.state.value.lower().replace('_', ' ')})"
    return top_concern(claims, evidence, scores, names)


CONFIDENCE = {"high": "high", "medium": "med", "low": "low"}


def build_tabs(
    session: Session, event_id: uuid.UUID, only: set[uuid.UUID] | None = None
) -> dict[str, dict[str, list[Cell]]]:
    """Every machine tab as {row key: cells}. Judge-facing order: by score, best first.
    `only`: restrict to these submissions (replays reveal teams one by one)."""
    event = session.get_one(Event, event_id)
    rubric, judges = _rubric(event)
    names = {c.id: c.name for c in rubric}
    tabs: dict[str, dict[str, list[Cell]]] = {t: {} for t in TABS if t not in JUDGE_EDITABLE}
    teams = []
    for team, sub in _latest_subs(session, event_id):
        if only is not None and sub.id not in only:
            continue
        ev = _ready_eval(session, sub)
        scores = (
            list(
                session.scalars(
                    select(CriterionScore)
                    .where(CriterionScore.evaluation_id == ev.id)
                    .order_by(CriterionScore.criterion_id)
                )
            )
            if ev
            else []
        )
        teams.append((team, sub, ev, scores))
    teams.sort(key=lambda t: (t[2] is None or t[2].total_score is None, -((t[2] and t[2].total_score) or 0), t[0]))

    rank = 0
    max_total = sum(c.weight for c in rubric)
    for team, sub, ev, scores in teams:
        claims = list(session.scalars(select(Claim).where(Claim.submission_id == sub.id).order_by(Claim.created_at)))
        attempt = sub.source_hashes.get("evidence_attempt")
        evidence = {
            str(e.id): e
            for e in session.scalars(
                select(Evidence)
                .where(Evidence.submission_id == sub.id, Evidence.stage_attempt == attempt)
                .order_by(Evidence.created_at, Evidence.key)
            )
        }
        scored = ev is not None and ev.total_score is not None
        rank += scored
        ref = parse_repo_url(sub.repo_url or "")
        questions = [
            dejargon(str(q)).replace("(from form:", "(from submission form:")
            for q in (ev.suggested_questions if ev else [])
        ]
        flags = sum(c.flagged for c in scores) + sum(c.verification_status == "CONTRADICTED" for c in claims)
        host = urlsplit(sub.deployed_url or "").hostname or ""
        tabs["Leaderboard"][f"team:{sub.team_id}"] = [
            rank if scored else "",
            team,
            str(sub.form_answers.get("project_title", ""))[:120],
            ev.total_score if ev and ev.total_score is not None else "",
            CONFIDENCE.get(ev.confidence or "", ev.confidence or "") if ev else "",
            flags if ev else "",
            _concern(sub, ev, scores, claims, list(evidence.values()), names),
            questions[0] if questions else "",
            link(f"{ref.owner}/{ref.repo}", sub.repo_url) if ref else (sub.repo_url or ""),
            link("Watch", sub.demo_url) if sub.demo_url else "",
            link(host, sub.deployed_url) if host else "",
            display(ev.model) if ev else "",
        ]
        tabs["System"][f"team:{sub.team_id}"] = [
            team,
            sub.version,
            (sub.commit_sha or "")[:7],
            sub.state.value,
            ", ".join(CATEGORY_LABELS.get(c, c) for c in sorted(_categories(session, sub))),
            sub.updated_at.strftime("%Y-%m-%d %H:%M"),
        ]

        cited = {i for c in scores for i in c.evidence_ids} | {str(i) for c in claims for i in c.evidence_ids}
        for eid, e in evidence.items():
            if eid in cited or e.status == "CONTRADICTED":
                label, uri = _source(e, sub)
                tabs["Evidence"][f"ev:{e.id}"] = [
                    team,
                    finding_name(e),
                    clip(e.observation, 300),
                    e.status,
                    link(label, uri),
                ]
        if ev is None:
            continue
        tabs["Team Detail"][f"hdr:{sub.id}"] = [
            team,
            "TEAM",
            f"{ev.total_score:g} / {max_total}" if scored else "",
            f"Confidence {CONFIDENCE.get(ev.confidence or '', ev.confidence)}; evaluated by {display(ev.model)}",
            "",
            f"{flags} flag{'s' if flags != 1 else ''}" if flags else "",
            "\n".join(f"• {q}" for q in questions[:5]),
        ]
        for c in scores:
            note = dejargon(c.critic_note or "")
            if c.pre_critic_score is not None and c.pre_critic_score != c.score:
                note = f"{c.pre_critic_score:g} → {c.score:g}: {note}".rstrip(": ")
            tabs["Team Detail"][f"crit:{sub.id}:{c.criterion_id}"] = [
                team,
                names.get(c.criterion_id, c.criterion_id),
                f"{c.score:g} / {c.max_score:g}",
                sentence(c.rationale),
                links([_source(evidence[i], sub) for i in c.evidence_ids if i in evidence][:4]),
                note,
                "",  # questions live in the team row: 3, grounded in evidence
            ]

    team_names = [t for t, *_ in teams]
    for n in range(max(len(team_names), len(rubric), len(judges))):
        tabs["Lists"][f"list:{n}"] = [
            team_names[n] if n < len(team_names) else "",
            rubric[n].name if n < len(rubric) else "",
            rubric[n].weight if n < len(rubric) else "",
            judges[n] if n < len(judges) else "",
        ]

    subs = {s.id: t for t, s, *_ in teams}
    for a in session.scalars(
        select(AuditEvent)
        .where(
            AuditEvent.event_id == event_id,
            AuditEvent.action.like("state:%") | (AuditEvent.action == "submission_rejected"),
        )
        .order_by(AuditEvent.created_at.desc())
        .limit(500)
    ):
        if only is not None and a.submission_id not in only:
            continue
        tabs["Audit Log"][f"audit:{a.id}"] = [
            a.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            subs.get(a.submission_id, "") if a.submission_id else str((a.detail or {}).get("team", ""))[:200],
            a.action,
            audit_text(a),
        ]
    return tabs


def write_rows(write: Callable[[str, list[list[Cell]]], None], tabs: dict[str, dict[str, list[Cell]]]) -> None:
    """Write whole machine tabs (no merge): for fresh workbooks such as replays, never the judging one."""
    for tab, header in TABS.items():
        if tab in JUDGE_EDITABLE:
            continue
        rows = tabs.get(tab, {})
        write(tab, [[*r, *[""] * (len(header) - 1 - len(r)), key] for key, r in rows.items()])


def _categories(session: Session, sub: Submission) -> dict[str, Any]:
    return (
        session.scalars(
            select(RepositoryProfileRow.categories)
            .where(RepositoryProfileRow.submission_id == sub.id)
            .order_by(RepositoryProfileRow.created_at.desc())
            .limit(1)
        ).first()
        or {}
    )


def audit_text(a: AuditEvent) -> str:
    """Readable Detail column. Raw IDs (e.g. the Form response ID) stay in the DB, not the Sheet."""
    d, state = a.detail or {}, a.action.removeprefix("state:")
    if a.action == "submission_rejected":
        return f"Rejected, not evaluated: {d.get('reason', '?')}"
    if state == "RECEIVED" and d.get("rerun_of"):
        return f"Re-evaluation requested by the organiser (v{d.get('version', '?')})"
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


def _remap(row: Sequence[Any], old: Sequence[str], new: Sequence[str]) -> list[Any]:
    """Move cells to the columns of the same name in `new` (layout migrations keep judge edits)."""
    at = {str(h): i for i, h in enumerate(old)}
    return [row[at[h]] if h in at and at[h] < len(row) else "" for h in new]


def _by_key(rows: list[list[Any]], header: list[str]) -> dict[str, list[Any]]:
    """Sheet rows by their row key, columns arranged as `header` (whatever layout the sheet has now)."""
    if not rows:
        return {}
    old = [str(h) for h in rows[0]]
    if KEY not in old:
        return {}
    k = old.index(KEY)
    return {str(r[k]): _remap(r, old, header)[:-1] for r in rows[1:] if len(r) > k and str(r[k]).strip()}


Layout = Callable[[str], None]  # (spreadsheet_id) -> apply formatting


def sync_workbook(
    session: Session, event: Event, read: Reader, write: Writer, layout: Layout | None = None
) -> SyncReport:
    from judgebench import tracing

    with tracing.span("sheets.publish", event_id=event.id) as s:
        report = _sync(session, event, read, write, layout)
        s.set_attributes({"jb.tabs_written": len(report.written_tabs), "jb.overrides": report.overrides})
        return report


def _sync(session: Session, event: Event, read: Reader, write: Writer, layout: Layout | None) -> SyncReport:
    from judgebench.google.layout import LAYOUT_VERSION

    integ = session.scalars(
        select(EventIntegration).where(
            EventIntegration.event_id == event.id, EventIntegration.provider == "google_sheets"
        )
    ).one()
    sid = integ.external_ids["spreadsheet_id"]
    if layout is not None and integ.cursor.get("layout") != LAYOUT_VERSION:
        layout(sid)  # new tabs, widths, colours, dropdowns, hidden + protected tabs
        integ.cursor = {**integ.cursor, "layout": LAYOUT_VERSION}
    snapshots: dict[str, Any] = dict(integ.cursor.get("tabs", {}))
    headers: dict[str, list[str]] = dict(integ.cursor.get("headers", {}))
    seen_overrides = set(integ.cursor.get("overrides", []))
    report = SyncReport()
    for tab, new in build_tabs(session, event.id).items():
        header = TABS[tab]
        sheet = read(sid, tab)
        current = _by_key(sheet, header)
        old = headers.get(tab) or ([str(h) for h in sheet[0]] if sheet else header)
        last = {k: _remap([*v, k], old, header)[:-1] for k, v in snapshots.get(tab, {}).items()}
        m = merge(new, current, last)
        rows = [[*r, *[""] * (len(header) - 1 - len(r)), key] for key, r in m.rows.items()]
        before = [[norm(v) for v in [*current[k], *[""] * (len(header) - 1 - len(current[k])), k]] for k in current]
        if not sheet or [str(h) for h in sheet[0]] != header or [[norm(v) for v in r] for r in rows] != before:
            write(sid, tab, rows)
            report.written_tabs.append(tab)
        snapshots[tab] = m.written
        headers[tab] = header
        for key, col, ai, human in m.overrides:
            marker = f"{tab}|{key}|{header[col]}|{human}"
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
    integ.cursor = {
        **integ.cursor,
        "tabs": snapshots,
        "headers": headers,
        "overrides": sorted(seen_overrides),
    }
    session.commit()
    return report


def ingest_reviews(session: Session, event: Event, rows: list[list[Any]]) -> int:
    """Judge Reviews tab -> judge_reviews (source=sheet). The Sheet is authoritative for these rows,
    so the event's sheet-sourced reviews are replaced by the current tab contents. Criteria may be
    picked by name (the dropdown) or typed as their id; both are stored as the id."""
    teams = {t.lower(): s for t, s in _latest_subs(session, event.id)}
    rubric, _ = _rubric(event)
    crit_ids = {c.name.lower(): c.id for c in rubric} | {c.id.lower(): c.id for c in rubric}
    session.query(JudgeReview).filter(JudgeReview.event_id == event.id, JudgeReview.source == "sheet").delete()
    count = 0
    for r in rows[1:]:
        team, judge, criterion, score, comment = (str(v).strip() for v in [*r, *[""] * 5][:5])
        sub = teams.get(team.lower())
        ev = _ready_eval(session, sub) if sub else None
        if ev is None or not (score or comment):
            continue
        crit = crit_ids.get(criterion.lower(), criterion[:60]) or None
        session.add(
            JudgeReview(
                tenant_id=event.tenant_id,
                event_id=event.id,
                evaluation_id=ev.id,
                judge_label=judge[:200],
                source="sheet",
                criterion_id=crit,
                human_scores={crit or "overall": float(score)} if _NUM.match(score) else {},
                comments=comment or None,
            )
        )
        count += 1
    return count
