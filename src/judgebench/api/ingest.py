"""Form response ingestion (§6.2): polling with a cursor, idempotent on the Forms response ID.

Delivery is at-least-once (overlapping polls, `>=` cursor, retries), so correctness rests on
the UNIQUE(event_id, form_response_id) constraint plus INSERT ... ON CONFLICT DO NOTHING, not on
the cursor. Each new submission starts in RECEIVED with its audit row and Telegram outbox row
written in the same transaction.
"""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from judgebench import tracing
from judgebench.db.models import AuditEvent, Event, EventIntegration, Notification, Submission, Team
from judgebench.states import State
from judgebench.worker.github import parse_repo_url
from judgebench.workflow import record_failure

MAX_ANSWER = 10_000


@dataclass(frozen=True)
class ParsedResponse:
    response_id: str
    submitted_at: datetime
    answers: dict[str, Any]  # key -> str | list[str]


def parse_response(raw: dict[str, Any], questions: dict[str, str]) -> ParsedResponse:
    answers: dict[str, Any] = {}
    for qid, ans in (raw.get("answers") or {}).items():
        key = questions.get(qid)
        if key is None:
            continue  # question added by hand in the Forms UI: ignored, not trusted
        values = [str(v.get("value", ""))[:MAX_ANSWER] for v in ans.get("textAnswers", {}).get("answers", [])]
        answers[key] = values if key in {"categories", "consent"} else (values[0].strip() if values else "")
    ts = raw.get("lastSubmittedTime") or raw["createTime"]
    return ParsedResponse(str(raw["responseId"]), datetime.fromisoformat(ts.replace("Z", "+00:00")), answers)


def _team(session: Session, event: Event, name: str) -> Team:
    key = " ".join(name.casefold().split())
    session.execute(
        insert(Team)
        .values(id=uuid.uuid4(), tenant_id=event.tenant_id, event_id=event.id, name=name, name_key=key)
        .on_conflict_do_nothing(index_elements=["event_id", "name_key"])
    )
    return session.scalars(select(Team).where(Team.event_id == event.id, Team.name_key == key)).one()


def _code_hash(event: Event, code: str) -> str:
    return hashlib.sha256(f"{event.id}:{code}".encode()).hexdigest()


def _code_problem(session: Session, event: Event, name: str, code: str) -> str | None:
    """None if this (team name, code) may submit. Codes off (no team_codes in the event file): always None."""
    codes = event.config.get("team_codes") or []
    if not codes:
        return None
    if code not in codes:
        return "missing or unknown team code"
    h = _code_hash(event, code)
    key = " ".join(name.casefold().split())
    owner = session.scalars(select(Team).where(Team.event_id == event.id, Team.code_hash == h)).first()
    if owner is not None and owner.name_key != key:
        return "this team code belongs to another team"
    named = session.scalars(select(Team).where(Team.event_id == event.id, Team.name_key == key)).first()
    if named is not None and named.code_hash is not None and named.code_hash != h:
        return "team code does not match this team"
    return None


def _rejected_before(session: Session, event: Event, response_id: str) -> bool:
    return (
        session.scalar(
            select(AuditEvent.id).where(
                AuditEvent.event_id == event.id,
                AuditEvent.action == "submission_rejected",
                AuditEvent.target == response_id,
            )
        )
        is not None
    )


def _reject(session: Session, event: Event, response_id: str, name: str, reason: str) -> None:
    """Not evaluated. Recorded once (audit, keyed by response ID) and announced in Telegram."""
    session.add(
        AuditEvent(
            tenant_id=event.tenant_id,
            event_id=event.id,
            actor="system:ingest",
            action="submission_rejected",
            target=response_id,
            detail={"team": name, "reason": reason},
        )
    )
    session.add(
        Notification(
            tenant_id=event.tenant_id,
            event_id=event.id,
            stage="REJECTED",
            text=f'Rejected a submission for "{name}": {reason}. Not evaluated.',
        )
    )
    session.flush()


def ingest_responses(
    session: Session, event: Event, raw_responses: list[dict[str, Any]], questions: dict[str, str]
) -> list[uuid.UUID]:
    """Insert unseen responses as RECEIVED submissions. Returns new submission IDs. Caller commits."""
    created: list[uuid.UUID] = []
    for raw in sorted(raw_responses, key=lambda r: r.get("lastSubmittedTime") or r.get("createTime", "")):
        parsed = parse_response(raw, questions)
        exists = session.scalar(
            select(Submission.id).where(
                Submission.event_id == event.id, Submission.form_response_id == parsed.response_id
            )
        )
        if exists or _rejected_before(session, event, parsed.response_id):
            continue
        a = parsed.answers
        code = str(a.pop("team_code", "") or "").strip().upper()  # never stored; only its hash on the team
        name = (a.get("team_name") or "Unnamed team")[:200]
        if reason := _code_problem(session, event, name, code):
            _reject(session, event, parsed.response_id, name, reason)
            continue
        team = _team(session, event, name)
        if event.config.get("team_codes") and team.code_hash is None:
            team.code_hash = _code_hash(event, code)  # first valid submission binds the code to this team
        version = (session.scalar(select(func.max(Submission.version)).where(Submission.team_id == team.id)) or 0) + 1
        sub_id = uuid.uuid4()
        with tracing.span(
            "submission.received",
            submission_id=sub_id,
            event_id=event.id,
            tenant_id=event.tenant_id,
            team_version=version,
        ):
            trace_ctx = tracing.carrier()
        inserted = session.execute(
            insert(Submission)
            .values(
                id=sub_id,
                tenant_id=event.tenant_id,
                event_id=event.id,
                team_id=team.id,
                form_response_id=parsed.response_id,
                version=version,
                submitted_at=parsed.submitted_at,
                repo_url=a.get("repo_url", ""),
                demo_url=a.get("demo_url") or None,
                deck_url=a.get("deck_url") or None,
                deployed_url=a.get("deployed_url") or None,
                claimed_categories=a.get("categories", []),
                form_answers=a,
                state=State.RECEIVED,
                source_hashes={"trace": trace_ctx},
            )
            .on_conflict_do_nothing(index_elements=["event_id", "form_response_id"])
            .returning(Submission.id)
        ).scalar_one_or_none()
        if inserted is None:
            continue  # a concurrent poll won the race
        late = event.deadline is not None and parsed.submitted_at > event.deadline
        scope = {"tenant_id": event.tenant_id, "event_id": event.id, "submission_id": sub_id}
        session.add(
            AuditEvent(
                **scope,
                actor="system:ingest",
                action=f"state:{State.RECEIVED}",
                target=parsed.response_id,
                detail={"version": version, "late": late},
            )
        )
        session.add(
            Notification(
                **scope,
                stage=State.RECEIVED.value,
                text=f"Submission received (v{version}){' after the deadline' if late else ''}",
            )
        )
        session.flush()
        if parse_repo_url(a.get("repo_url", "")) is None:
            record_failure(
                session,
                sub_id,
                "invalid_repo_url",
                retryable=False,
                detail="expected https://github.com/<owner>/<repo>",
            )
        created.append(sub_id)
    return created


def forms_integration(session: Session, event: Event) -> EventIntegration:
    return session.scalars(
        select(EventIntegration).where(
            EventIntegration.event_id == event.id, EventIntegration.provider == "google_forms"
        )
    ).one()


def sync_event(session: Session, event: Event, fetch: Any) -> list[uuid.UUID]:
    """One poll. `fetch(form_id, since) -> raw responses` (workspace.list_responses bound to a session)."""
    integ = forms_integration(session, event)
    since = integ.cursor.get("since")
    with tracing.span("event.ingestion", event_id=event.id, tenant_id=event.tenant_id):
        raw = fetch(integ.external_ids["form_id"], since)
    created = ingest_responses(session, event, raw, integ.external_ids["questions"])
    stamps = [r.get("lastSubmittedTime") or r["createTime"] for r in raw]
    if stamps:
        integ.cursor = {**integ.cursor, "since": max([since or "", *stamps])}  # keep other keys (reset_at)
    session.commit()
    return created


def reevaluate(session: Session, event: Event, team_name: str) -> Submission:
    """Organiser re-run: the team's latest submission again as the next version, same answers and same
    submission time (so the same pinned commit). History is kept; nothing is overwritten. Caller commits."""
    latest = session.scalars(
        select(Submission)
        .join(Team, Team.id == Submission.team_id)
        .where(Submission.event_id == event.id, func.lower(Team.name) == team_name.strip().lower())
        .order_by(Submission.version.desc())
        .limit(1)
    ).first()
    if latest is None:
        raise ValueError(f"no submission for team {team_name!r} in {event.slug}")
    version = latest.version + 1
    sub = Submission(
        tenant_id=event.tenant_id,
        event_id=event.id,
        team_id=latest.team_id,
        form_response_id=f"{latest.form_response_id.split('#')[0]}#rerun-v{version}",
        version=version,
        submitted_at=latest.submitted_at,
        repo_url=latest.repo_url,
        demo_url=latest.demo_url,
        deck_url=latest.deck_url,
        deployed_url=latest.deployed_url,
        claimed_categories=latest.claimed_categories,
        form_answers=dict(latest.form_answers),
        state=State.RECEIVED,
        source_hashes={},
    )
    session.add(sub)
    session.flush()
    scope = {"tenant_id": event.tenant_id, "event_id": event.id, "submission_id": sub.id}
    session.add(
        AuditEvent(
            **scope,
            actor="operator",
            action=f"state:{State.RECEIVED}",
            target=f"rerun of v{latest.version}",
            detail={"version": version, "late": False, "rerun_of": str(latest.id)},
        )
    )
    session.add(
        Notification(**scope, stage=State.RECEIVED.value, text=f"Re-evaluation requested by the organiser (v{version})")
    )
    return sub
