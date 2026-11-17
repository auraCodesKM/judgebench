"""DB -> judging workbook projection. Sheets is a view; the DB is the source of truth (§17).

M1 writes the Leaderboard tab only. It is fully machine-owned, so a full rewrite is safe;
judge input lives in the Judge Reviews tab, which this never touches.
"""

import hashlib
import json
import uuid
from collections.abc import Callable, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from judgebench.db.models import Event, EventIntegration, RepositoryProfileRow, Submission, Team

CATEGORY_LABELS = {"traditional_ml": "ML", "llm_rag": "LLM/RAG", "agentic": "Agentic",
                   "vision_multimodal": "Vision", "ai_infrastructure": "AI infra"}
Row = list[str | int]
Writer = Callable[[str, str, Sequence[Sequence[str | int]]], None]  # (spreadsheet_id, tab, rows)


def leaderboard_rows(session: Session, event_id: uuid.UUID) -> list[Row]:
    latest = (select(Submission.team_id, func.max(Submission.version).label("v"))
              .where(Submission.event_id == event_id).group_by(Submission.team_id).subquery())
    subs = session.execute(
        select(Team.name, Submission)
        .join(Submission, Submission.team_id == Team.id)
        .join(latest, (latest.c.team_id == Submission.team_id) & (latest.c.v == Submission.version))
        .where(Team.event_id == event_id)
        .order_by(Team.name_key)
    ).all()
    rows: list[Row] = []
    for team, sub in subs:
        cats = session.scalars(
            select(RepositoryProfileRow.categories).where(RepositoryProfileRow.submission_id == sub.id)
            .order_by(RepositoryProfileRow.created_at.desc()).limit(1)
        ).first() or {}
        rows.append([team, sub.version, (sub.commit_sha or "")[:7], sub.state.value,
                     ", ".join(CATEGORY_LABELS.get(c, c) for c in sorted(cats)),
                     sub.updated_at.strftime("%Y-%m-%d %H:%M")])
    return rows


def publish_leaderboard(session: Session, event: Event, write: Writer) -> bool:
    """Rewrite the Leaderboard tab if it changed. Returns True if a write happened. Commits."""
    integ = session.scalars(select(EventIntegration).where(
        EventIntegration.event_id == event.id, EventIntegration.provider == "google_sheets")).one()
    rows = leaderboard_rows(session, event.id)
    digest = hashlib.sha256(json.dumps(rows).encode()).hexdigest()
    if integ.cursor.get("leaderboard_sha256") == digest:
        return False  # saves Sheets write quota
    write(integ.external_ids["spreadsheet_id"], "Leaderboard", rows)
    integ.cursor = {**integ.cursor, "leaderboard_sha256": digest}
    session.commit()
    return True
