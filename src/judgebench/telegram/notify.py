"""Telegram stage notifications (§6.4): drain the outbox written by state transitions.

- Plain text only (no parse_mode), so team names can't inject markup or links.
- 429: honour retry_after and stop draining that batch, keeping per-chat order.
- At-least-once: a crash between Telegram's 200 and our commit re-sends one message.
- One new message per stage (cut-order item 4: edit-in-place deferred).
- The bot token sits in the URL path, so httpx request logging is silenced and every error
  string is scrubbed before it reaches the DB.
"""

import logging
from datetime import timedelta

import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from judgebench.db.models import Event, Notification, Submission, Team, utcnow

logging.getLogger("httpx").setLevel(logging.WARNING)
MAX_TEXT = 4000


class RateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__(f"rate limited for {retry_after}s")
        self.retry_after = retry_after


class TelegramBot:
    def __init__(self, token: str, client: httpx.Client | None = None) -> None:
        self._token = token
        self._http = client or httpx.Client(timeout=15)

    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "<bot-token>")

    def send(self, chat_id: int, text: str) -> int:
        try:
            resp = self._http.post(f"https://api.telegram.org/bot{self._token}/sendMessage",
                                   json={"chat_id": chat_id, "text": text[:MAX_TEXT],
                                         "disable_web_page_preview": True})
        except httpx.HTTPError as exc:
            raise RuntimeError(self._scrub(f"{type(exc).__name__}: {exc}")) from None
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code == 429:
            raise RateLimited(int(body.get("parameters", {}).get("retry_after", 5)))
        if not body.get("ok"):
            raise RuntimeError(self._scrub(f"HTTP {resp.status_code}: {body.get('description', '')}"))
        return int(body["result"]["message_id"])


def format_message(team: str | None, text: str) -> str:
    team = " ".join((team or "").split())[:80]
    return f"[{team}] {text}" if team else text


def drain(session: Session, bot: TelegramBot, *, limit: int = 20) -> int:
    """Send pending notifications in order. Returns how many were sent. Commits."""
    now = utcnow()
    rows = session.execute(
        select(Notification, Event.telegram_chat_id, Team.name)
        .join(Event, Event.id == Notification.event_id)
        .outerjoin(Submission, Submission.id == Notification.submission_id)
        .outerjoin(Team, Team.id == Submission.team_id)
        .where(Notification.sent_at.is_(None), Event.telegram_chat_id.is_not(None),
               or_(Notification.next_attempt_at.is_(None), Notification.next_attempt_at <= now))
        .order_by(Notification.id)
        .limit(limit)
        .with_for_update(of=Notification, skip_locked=True)
    ).all()
    sent = 0
    for note, chat_id, team in rows:
        note.attempts += 1
        try:
            note.telegram_message_id = bot.send(chat_id, format_message(team, note.text))
            note.sent_at, note.last_error = utcnow(), None
            sent += 1
        except RateLimited as exc:
            note.next_attempt_at = utcnow() + timedelta(seconds=exc.retry_after)
            break
        except RuntimeError as exc:
            note.last_error = str(exc)[:500]
            note.next_attempt_at = utcnow() + timedelta(seconds=min(300, 5 * 2 ** note.attempts))
    session.commit()
    return sent
