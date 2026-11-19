"""purge_team: a team disappears from the DB, its Telegram posts are deleted, others are untouched."""

import pytest
from sqlalchemy.orm import Session, sessionmaker

from judgebench.admin import purge_team
from judgebench.db import models as m
from tests import fixtures_repos as repos
from tests.fake_llm import fake
from tests.test_m2_flow import deps, drive, submit

pytestmark = pytest.mark.db


def test_purge_removes_one_team_and_its_messages(session_factory: sessionmaker[Session], session: Session) -> None:
    dry, keep = submit(session, "DRY RUN 1"), submit(session, "Team Keep")
    drive(session_factory, deps(repos.ALPHA_RAG, fake()))
    for i, n in enumerate(session.query(m.Notification).all()):
        n.telegram_message_id = 100 + i
    session.commit()
    dry_msgs = {n.telegram_message_id for n in session.query(m.Notification).filter_by(submission_id=dry.id)}
    deleted: list[tuple[int, int]] = []

    r = purge_team(
        session, session.get_one(m.Event, dry.event_id), "dry run 1", lambda c, mid: deleted.append((c, mid)) or True
    )
    session.commit()

    assert (r.submissions, r.telegram_deleted, r.telegram_failed) == (1, len(dry_msgs), 0)
    assert {mid for _, mid in deleted} == dry_msgs and all(c == -100 for c, _ in deleted)
    assert session.query(m.Team).filter_by(name="DRY RUN 1").count() == 0
    assert session.query(m.Submission).filter_by(id=dry.id).count() == 0
    for table in m.Base.metadata.sorted_tables:  # nothing anywhere still points at the purged submission
        if "submission_id" in table.c and table.name != "usage_records":
            assert not session.execute(table.select().where(table.c.submission_id == dry.id)).first(), table.name
    assert session.query(m.Submission).filter_by(id=keep.id).count() == 1  # the other team is untouched
    assert session.query(m.Evaluation).filter_by(submission_id=keep.id).count() == 1
    [audit] = session.query(m.AuditEvent).filter_by(action="team_purged").all()
    assert audit.detail == {"submissions": 1, "telegram_deleted": len(dry_msgs), "telegram_failed": 0}
    with pytest.raises(ValueError):
        purge_team(session, session.get_one(m.Event, dry.event_id), "nobody", lambda c, mid: True)


def test_purge_keeps_usage_history(session_factory: sessionmaker[Session], session: Session) -> None:
    sub = submit(session, "DRY RUN 2")
    drive(session_factory, deps(repos.ALPHA_RAG, fake()))
    used = session.query(m.UsageRecord).filter_by(submission_id=sub.id).count()
    purge_team(session, session.get_one(m.Event, sub.event_id), "DRY RUN 2", lambda c, mid: True)
    session.commit()
    assert used > 0 and session.query(m.UsageRecord).filter_by(submission_id=sub.id).count() == used
