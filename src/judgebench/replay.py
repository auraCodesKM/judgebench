"""`judgebench replay`: show a stored real run again, for a demo recording.

Re-sends the run's stored Telegram lines, in order, with the original gaps divided by `speed`, into a
demo event's channel, and fills the demo workbook team by team as each one finishes. Nothing is
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
from judgebench.db.models import Evaluation, Event, Notification, Submission, Team
from judgebench.google.sheets_sync import Cell, build_tabs
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
    return f"replay of run {event.slug}, {' + '.join(models) or 'no model'}, {event.created_at:%Y-%m-%d}"


def replay(
    session: Session,
    source: Event,
    send: Callable[[str], None],
    write: Callable[[dict[str, dict[str, list[Cell]]]], None],
    speed: float = 10.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Replayed:
    label = run_label(session, source)
    rows = session.execute(
        select(Notification, Team.name, Submission.id)
        .join(Submission, Submission.id == Notification.submission_id)
        .join(Team, Team.id == Submission.team_id)
        .where(Notification.event_id == source.id)
        .order_by(Notification.created_at, Notification.id)
    ).all()
    last_line = {sid: n.id for n, _, sid in rows}  # a team is "done" after its last stored line
    send(f"▶ Start of {label}. Every line below is stored output from that run, replayed at {speed:g}x.")
    shown: set[uuid.UUID] = set()
    prev = None
    for note, team, sid in rows:
        if prev is not None:
            sleep(min(max((note.created_at - prev).total_seconds() / speed, MIN_GAP), MAX_GAP))
        prev = note.created_at
        send(format_message(team, note.text))
        if last_line[sid] == note.id:
            shown.add(sid)
            write(build_tabs(session, source.id, only=set(shown)))
    send(f"■ End of {label}.")
    return Replayed(label, len(rows), len(shown))
