"""Telegram authorisation (§6.5 steps 1-3, §13.5). M1 stub: the M3 Copilot webhook calls this
before any lookup. The event comes from the judge's membership, never from the chat text."""

import uuid

from sqlalchemy.orm import Session

from judgebench.db.scope import EventScope, scope_for_telegram_user


def authorise(session: Session, *, telegram_user_id: int, event_id: uuid.UUID, action: str) -> EventScope:
    scope = scope_for_telegram_user(session, telegram_user_id)
    scope.require(session, event_id, action)  # denies and logs
    return scope
