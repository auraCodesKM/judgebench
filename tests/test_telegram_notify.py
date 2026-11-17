"""Telegram outbox drain (§6.4)."""

import json

import httpx
import pytest
from sqlalchemy.orm import Session

from judgebench.db import models as m
from judgebench.telegram.notify import TelegramBot, drain
from judgebench.workflow import State, transition
from tests.conftest import make_event, make_submission, make_team

pytestmark = pytest.mark.db
TOKEN = "123456:SECRET-token"


def bot(handler: list[httpx.Response] | None = None, sent: list[dict[str, object]] | None = None) -> TelegramBot:
    queue = list(handler or [])
    log = sent if sent is not None else []

    def respond(req: httpx.Request) -> httpx.Response:
        log.append(json.loads(req.content))
        return queue.pop(0) if queue else httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    return TelegramBot(TOKEN, httpx.Client(transport=httpx.MockTransport(respond)))


@pytest.fixture
def sub(session: Session) -> m.Submission:
    event = make_event(session, "ev")
    event.telegram_chat_id = -100123
    s = make_submission(session, make_team(session, event, "=Team Alpha"))
    transition(session, s.id, State.RECEIVED, State.PROFILED, message="Profiled (commit 3f2c1ab): Python")
    session.commit()
    return s


def test_sends_plain_text_with_team_prefix(session: Session, sub: m.Submission) -> None:
    sent: list[dict[str, object]] = []
    assert drain(session, bot(sent=sent)) == 1
    assert sent == [
        {
            "chat_id": -100123,
            "text": "[=Team Alpha] Profiled (commit 3f2c1ab): Python",
            "disable_web_page_preview": True,
        }
    ]  # no parse_mode
    note = session.query(m.Notification).one()
    assert note.sent_at is not None and note.telegram_message_id == 42
    assert drain(session, bot()) == 0  # never re-sent


def test_rate_limit_defers_without_losing_message(session: Session, sub: m.Submission) -> None:
    limited = httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 30}})
    assert drain(session, bot([limited])) == 0
    note = session.query(m.Notification).one()
    assert note.sent_at is None and note.next_attempt_at is not None
    assert drain(session, bot()) == 0  # still inside retry_after window


def test_errors_never_store_the_bot_token(session: Session, sub: m.Submission) -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed {req.url}")

    drain(session, TelegramBot(TOKEN, httpx.Client(transport=httpx.MockTransport(boom))))
    note = session.query(m.Notification).one()
    assert note.last_error and TOKEN not in note.last_error and "<bot-token>" in note.last_error


def test_event_level_message_has_no_team_prefix(session: Session, sub: m.Submission) -> None:
    session.query(m.Notification).update({"sent_at": m.utcnow()})
    session.add(
        m.Notification(
            tenant_id=sub.tenant_id, event_id=sub.event_id, submission_id=None, stage="EVENT_READY", text="Event ready"
        )
    )
    session.commit()
    sent: list[dict[str, object]] = []
    drain(session, bot(sent=sent))
    assert sent[0]["text"] == "Event ready"
