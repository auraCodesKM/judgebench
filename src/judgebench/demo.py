"""Demo recording helpers: `reset-demo` (back to an empty, formatted event for a re-take) and
`demo-preflight` (everything a live take needs, checked before the camera rolls).

Both refuse any event whose slug does not contain "demo", so the real event can never be wiped.
"""

import time as clock
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any
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
    for mid in set(
        session.scalars(
            select(Notification.telegram_message_id).where(
                Notification.event_id == event.id, Notification.telegram_message_id.is_not(None)
            )
        )
    ):
        if mid and chat:
            ok = delete_message(chat, mid)
            report.telegram_deleted += ok
            report.telegram_failed += not ok
    session.execute(delete(Notification).where(Notification.event_id == event.id))
    session.execute(delete(AuditEvent).where(AuditEvent.event_id == event.id))
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    for integ in session.scalars(select(EventIntegration).where(EventIntegration.event_id == event.id)):
        if integ.provider == "google_forms":
            integ.cursor = {"since": now, "reset_at": now}  # earlier responses stay in the Form, unread
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


def quota_check(p: Preflight, session: Session, model: str, teams: int = 1, extra: int = 0) -> None:
    limit = FREE_DAILY.get(model)
    if limit is None:
        p.problems.append(f"unknown free-tier quota for {model}: set GEMINI_FREE_MODEL to one of {sorted(FREE_DAILY)}")
        return
    left = int((limit - calls_today(session, model)) * 0.9)  # our count misses retried requests: keep 10% back
    need = CALLS_PER_TEAM * teams + extra
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


def form_post(responder_uri: str, questions: dict[str, str], answers: dict[str, Any], code: str) -> int:
    """Submit one team through the Form's public endpoint, exactly as a browser does (same responses API,
    same ingestion). Returns the HTTP status."""
    from urllib.parse import urlencode

    import httpx

    from judgebench.google.workspace import CONSENT

    entry = {key: f"entry.{int(qid, 16)}" for qid, key in questions.items()}
    data = [
        (entry[k], str(x))
        for k, v in {**answers, "team_code": code, "consent": [CONSENT]}.items()
        if k in entry
        for x in (v if isinstance(v, list) else [v])
    ]
    r = httpx.post(
        responder_uri.replace("/viewform", "/formResponse"),
        content=urlencode(data),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
        follow_redirects=True,
    )
    return r.status_code


def demo_submit(
    session_factory: Any,
    event: Event,
    teams: list[dict[str, Any]],
    codes: list[str],
    after: str,
    gap: float,
    post: Callable[[str, dict[str, str], dict[str, Any], str], int] = form_post,
    sleep: Callable[[float], None] = clock.sleep,
    timeout: float = 1800,
    say: Callable[[str], None] = print,
    only: list[str] | None = None,
    rest: bool = False,
    responded: Callable[[], set[str]] = set,
) -> list[str]:
    """Wait until `after`'s real Form submission has been ingested, then submit every other team, one per
    `gap` seconds, through the public Form. Returns the teams submitted."""
    from judgebench.db.models import Submission

    require_demo(event)
    if len(codes) < len(teams):
        raise SystemExit(f"{event.slug}: {len(codes)} team codes for {len(teams)} teams (run team-codes)")
    with session_factory() as s:
        forms = s.scalars(
            select(EventIntegration).where(
                EventIntegration.event_id == event.id, EventIntegration.provider == "google_forms"
            )
        ).one()
        uri, questions = forms.external_ids["responder_uri"], forms.external_ids["questions"]
    started = datetime.now(UTC)
    if rest:  # off camera: everyone not in the event yet, no waiting
        with session_factory() as s:
            present = {n.lower() for n in s.scalars(select(Team.name).where(Team.event_id == event.id))}
        present |= {n.lower() for n in responded()}  # in the Form already, maybe not ingested yet
        only = [
            t["team_name"]
            for t in teams
            if t["team_name"].lower() not in present and t["team_name"].lower() != after.lower()
        ]
        say(f"submitting the remaining {len(only)} team(s): {', '.join(only) or 'none'}")
    waited = 0.0
    if not rest:
        say(f"waiting for {after}'s Form submission to arrive (start the run loop first) ...")
    while not rest:
        with session_factory() as s:
            arrived = s.scalar(
                select(Submission.id)
                .join(Team, Team.id == Submission.team_id)
                .where(
                    Submission.event_id == event.id,
                    func.lower(Team.name) == after.lower(),
                    Submission.created_at >= started,
                )
                .limit(1)
            )
        if arrived:
            break
        if waited >= timeout:
            raise SystemExit(f"{after} did not arrive within {timeout:.0f} s")
        sleep(3)
        waited += 3
    say(f"{after} arrived. Submitting the other teams, one every {gap:g} s.")
    done = []
    coded = list(zip(teams, codes, strict=False))
    if only is not None:  # in the order given
        coded = sorted(
            (tc for tc in coded if tc[0]["team_name"] in only), key=lambda tc: only.index(tc[0]["team_name"])
        )
    for t, code in coded:
        if t["team_name"].lower() == after.lower():
            continue
        sleep(gap)
        status = post(uri, questions, t, code)
        say(f"  {datetime.now():%H:%M:%S}  {t['team_name']}: Form submitted (HTTP {status})")
        done.append(t["team_name"])
    return done


def live_rate_check(p: Preflight, model: str, api_key: str) -> None:
    """One tiny real call (1 request): the model answers and is not rate-limited right now (per minute or
    per day). Also reports the limiter's rate: one under the free tier's RPM."""
    import re

    from google import genai
    from google.genai import types

    from judgebench.agents.modes import FREE_LIMITS
    from judgebench.config import settings

    rpm = settings().gemini_free_rpm or FREE_LIMITS.get(model, (10, 0))[0]
    try:
        genai.Client(api_key=api_key, vertexai=False).models.generate_content(
            model=model,
            contents="ok",
            config=types.GenerateContentConfig(
                max_output_tokens=5, automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)
            ),
        )
        p.ok.append(f"{model}: answering now; free tier {rpm} requests/min, limiter set to {rpm - 1}/min")
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        t = str(exc)
        q = re.search(r"'quotaId': '([^']+)'", t)
        d = re.search(r"'retryDelay': '([^']+)'", t)
        what = q.group(1) if q else t[:80]
        p.problems.append(f"{model}: {what}" + (f"; retry in {d.group(1)}" if d else ""))
