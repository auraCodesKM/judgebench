"""Demo recording helpers: `reset-demo` (back to an empty, formatted event for a re-take) and
`demo-preflight` (everything a live take needs, checked before the camera rolls).

Both refuse any event whose slug does not contain "demo", so the real event can never be wiped.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from judgebench.admin import purge_team
from judgebench.agents.modes import FREE_TAG
from judgebench.db.models import AuditEvent, Event, EventIntegration, Notification, Team, UsageRecord

# Free-tier requests/day per model (DECISIONS.md: 3.6/3.8 Flash measured, Flash-Lite third-party).
FREE_DAILY = {
    "gemini-3.6-flash": 20,
    "gemini-3.8-flash": 20,
    "gemini-3.5-flash-lite": 500,
    "gemini-3.1-flash-lite": 500,
}
CALLS_PER_TEAM = 17  # measured worst case per team (budget 8, majority-of-3); retried requests also count


def require_demo(event: Event) -> None:
    if "demo" not in event.slug:
        raise SystemExit(f"{event.slug} is not a demo event (slug must contain 'demo'); refusing")


@dataclass
class ResetReport:
    teams: int = 0
    telegram_deleted: int = 0
    telegram_failed: int = 0


def reset_demo(
    session: Session,
    event: Event,
    delete_message: Callable[[int, int], bool],
    clear_workbook: Callable[[str], None],
) -> ResetReport:
    """Every team (rows, Telegram posts) and every event-level post goes; the Form cursor moves to now
    so earlier responses are never re-ingested; the workbook is emptied and re-formatted."""
    require_demo(event)
    report = ResetReport()
    for team in list(session.scalars(select(Team.name).where(Team.event_id == event.id))):
        r = purge_team(session, event, team, delete_message)
        report.teams += 1
        report.telegram_deleted += r.telegram_deleted
        report.telegram_failed += r.telegram_failed
    chat = event.telegram_chat_id
    for n in session.scalars(select(Notification).where(Notification.event_id == event.id)):
        if n.telegram_message_id and chat:
            ok = delete_message(chat, n.telegram_message_id)
            report.telegram_deleted += ok
            report.telegram_failed += not ok
    session.execute(delete(Notification).where(Notification.event_id == event.id))
    session.execute(delete(AuditEvent).where(AuditEvent.event_id == event.id))
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    for integ in session.scalars(select(EventIntegration).where(EventIntegration.event_id == event.id)):
        if integ.provider == "google_forms":
            integ.cursor = {"since": now}  # responses submitted before the reset stay in the Form, unread
        if integ.provider == "google_sheets":
            clear_workbook(integ.external_ids["spreadsheet_id"])
            integ.cursor = {}
    session.add(
        AuditEvent(
            tenant_id=event.tenant_id,
            event_id=event.id,
            actor="operator",
            action="demo_reset",
            detail={"teams": report.teams, "telegram_deleted": report.telegram_deleted},
        )
    )
    session.flush()
    return report


@dataclass
class Preflight:
    problems: list[str] = field(default_factory=list)
    ok: list[str] = field(default_factory=list)

    def check(self, good: bool, ok: str, problem: str) -> None:
        (self.ok if good else self.problems).append(ok if good else problem)

    def render(self) -> str:
        lines = [f"  ok    {m}" for m in self.ok] + [f"  FIX   {m}" for m in self.problems]
        return "\n".join(lines) + (
            "\n\nREADY" if not self.problems else f"\n\nNOT READY: {len(self.problems)} problem(s)"
        )


def calls_today(session: Session, model: str) -> int:
    """Our own recorded calls for `model` since midnight Pacific (when free-tier daily quotas reset).
    Other use of the same key is invisible to us: an estimate, never a guarantee."""
    pt = ZoneInfo("America/Los_Angeles")
    midnight = datetime.combine(datetime.now(pt).date(), time(0), tzinfo=pt)
    return int(
        session.scalar(
            select(func.count()).where(
                UsageRecord.model == model + FREE_TAG, UsageRecord.created_at >= midnight.astimezone(UTC)
            )
        )
        or 0
    )


def quota_check(p: Preflight, session: Session, model: str, teams: int = 1) -> None:
    limit = FREE_DAILY.get(model)
    if limit is None:
        p.problems.append(f"unknown free-tier quota for {model}: set GEMINI_FREE_MODEL to one of {sorted(FREE_DAILY)}")
        return
    left = limit - calls_today(session, model)
    need = CALLS_PER_TEAM * teams
    reset = (datetime.now(ZoneInfo("America/Los_Angeles")) + timedelta(days=1)).replace(hour=0, minute=0)
    p.check(
        left >= need,
        f"Gemini quota ({model}): ~{left}/{limit} requests left today (estimate), need ~{need}",
        f"Gemini quota ({model}): only ~{left}/{limit} left today, need ~{need}; resets "
        f"{reset.astimezone(ZoneInfo('Asia/Kolkata')):%d %b %H:%M} IST",
    )


def event_empty(p: Preflight, session: Session, event: Event, sheet_rows: int) -> None:
    teams = session.scalar(select(func.count()).where(Team.event_id == event.id)) or 0
    posts = (
        session.scalar(
            select(func.count()).where(Notification.event_id == event.id, Notification.telegram_message_id.is_not(None))
        )
        or 0
    )
    p.check(teams == 0, "no teams in the demo event", f"{teams} team(s) in the event: run reset-demo")
    p.check(posts == 0, "no posts of ours in the demo channel", f"{posts} post(s) in the channel: run reset-demo")
    p.check(sheet_rows == 0, "workbook Leaderboard is empty", f"Leaderboard has {sheet_rows} row(s): run reset-demo")
