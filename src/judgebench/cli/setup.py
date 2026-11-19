"""`setup` command (§6.1): event row, judges, Google Form + judging workbook. Re-runnable:
existing Google resources are reused, never duplicated; judges missing from the file lose access."""

from typing import Any

from google.auth.transport.requests import AuthorizedSession
from sqlalchemy import select
from sqlalchemy.orm import Session

from judgebench.db.models import (
    Event,
    EventIntegration,
    JudgeMembership,
    Notification,
    Tenant,
    User,
)
from judgebench.event_config import EventConfig
from judgebench.google import workspace


def upsert_event(session: Session, cfg: EventConfig) -> Event:
    tenant = session.scalars(select(Tenant).where(Tenant.name == "default")).first()
    if tenant is None:
        tenant = Tenant(name="default")
        session.add(tenant)
        session.flush()
    event = session.scalars(select(Event).where(Event.tenant_id == tenant.id, Event.slug == cfg.slug)).first()
    if event is None:
        event = Event(tenant_id=tenant.id, slug=cfg.slug)
        session.add(event)
    event.name, event.deadline, event.telegram_chat_id = cfg.name, cfg.deadline, cfg.telegram_chat_id
    event.rubric_version, event.status = cfg.rubric_version, "active"
    event.config = cfg.model_dump(mode="json")
    session.flush()
    return event


def sync_judges(session: Session, event: Event, cfg: EventConfig) -> None:
    wanted = {j.telegram_user_id for j in cfg.judges if j.telegram_user_id is not None}
    for tg in wanted:
        user = session.scalars(
            select(User).where(User.identity_provider == "telegram", User.subject == str(tg))
        ).first()
        if user is None:
            user = User(identity_provider="telegram", subject=str(tg))
            session.add(user)
            session.flush()
        member = session.scalars(
            select(JudgeMembership).where(JudgeMembership.event_id == event.id, JudgeMembership.user_id == user.id)
        ).first()
        if member is None:
            member = JudgeMembership(tenant_id=event.tenant_id, event_id=event.id, user_id=user.id, telegram_user_id=tg)
            session.add(member)
        member.status = "active"
    for member in session.scalars(select(JudgeMembership).where(JudgeMembership.event_id == event.id)):
        if member.telegram_user_id not in wanted:
            member.status = "removed"  # revoked immediately (§13.5)
    session.flush()


def _integration(session: Session, event: Event, provider: str) -> EventIntegration | None:
    return session.scalars(
        select(EventIntegration).where(EventIntegration.event_id == event.id, EventIntegration.provider == provider)
    ).first()


def setup_google(session: Session, event: Event, cfg: EventConfig, g: AuthorizedSession) -> dict[str, Any]:
    """Each resource is committed as soon as it exists, so a failure mid-setup never orphans it."""
    form = _integration(session, event, "google_forms")
    if form is None:
        ids = workspace.create_form(g, f"{cfg.name}: project submission")
        form = EventIntegration(
            tenant_id=event.tenant_id,
            event_id=event.id,
            provider="google_forms",
            external_ids=ids,
            credential_ref="operator-oauth",
            status="ready",
        )
        session.add(form)
        session.commit()
    else:  # existing Form: apply question/consent changes in place (responses and links stay valid)
        qmap = workspace.update_form(g, form.external_ids["form_id"], form.external_ids["questions"])
        if qmap != form.external_ids["questions"]:
            form.external_ids = {**form.external_ids, "questions": qmap}
            session.commit()
    book = _integration(session, event, "google_sheets")
    if book is None:
        ids = workspace.create_judging_workbook(g, f"{cfg.name}: judging")
        book = EventIntegration(
            tenant_id=event.tenant_id,
            event_id=event.id,
            provider="google_sheets",
            external_ids={**ids, "shared_with": []},
            credential_ref="operator-oauth",
            status="ready",
        )
        session.add(book)
        session.commit()
    shared = set(book.external_ids.get("shared_with", []))
    for judge in cfg.judges:
        if judge.email and judge.email not in shared:
            workspace.share_with(g, book.external_ids["spreadsheet_id"], judge.email)
            shared.add(judge.email)
            book.external_ids = {**book.external_ids, "shared_with": sorted(shared)}
            session.commit()
    return {"form": form.external_ids, "workbook": book.external_ids}


def announce_ready(session: Session, event: Event) -> None:
    """Once per event: re-running setup (judges, Form changes) must not re-announce."""
    already = session.scalar(
        select(Notification.id).where(Notification.event_id == event.id, Notification.stage == "EVENT_READY")
    )
    if already is not None:
        return
    session.add(
        Notification(
            tenant_id=event.tenant_id,
            event_id=event.id,
            submission_id=None,
            stage="EVENT_READY",
            text=f"{event.name} is open. I'll post here as teams submit.",
        )
    )
