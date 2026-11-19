"""Operator housekeeping: remove a team completely (dry runs, test submissions before an event opens).

`purge_team` deletes the bot's Telegram posts about the team, then the team; every submission,
evidence item, evaluation, score, claim, check, chunk, notification and judge review goes with it
(every table with a submission_id is cleared explicitly, cascade or not). Its audit rows are removed too, and one `team_purged` audit row with counts (no
content) is kept. The next Sheets sync drops the team's rows (rows nobody edited).
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from judgebench.db.models import AuditEvent, Base, Event, Notification, Submission, Team


@dataclass
class Purged:
    team: str
    submissions: int
    telegram_deleted: int
    telegram_failed: int


def purge_team(session: Session, event: Event, team_name: str, delete_message: Callable[[int, int], bool]) -> Purged:
    team = session.scalars(
        select(Team).where(Team.event_id == event.id, func.lower(Team.name) == team_name.strip().lower())
    ).one_or_none()
    if team is None:
        raise ValueError(f"no team {team_name!r} in {event.slug}")
    subs: list[uuid.UUID] = list(session.scalars(select(Submission.id).where(Submission.team_id == team.id)))
    chat = int(event.config.get("telegram_chat_id") or 0)
    ok = failed = 0
    for mid in session.scalars(  # distinct: a team's progress lines all share one (edited) message
        select(Notification.telegram_message_id)
        .where(Notification.submission_id.in_(subs), Notification.telegram_message_id.is_not(None))
        .distinct()
    ):
        if chat and mid is not None and delete_message(chat, mid):
            ok += 1
        else:
            failed += 1
    # Every table with a submission_id, whether or not its FK cascades (usage, audit, outbox, ...).
    for table in reversed(Base.metadata.sorted_tables):
        # usage_records stay: spend history, and the free-tier quota estimate needs them after a reset
        if "submission_id" in table.c and table.name not in ("submissions", "usage_records"):
            session.execute(delete(table).where(table.c.submission_id.in_(subs)))
    session.delete(team)
    session.add(
        AuditEvent(
            tenant_id=event.tenant_id,
            event_id=event.id,
            actor="operator",
            action="team_purged",
            target=None,
            detail={"submissions": len(subs), "telegram_deleted": ok, "telegram_failed": failed},
        )
    )
    session.flush()
    return Purged(team.name, len(subs), ok, failed)
