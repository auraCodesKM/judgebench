"""Telegram stage notifications (§6.4): drain the outbox written by state transitions.

- Plain text only (no parse_mode), so team names can't inject markup or links.
- 429: honour retry_after and stop draining that batch, keeping per-chat order.
- At-least-once: a crash between Telegram's 200 and our commit re-sends one message.
- Compact mode (default, TELEGRAM_MODE=compact): one progress message per team edited in place, new
  messages only for alerts and the summary card (telegram/compact.py). TELEGRAM_MODE=stages: one
  message per stage, for debugging.
- Per-chat spacing (min_gap) keeps bursts under Telegram's group limits; pending updates for a team
  are coalesced into one edit per drain.
- The bot token sits in the URL path, so httpx request logging is silenced and every error
  string is scrubbed before it reaches the DB.
"""

import logging
import time
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from judgebench.db.models import Event, Notification, Submission, Team, utcnow
from judgebench.telegram.compact import PROGRESS_STAGES, classify, progress_text

logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)
MAX_TEXT = 4000


class RateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__(f"rate limited for {retry_after}s")
        self.retry_after = retry_after


class TelegramBot:
    def __init__(self, token: str, client: httpx.Client | None = None, min_gap: float = 0.0) -> None:
        self._token = token
        self._http = client or httpx.Client(timeout=15)
        self._min_gap, self._last = min_gap, dict[int, float]()

    def _space(self, chat_id: int) -> None:
        if self._min_gap:
            wait = self._last.get(chat_id, 0.0) + self._min_gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last[chat_id] = time.monotonic()

    def edit(self, chat_id: int, message_id: int, text: str) -> None:
        self._space(chat_id)
        log.info("telegram edit #%s: %s", message_id, text.replace("\n", " / "))
        try:
            resp = self._http.post(
                f"https://api.telegram.org/bot{self._token}/editMessageText",
                json={"chat_id": chat_id, "message_id": message_id, "text": text[:MAX_TEXT]},
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(self._scrub(f"{type(exc).__name__}: {exc}")) from None
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code == 429:
            raise RateLimited(int(body.get("parameters", {}).get("retry_after", 5)))
        if not body.get("ok") and "message is not modified" not in str(body.get("description", "")):
            raise RuntimeError(self._scrub(f"HTTP {resp.status_code}: {body.get('description', '')}"))

    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "<bot-token>")

    def send(self, chat_id: int, text: str) -> int:
        self._space(chat_id)
        log.info("telegram send: %s", text.replace("\n", " / "))
        try:
            resp = self._http.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json={"chat_id": chat_id, "text": text[:MAX_TEXT], "disable_web_page_preview": True},
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(self._scrub(f"{type(exc).__name__}: {exc}")) from None
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code == 429:
            raise RateLimited(int(body.get("parameters", {}).get("retry_after", 5)))
        if not body.get("ok"):
            raise RuntimeError(self._scrub(f"HTTP {resp.status_code}: {body.get('description', '')}"))
        return int(body["result"]["message_id"])

    def delete(self, chat_id: int, message_id: int) -> bool:
        """Delete one of the bot's own posts (a channel admin with Post Messages may)."""
        try:
            resp = self._http.post(
                f"https://api.telegram.org/bot{self._token}/deleteMessage",
                json={"chat_id": chat_id, "message_id": message_id},
            )
        except httpx.HTTPError:
            return False
        return bool(resp.headers.get("content-type", "").startswith("application/json") and resp.json().get("ok"))


def format_message(team: str | None, text: str) -> str:
    """ "Team Beta: text", unless the text already names the team (alerts and cards do)."""
    team = " ".join((team or "").split())[:80]
    return f"{team}: {text}" if team and team not in text else text


def drain(session: Session, bot: TelegramBot, *, limit: int = 20, mode: str | None = None) -> int:
    """Send pending notifications in order. Returns how many were handled. Commits."""
    from judgebench.config import settings

    mode = mode or settings().telegram_mode
    now = utcnow()
    rows = session.execute(
        select(Notification, Event.telegram_chat_id, Team.name, Submission.version)
        .join(Event, Event.id == Notification.event_id)
        .outerjoin(Submission, Submission.id == Notification.submission_id)
        .outerjoin(Team, Team.id == Submission.team_id)
        .where(
            Notification.sent_at.is_(None),
            Event.telegram_chat_id.is_not(None),
            or_(Notification.next_attempt_at.is_(None), Notification.next_attempt_at <= now),
        )
        .order_by(Notification.id)
        .limit(limit)
        .with_for_update(of=Notification, skip_locked=True)
    ).all()
    handled = 0
    progress: dict[Any, tuple[int, str | None, int | None, list[Notification]]] = {}
    try:
        for note, chat_id, team, version in rows:
            assert chat_id is not None  # filtered in the query
            note.attempts += 1
            kinds = {"alert"} if mode == "stages" else classify(note.stage, note.text)
            try:
                if "alert" in kinds:
                    note.telegram_message_id = bot.send(chat_id, format_message(team, note.text))
                if "progress" in kinds and note.submission_id is not None:
                    progress.setdefault(note.submission_id, (chat_id, team, version, []))[3].append(note)
                else:
                    note.sent_at, note.last_error = utcnow(), None
                handled += 1
            except RuntimeError as exc:
                note.last_error = str(exc)[:500]
                note.next_attempt_at = utcnow() + timedelta(seconds=min(300, 5 * 2**note.attempts))
        for sid, (p_chat, p_team, p_version, p_notes) in progress.items():
            _update_progress(session, bot, sid, p_chat, p_team, p_version, p_notes)
    except RateLimited as exc:
        for note, *_ in rows:
            if note.sent_at is None:
                note.next_attempt_at = utcnow() + timedelta(seconds=exc.retry_after)
    session.commit()
    return handled


def _update_progress(
    session: Session,
    bot: TelegramBot,
    sid: Any,
    chat_id: int,
    team: str | None,
    version: int | None,
    batch: list[Notification],
) -> None:
    """One send (first time) or one edit for all of this team's new progress lines."""
    all_notes = list(session.scalars(select(Notification).where(Notification.submission_id == sid)))
    pending = {n.id for n in batch}
    done = {n.stage for n in all_notes if n.sent_at is not None or n.id in pending}
    failed = any(n.stage in ("FAILED", "REQUIRES_HUMAN") for n in all_notes)
    text = format_message(None, progress_text(team, version, done, failed))
    # The team's progress message is the one sent for its earliest progress line (normally RECEIVED).
    anchor = min((n for n in all_notes if n.stage in PROGRESS_STAGES), key=lambda n: n.id, default=None)
    try:
        if anchor is not None and anchor.telegram_message_id and anchor.id not in pending:
            bot.edit(chat_id, anchor.telegram_message_id, text)
            mid = anchor.telegram_message_id
        else:
            mid = bot.send(chat_id, text)
            if anchor is not None:
                anchor.telegram_message_id = mid
    except RuntimeError as exc:
        for n in batch:
            n.last_error = str(exc)[:500]
            n.next_attempt_at = utcnow() + timedelta(seconds=min(300, 5 * 2**n.attempts))
        return
    for n in batch:
        n.sent_at, n.last_error = utcnow(), None
        n.telegram_message_id = n.telegram_message_id or mid
