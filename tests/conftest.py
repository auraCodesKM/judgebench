"""DB fixtures. DB tests need Postgres: `docker compose up -d db`.

They fail (not skip) when the DB is down, so CI can never go green by skipping security tests.
"""

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from judgebench.db import models as m

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://judgebench:judgebench@localhost:5432/judgebench_test"
)


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    from alembic import command
    from alembic.config import Config

    admin = create_engine(TEST_DB_URL.rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT")
    db_name = TEST_DB_URL.rsplit("/", 1)[1]
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        c.execute(text(f'CREATE DATABASE "{db_name}"'))
    admin.dispose()

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_DB_URL)
    command.upgrade(cfg, "head")
    eng = create_engine(TEST_DB_URL)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    tables = ", ".join(t.name for t in m.Base.metadata.sorted_tables)
    with engine.begin() as c:
        c.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    return sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as s:
        yield s


def make_event(s: Session, slug: str) -> m.Event:
    tenant = s.query(m.Tenant).one_or_none() or m.Tenant(name="default")
    event = m.Event(
        tenant=tenant, slug=slug, name=f"Event {slug}", status="active", rubric_version="default-v1", config={}
    )
    s.add(event)
    s.flush()
    return event


def make_team(s: Session, event: m.Event, name: str) -> m.Team:
    team = m.Team(tenant_id=event.tenant_id, event_id=event.id, name=name, name_key=name.casefold())
    s.add(team)
    s.flush()
    return team


def make_submission(s: Session, team: m.Team, response_id: str = "r1", version: int = 1) -> m.Submission:
    sub = m.Submission(
        tenant_id=team.tenant_id,
        event_id=team.event_id,
        team_id=team.id,
        form_response_id=response_id,
        version=version,
        repo_url="https://github.com/acme/demo",
        form_answers={},
        submitted_at=m.utcnow(),
    )
    s.add(sub)
    s.flush()
    return sub


def make_judge(s: Session, event: m.Event, telegram_user_id: int) -> m.User:
    user = m.User(identity_provider="telegram", subject=str(telegram_user_id))
    s.add(user)
    s.flush()
    s.add(
        m.JudgeMembership(
            event_id=event.id,
            tenant_id=event.tenant_id,
            user_id=user.id,
            telegram_user_id=telegram_user_id,
            role="judge",
            status="active",
        )
    )
    s.flush()
    return user
