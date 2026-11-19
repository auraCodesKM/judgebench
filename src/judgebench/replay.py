"""`judgebench replay`: show a stored real run again, for a demo recording.

Re-renders the run's stored Telegram lines (compact, as live: progress edited in place, alerts and
cards as messages), in order, with the original gaps divided by `speed`, into a demo event's channel, and fills the demo workbook team by team as each one finishes. Nothing is
generated: every team line is a stored Notification text, every Sheet row comes from the stored
evaluation. The only added text is the header and footer saying this is a replay, of which run,
with which model, on which date.
"""

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from judgebench.agents.modes import display
from judgebench.db.models import AuditEvent, Evaluation, Event, JudgeMembership, Notification, Submission, Team
from judgebench.google.sheets_sync import Cell, build_tabs
from judgebench.telegram.compact import classify, progress_text
from judgebench.telegram.notify import format_message

MIN_GAP, MAX_GAP = 1.1, 20.0  # seconds between Telegram posts (rate limits; keep long waits watchable)


@dataclass
class Replayed:
    label: str
    messages: int
    teams: int


def run_label(session: Session, event: Event) -> str:
    models = sorted(
        {
            display(m)
            for m in session.scalars(
                select(Evaluation.model).where(Evaluation.event_id == event.id, Evaluation.status == "ready")
            )
            if m
        }
    )
    kind = " · synthetic benchmark teams" if event.slug.startswith("bench-") else ""
    return f"replay of run {event.slug}{kind}, {' + '.join(models) or 'no model'}, {event.created_at:%Y-%m-%d}"


def grant_judges(session: Session, demo: Event, source: Event) -> int:
    """Let the demo event's judges ask the Copilot about the replayed run (scoped, audited)."""
    have = {m.user_id for m in session.scalars(select(JudgeMembership).where(JudgeMembership.event_id == source.id))}
    added = 0
    for m in session.scalars(
        select(JudgeMembership).where(JudgeMembership.event_id == demo.id, JudgeMembership.status == "active")
    ):
        if m.user_id not in have:
            session.add(
                JudgeMembership(
                    tenant_id=source.tenant_id,
                    event_id=source.id,
                    user_id=m.user_id,
                    telegram_user_id=m.telegram_user_id,
                    status="active",
                )
            )
            added += 1
    if added:
        session.add(
            AuditEvent(
                tenant_id=source.tenant_id,
                event_id=source.id,
                actor="operator",
                action="judges_granted_for_replay",
                target=demo.slug,
                detail={"judges": added},
            )
        )
    session.flush()
    return added


def replay(
    session: Session,
    source: Event,
    send: Callable[[str], int],
    write: Callable[[dict[str, dict[str, list[Cell]]]], None],
    speed: float = 10.0,
    sleep: Callable[[float], None] = time.sleep,
    edit: Callable[[int, str], None] | None = None,
) -> Replayed:
    """Compact rendering (as live): one progress message per team edited in place, alerts and the
    summary card as new messages. Without `edit`, every stored line is sent (stages mode)."""
    label = run_label(session, source)
    rows = session.execute(
        select(Notification, Team.name, Submission.id, Submission.version)
        .join(Submission, Submission.id == Notification.submission_id)
        .join(Team, Team.id == Submission.team_id)
        .where(Notification.event_id == source.id)
        .order_by(Notification.created_at, Notification.id)
    ).all()
    last_line = {sid: n.id for n, _, sid, _ in rows}  # a team is "done" after its last stored line
    send(f"▶ Start of {label}. Every line below is stored output from that run, replayed at {speed:g}x.")
    shown: set[uuid.UUID] = set()
    progress: dict[uuid.UUID, tuple[int, set[str]]] = {}
    prev = None
    for note, team, sid, version in rows:
        if prev is not None:
            sleep(min(max((note.created_at - prev).total_seconds() / speed, MIN_GAP), MAX_GAP))
        prev = note.created_at
        kinds = classify(note.stage, note.text) if edit else {"alert"}
        if "alert" in kinds:
            send(format_message(team, note.text))
        if "progress" in kinds and edit:
            mid, done = progress.get(sid, (0, set()))
            done = done | {note.stage}
            text = progress_text(team, version, done)
            if mid:
                edit(mid, text)
            else:
                mid = send(text)
            progress[sid] = (mid, done)
        if last_line[sid] == note.id:
            shown.add(sid)
            write(build_tabs(session, source.id, only=set(shown)))
    send(f"■ End of {label}.")
    return Replayed(label, len(rows), len(shown))
