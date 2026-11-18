"""PostgreSQL schema (§17). The database is the source of truth; Sheets is a projection.

Isolation (§13.2): every event-owned row carries tenant_id + event_id, and composite foreign
keys make it impossible for a row to point at a parent in another event:
  (tenant_id, event_id)     -> events(tenant_id, id)
  (event_id, team_id)       -> teams(event_id, id)
  (event_id, submission_id) -> submissions(event_id, id)
  (event_id, evaluation_id) -> evaluations(event_id, id)
"""

import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column, relationship

from judgebench.states import State

Json = JSON().with_variant(JSONB(), "postgresql")
EMBED_DIM = 768


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        datetime: DateTime(timezone=True),
        dict[str, Any]: Json,
        list[Any]: Json,
    }


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, default=uuid.uuid4)


def _created() -> Mapped[datetime]:
    return mapped_column(server_default=func.now())


class EventOwned:
    """Mixin: tenant_id + event_id with a composite FK to events. Subclasses add _extra_args."""

    _extra_args: ClassVar[tuple[Any, ...]] = ()
    tenant_id: Mapped[uuid.UUID]
    event_id: Mapped[uuid.UUID] = mapped_column(index=True)

    @declared_attr.directive
    def __table_args__(cls) -> tuple[Any, ...]:  # noqa: N805
        return (
            ForeignKeyConstraint(["tenant_id", "event_id"], ["events.tenant_id", "events.id"], ondelete="CASCADE"),
            *cls._extra_args,
        )


def _child_of(parent: str, col: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(["event_id", col], [f"{parent}.event_id", f"{parent}.id"], ondelete="CASCADE")


# --- tenancy and identity --------------------------------------------------------------


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = _created()


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("identity_provider", "subject"),)
    id: Mapped[uuid.UUID] = _pk()
    identity_provider: Mapped[str] = mapped_column(String(30))  # google | telegram
    subject: Mapped[str] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320))
    created_at: Mapped[datetime] = _created()


class TenantMembership(Base):
    __tablename__ = "tenant_memberships"
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(20))  # operator
    status: Mapped[str] = mapped_column(String(20), default="active")


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("tenant_id", "id"), UniqueConstraint("tenant_id", "slug"))
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    slug: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|active|closed
    deadline: Mapped[datetime | None]
    rubric_version: Mapped[str] = mapped_column(String(50))
    config: Mapped[dict[str, Any]]  # validated EventConfig snapshot
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _created()
    tenant: Mapped[Tenant] = relationship()


class EventIntegration(EventOwned, Base):
    __tablename__ = "event_integrations"
    _extra_args = (UniqueConstraint("event_id", "provider"),)
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column()
    provider: Mapped[str] = mapped_column(String(30))  # google_forms | google_sheets
    external_ids: Mapped[dict[str, Any]] = mapped_column(default=dict)
    credential_ref: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    cursor: Mapped[dict[str, Any]] = mapped_column(default=dict)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class JudgeMembership(EventOwned, Base):
    __tablename__ = "judge_memberships"
    _extra_args = (UniqueConstraint("event_id", "user_id"), UniqueConstraint("event_id", "id"))
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    role: Mapped[str] = mapped_column(String(20), default="judge")
    status: Mapped[str] = mapped_column(String(20), default="invited")  # invited|active|removed
    created_at: Mapped[datetime] = _created()


# --- submissions -----------------------------------------------------------------------


class Team(EventOwned, Base):
    __tablename__ = "teams"
    _extra_args = (UniqueConstraint("event_id", "name_key"), UniqueConstraint("event_id", "id"))
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column()
    name: Mapped[str] = mapped_column(String(200))
    name_key: Mapped[str] = mapped_column(String(200))  # casefolded, for dedupe
    created_at: Mapped[datetime] = _created()


class Submission(EventOwned, Base):
    __tablename__ = "submissions"
    _extra_args = (
        _child_of("teams", "team_id"),
        UniqueConstraint("event_id", "form_response_id"),  # idempotent ingestion
        UniqueConstraint("team_id", "version"),
        UniqueConstraint("event_id", "id"),
    )
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column()
    team_id: Mapped[uuid.UUID] = mapped_column()
    form_response_id: Mapped[str] = mapped_column(String(200))
    version: Mapped[int]
    submitted_at: Mapped[datetime]
    repo_url: Mapped[str] = mapped_column(Text)
    demo_url: Mapped[str | None] = mapped_column(Text)
    deck_url: Mapped[str | None] = mapped_column(Text)
    deployed_url: Mapped[str | None] = mapped_column(Text)
    claimed_categories: Mapped[list[Any]] = mapped_column(default=list)  # participant hint only
    form_answers: Mapped[dict[str, Any]]
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    # --- workflow state (§16.3) ---
    state: Mapped[State] = mapped_column(
        SAEnum(State, native_enum=False, length=30), default=State.RECEIVED, index=True
    )
    last_successful_state: Mapped[State | None] = mapped_column(SAEnum(State, native_enum=False, length=30))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None]
    error_category: Mapped[str | None] = mapped_column(String(50))
    error_detail: Mapped[str | None] = mapped_column(Text)
    retryable: Mapped[bool | None]
    source_hashes: Mapped[dict[str, Any]] = mapped_column(default=dict)
    evaluator_version: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class SubmissionArtefact(EventOwned, Base):
    __tablename__ = "submission_artefacts"
    _extra_args = (_child_of("submissions", "submission_id"),)
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column()
    submission_id: Mapped[uuid.UUID] = mapped_column(index=True)
    type: Mapped[str] = mapped_column(String(30))  # repo_tarball | deck | demo
    storage_ref: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = _created()


class RepositoryProfileRow(EventOwned, Base):
    __tablename__ = "repository_profiles"
    _extra_args = (
        _child_of("submissions", "submission_id"),
        UniqueConstraint("submission_id", "commit_sha", "profiler_version"),
    )
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column()
    submission_id: Mapped[uuid.UUID] = mapped_column()
    commit_sha: Mapped[str] = mapped_column(String(64))
    profiler_version: Mapped[str] = mapped_column(String(20))
    categories: Mapped[dict[str, Any]]
    technologies: Mapped[list[Any]]
    profile: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = _created()


# --- evidence and evaluation (written from M2) ----------------------------------------


class Evidence(EventOwned, Base):
    __tablename__ = "evidence"
    _extra_args = (_child_of("submissions", "submission_id"),)
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column()
    submission_id: Mapped[uuid.UUID] = mapped_column(index=True)
    key: Mapped[str] = mapped_column(String(80))  # stable finding id, e.g. "rag.retrieval_on_request_path"
    stage_attempt: Mapped[str] = mapped_column(String(40))  # which evidence run produced it (append-only)
    criterion: Mapped[str | None] = mapped_column(String(60))
    claim: Mapped[str | None] = mapped_column(Text)
    observation: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(30))
    source_locator: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20))  # OBSERVED|VERIFIED|INFERRED|MISSING|CONTRADICTED
    confidence: Mapped[float] = mapped_column(Float)
    verification_method: Mapped[str] = mapped_column(String(40))
    limitations: Mapped[list[Any]] = mapped_column(default=list)
    provenance: Mapped[dict[str, Any]] = mapped_column(default=dict)
    source_commit_sha: Mapped[str | None] = mapped_column(String(64))
    evaluator_version: Mapped[str] = mapped_column(String(50))
    created_at: Mapped[datetime] = _created()


class Claim(EventOwned, Base):
    __tablename__ = "claims"
    _extra_args = (_child_of("submissions", "submission_id"),)
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column()
    submission_id: Mapped[uuid.UUID] = mapped_column(index=True)
    text: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(30))
    source_locator: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(
        String(30), default="feature"
    )  # accuracy|performance|scale|testing|architecture|feature
    verification_status: Mapped[str] = mapped_column(String(30), default="NOT_ATTEMPTED")
    evidence_ids: Mapped[list[Any]] = mapped_column(default=list)
    rationale: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class Evaluation(EventOwned, Base):
    __tablename__ = "evaluations"
    _extra_args = (_child_of("submissions", "submission_id"), UniqueConstraint("event_id", "id"))
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column()
    submission_id: Mapped[uuid.UUID] = mapped_column(index=True)
    rubric_version: Mapped[str] = mapped_column(String(50))
    evaluator_version: Mapped[str] = mapped_column(String(50))
    prompt_version: Mapped[str | None] = mapped_column(String(50))
    model: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20))
    total_score: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[str | None] = mapped_column(String(10))
    suggested_questions: Mapped[list[Any]] = mapped_column(default=list)
    flags: Mapped[list[Any]] = mapped_column(default=list)
    created_at: Mapped[datetime] = _created()


class CriterionScore(EventOwned, Base):
    __tablename__ = "criterion_scores"
    _extra_args = (_child_of("evaluations", "evaluation_id"), UniqueConstraint("evaluation_id", "criterion_id"))
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column()
    evaluation_id: Mapped[uuid.UUID] = mapped_column()
    criterion_id: Mapped[str] = mapped_column(String(60))
    score: Mapped[float] = mapped_column(Float)
    max_score: Mapped[float] = mapped_column(Float)
    rationale: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    evidence_ids: Mapped[list[Any]] = mapped_column(default=list)
    missing_evidence: Mapped[list[Any]] = mapped_column(default=list)
    suggested_question: Mapped[str | None] = mapped_column(Text)
    pre_critic_score: Mapped[float | None] = mapped_column(Float)  # the evaluator's original recommendation
    critic_note: Mapped[str | None] = mapped_column(Text)
    flagged: Mapped[bool] = mapped_column(default=False)


class AgentRun(EventOwned, Base):
    __tablename__ = "agent_runs"
    _extra_args = (_child_of("submissions", "submission_id"), _child_of("evaluations", "evaluation_id"))
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column()
    submission_id: Mapped[uuid.UUID] = mapped_column(index=True)
    evaluation_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    stage: Mapped[str] = mapped_column(String(30))
    agent_name: Mapped[str] = mapped_column(String(60))
    llm_calls: Mapped[int] = mapped_column(default=0)
    tool_calls: Mapped[int] = mapped_column(default=0)
    trace: Mapped[list[Any]] = mapped_column(default=list)  # tool name + args + short observation
    state: Mapped[str] = mapped_column(String(20))
    model: Mapped[str | None] = mapped_column(String(100))
    prompt_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int | None]
    created_at: Mapped[datetime] = _created()


class BehaviourCheck(EventOwned, Base):
    __tablename__ = "behaviour_checks"
    _extra_args = (_child_of("submissions", "submission_id"),)
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column()
    submission_id: Mapped[uuid.UUID] = mapped_column(index=True)
    type: Mapped[str] = mapped_column(String(20))  # ci | live_check | demo
    status: Mapped[str] = mapped_column(String(30))
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = _created()


class JudgeReview(EventOwned, Base):
    __tablename__ = "judge_reviews"
    _extra_args = (_child_of("evaluations", "evaluation_id"), _child_of("judge_memberships", "judge_membership_id"))
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column()
    evaluation_id: Mapped[uuid.UUID] = mapped_column(index=True)
    judge_membership_id: Mapped[uuid.UUID | None] = mapped_column()  # None when entered in the Sheet
    judge_label: Mapped[str | None] = mapped_column(String(200))  # free-text "Judge" column from the Sheet
    source: Mapped[str] = mapped_column(String(20), default="sheet")
    criterion_id: Mapped[str | None] = mapped_column(String(60))
    human_scores: Mapped[dict[str, Any]] = mapped_column(default=dict)
    comments: Mapped[str | None] = mapped_column(Text)
    override_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class JudgeQuery(EventOwned, Base):
    __tablename__ = "judge_queries"
    _extra_args = (_child_of("judge_memberships", "judge_membership_id"),)
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column()
    judge_membership_id: Mapped[uuid.UUID] = mapped_column()
    query: Mapped[str] = mapped_column(Text)
    evidence_refs: Mapped[list[Any]] = mapped_column(default=list)
    response_meta: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = _created()


class UsageRecord(EventOwned, Base):
    __tablename__ = "usage_records"
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column()
    submission_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    model: Mapped[str] = mapped_column(String(100))
    prompt_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int | None]
    stage: Mapped[str | None] = mapped_column(String(30))
    agent: Mapped[str | None] = mapped_column(String(60))
    tokens_per_sec: Mapped[float | None] = mapped_column(Float)  # output tokens / call latency
    created_at: Mapped[datetime] = _created()


class Chunk(EventOwned, Base):
    """Retrieval unit for hybrid search (§11.3): repo text windows, README sections, evidence.
    Every query filters on event_id (and usually submission_id) before ranking (§11.4)."""

    __tablename__ = "chunks"
    _extra_args = (
        _child_of("submissions", "submission_id"),
        UniqueConstraint("submission_id", "locator", "content_sha256"),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
        Index(
            "ix_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column()
    submission_id: Mapped[uuid.UUID] = mapped_column(index=True)
    source_type: Mapped[str] = mapped_column(String(20))  # code | documentation | evidence
    locator: Mapped[str] = mapped_column(Text)  # "app/rag.py#L1-L40", "README.md#3", "evidence:<uuid>"
    content: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64))
    tsv: Mapped[Any] = mapped_column(TSVECTOR, Computed("to_tsvector('simple', content)", persisted=True))
    embedding: Mapped[Any] = mapped_column(Vector(EMBED_DIM))
    embedding_model: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = _created()


# --- audit and outbox ------------------------------------------------------------------


class AuditEvent(Base):
    """Append-only. Not EventOwned: access denials may reference an event the actor can't see."""

    __tablename__ = "audit_events"
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id"))
    event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    submission_id: Mapped[uuid.UUID | None]
    actor: Mapped[str] = mapped_column(String(100))  # system:worker | telegram:<id> | operator
    action: Mapped[str] = mapped_column(String(60), index=True)
    target: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = _created()


class Notification(EventOwned, Base):
    """Transactional outbox for Telegram: written in the same transaction as the state change,
    so the bot only ever announces what the database recorded (§6.4)."""

    __tablename__ = "notifications"
    _extra_args = (_child_of("submissions", "submission_id"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column()
    submission_id: Mapped[uuid.UUID | None] = mapped_column()
    stage: Mapped[str] = mapped_column(String(40))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    sent_at: Mapped[datetime | None] = mapped_column(index=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
