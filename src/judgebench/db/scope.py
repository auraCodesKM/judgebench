"""Event-scoped access (§13.2). Scope is derived from membership in the DB, never from an
event ID supplied in a request or chat message. Every query a judge can trigger goes through an
EventScope, so the event filter is applied before anything can reach a model prompt."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from judgebench.db.models import AuditEvent, Event, JudgeMembership, Submission, Team


class AccessDenied(Exception):
    pass


@dataclass(frozen=True)
class EventScope:
    actor: str
    event_ids: frozenset[uuid.UUID]

    def require(self, session: Session, event_id: uuid.UUID, action: str) -> None:
        if event_id in self.event_ids:
            return
        log_denial(session, self.actor, event_id, action)
        raise AccessDenied(f"{self.actor} may not access event {event_id}")

    def find_team(self, session: Session, name: str) -> Team | None:
        return session.scalars(
            select(Team).where(Team.event_id.in_(self.event_ids), Team.name_key == name.casefold())
        ).first()

    def submissions(self, session: Session) -> Sequence[Submission]:
        return session.scalars(select(Submission).where(Submission.event_id.in_(self.event_ids))).all()


def scope_for_telegram_user(session: Session, telegram_user_id: int) -> EventScope:
    ids = session.scalars(
        select(JudgeMembership.event_id).where(JudgeMembership.telegram_user_id == telegram_user_id,
                                               JudgeMembership.status == "active")
    ).all()
    return EventScope(actor=f"telegram:{telegram_user_id}", event_ids=frozenset(ids))


def log_denial(session: Session, actor: str, event_id: uuid.UUID | None, action: str) -> None:
    """Adds the audit row to the caller's transaction; the caller commits."""
    event = session.get(Event, event_id) if event_id else None
    session.add(AuditEvent(
        tenant_id=event.tenant_id if event else None, event_id=event.id if event else None,
        actor=actor, action="access_denied", target=action[:200],
        detail={"requested_event_id": str(event_id)},
    ))
