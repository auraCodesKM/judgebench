"""Form response ingestion (§6.2): polling with a cursor, idempotent on the Forms response ID.

Delivery is at-least-once (overlapping polls, `>=` cursor, retries), so correctness rests on
the UNIQUE(event_id, form_response_id) constraint plus INSERT ... ON CONFLICT DO NOTHING, not on
the cursor. Each new submission starts in RECEIVED with its audit row and Telegram outbox row
written in the same transaction.
"""

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
        if exists:
            continue
        a = parsed.answers
        team = _team(session, event, (a.get("team_name") or "Unnamed team")[:200])
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
        integ.cursor = {"since": max([since or "", *stamps])}
    session.commit()
    return created
