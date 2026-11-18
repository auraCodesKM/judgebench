"""M2 stages after profiling (§10.2):

  PROFILED -> COLLECTING_EVIDENCE   adapters + wiring, CI, live check, claim extraction, demo check
  COLLECTING_EVIDENCE -> EVALUATING specialists (routed), ReAct claim investigator, rubric evaluator
  EVALUATING -> CRITIQUING          deterministic critic rules + LLM critic (may only lower)
  CRITIQUING -> READY_FOR_REVIEW    totals, confidence, Telegram summary and cost

Each stage claims with a lease, works, then writes all its rows + Telegram lines + the transition
in one transaction. A crash leaves the previous state; the next run redoes only that stage.
The repo is re-fetched at the pinned commit when a stage needs code (no shared disk between runs).
"""

import logging
import re
import tempfile
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from judgebench import tracing
from judgebench.adapters.base import ADAPTER_VERSION, Snapshot
from judgebench.adapters.inspectors import inspect_repository
from judgebench.adapters.profiler import RepositoryProfile
from judgebench.adapters.wiring import CodeIndex
from judgebench.agents import runtime, team
from judgebench.agents.modes import configured, display, is_free, is_mock, submission_repo
from judgebench.agents.runtime import BudgetExceeded, Meter, fence
from judgebench.agents.scoring import Score, apply_critique, apply_rules, overall_confidence, validate_rubric
from judgebench.agents.views import ClaimView, EvidenceView, claims_block, rubric_block
from judgebench.config import settings
from judgebench.db.models import (
    AgentRun,
    BehaviourCheck,
    Claim,
    CriterionScore,
    Evaluation,
    Event,
    Evidence,
    Notification,
    RepositoryProfileRow,
    Submission,
    UsageRecord,
    utcnow,
)
from judgebench.errors import StageError
from judgebench.event_config import EventConfig
from judgebench.evidence import EVALUATOR_VERSION, CheckStatus, EvidenceStatus, Finding, SourceType, sha256_file
from judgebench.retrieval import Embedder, HashingEmbedder, Piece, chunk_repository, default_embedder, index_pieces
from judgebench.states import State
from judgebench.worker.ci import CiReader, ci_findings
from judgebench.worker.extract import extract_tarball
from judgebench.worker.github import GitHub, RepoRef, parse_repo_url
from judgebench.worker.live_check import ProbeClient, live_check_findings, probe_client
from judgebench.worker.pipeline import _profile
from judgebench.workflow import claim_next, record_failure, sweep_exhausted, transition

log = logging.getLogger(__name__)
DEMO_SECONDS, DEMO_FPS = 300, 0.5
_YOUTUBE = re.compile(
    r"^https://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?v=|shorts/|live/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)


@dataclass
class Deps:
    github: GitHub
    probe: ProbeClient
    cheap: team.Model
    strong: team.Model
    scratch: Path | None = None
    ci_reader: Callable[[GitHub], CiReader] = CiReader
    embedder: Embedder = field(default_factory=HashingEmbedder)

    @classmethod
    def from_settings(cls) -> "Deps":
        cfg = settings()
        return cls(
            GitHub(cfg.github_token),
            probe_client(cfg.probe_url),
            configured(cfg.gemini_model),
            configured(cfg.gemini_model_strong),
            embedder=default_embedder(),
        )


@dataclass
class Out:
    """Rows and Telegram lines a stage will write in its final transaction."""

    lines: list[tuple[str, str]] = field(default_factory=list)  # (stage label, text)


# --- helpers ---------------------------------------------------------------------------


def youtube_url(url: str | None) -> str | None:
    m = _YOUTUBE.match((url or "").strip())
    return f"https://www.youtube.com/watch?v={m.group(1)}" if m else None


@contextmanager
def checkout(deps: Deps, sub: Submission) -> Iterator[Path]:
    ref = _ref(sub)
    assert sub.commit_sha
    with tempfile.TemporaryDirectory(dir=deps.scratch, prefix="sub-") as tmp:
        with tracing.span("repository.fetch", commit=sub.commit_sha[:7]):
            tarball = deps.github.download_tarball(ref, sub.commit_sha, Path(tmp) / "repo.tar.gz")
        with tracing.span("artefact.extract"), tarball.path.open("rb") as f:
            result = extract_tarball(f, Path(tmp) / "src")
            tracing.annotate(files=result.files_written)
        yield Path(tmp) / "src"


def _ref(sub: Submission) -> RepoRef:
    ref = parse_repo_url(sub.repo_url)
    if ref is None:
        raise StageError("invalid_repo_url", False)
    return ref


def _scope(sub: Submission) -> dict[str, Any]:
    return {"tenant_id": sub.tenant_id, "event_id": sub.event_id, "submission_id": sub.id}


def _notify(session: Session, sub: Submission, stage: str, text: str) -> None:
    session.add(Notification(**_scope(sub), stage=stage, text=text))


def _used_tokens(session: Session, sid: uuid.UUID) -> int:
    return int(
        session.scalar(
            select(func.coalesce(func.sum(UsageRecord.prompt_tokens + UsageRecord.output_tokens), 0)).where(
                UsageRecord.submission_id == sid
            )
        )
        or 0
    )


def _persist_usage(
    session: Session, sub: Submission, meter: Meter, stage: str, evaluation_id: uuid.UUID | None = None
) -> None:
    for call in meter.calls:
        session.add(
            UsageRecord(
                tenant_id=sub.tenant_id,
                event_id=sub.event_id,
                submission_id=sub.id,
                model=call.model,
                prompt_tokens=call.prompt_tokens,
                output_tokens=call.output_tokens,
                cost_usd=call.cost_usd,
                latency_ms=call.latency_ms,
                stage=stage,
                agent=call.agent,
                tokens_per_sec=call.tokens_per_sec,
            )
        )
    agents = list(dict.fromkeys([*(c.agent for c in meter.calls), *meter.tools]))  # first-call order
    for name in agents:
        calls = [c for c in meter.calls if c.agent == name]
        session.add(
            AgentRun(
                **_scope(sub),
                evaluation_id=evaluation_id,
                stage=stage,
                agent_name=name,
                state="done",
                model=calls[0].model if calls else None,
                llm_calls=len(calls),
                tool_calls=len(meter.tools.get(name, [])),
                trace=meter.tools.get(name, []),
                prompt_tokens=sum(c.prompt_tokens for c in calls),
                output_tokens=sum(c.output_tokens for c in calls),
                cost_usd=sum(c.cost_usd for c in calls),
                latency_ms=sum(c.latency_ms for c in calls),
            )
        )


def _persist_findings(session: Session, sub: Submission, findings: list[Finding], attempt: str) -> None:
    now = utcnow().isoformat()
    for f in findings:
        session.add(
            Evidence(
                **_scope(sub),
                key=f.key,
                stage_attempt=attempt,
                criterion=f.criterion,
                claim=f.claim,
                observation=f.observation[:4000],
                source_type=f.source_type.value,
                source_locator=f.source_locator[:500],
                status=f.status.value,
                confidence=f.confidence,
                verification_method=f.verification_method,
                limitations=f.limitations,
                source_commit_sha=sub.commit_sha,
                evaluator_version=EVALUATOR_VERSION,
                provenance={
                    "repo_url": sub.repo_url,
                    "commit_sha": sub.commit_sha,
                    "path": f.path,
                    "line_start": f.line_start,
                    "line_end": f.line_end,
                    "file_sha256": f.file_sha256,
                    "retrieved_at": now,
                    "behavioural": f.behavioural,
                    "adapter_version": ADAPTER_VERSION,
                },
            )
        )


def _evidence_views(session: Session, sub: Submission) -> list[EvidenceView]:
    attempt = sub.source_hashes.get("evidence_attempt")
    rows = session.scalars(
        select(Evidence)
        .where(Evidence.submission_id == sub.id, Evidence.stage_attempt == attempt)
        .order_by(Evidence.created_at, Evidence.key)
    ).all()
    return [
        EvidenceView(
            f"E{i}",
            r.id,
            r.key,
            r.source_type,
            r.status,
            r.source_locator,
            r.observation,
            bool(r.provenance.get("behavioural")),
            r.criterion,
        )
        for i, r in enumerate(rows, 1)
    ]


def _claim_views(session: Session, sub: Submission) -> list[ClaimView]:
    rows = session.scalars(
        select(Claim).where(Claim.submission_id == sub.id).order_by(Claim.created_at, Claim.text)
    ).all()
    return [ClaimView(f"C{i}", r.id, r.text, r.source, r.kind) for i, r in enumerate(rows, 1)]


def _rubric(session: Session, sub: Submission) -> EventConfig:
    event = session.get_one(Event, sub.event_id)
    return EventConfig.model_validate(event.config)


def _profile_of(session: Session, sub: Submission) -> RepositoryProfile:
    row = session.scalars(
        select(RepositoryProfileRow)
        .where(RepositoryProfileRow.submission_id == sub.id, RepositoryProfileRow.commit_sha == sub.commit_sha)
        .order_by(RepositoryProfileRow.created_at.desc())
    ).first()
    if row is None:
        raise StageError("profile_missing", False)
    return RepositoryProfile.model_validate(row.profile)


# --- COLLECTING_EVIDENCE ---------------------------------------------------------------


def _demo_findings(review: dict[str, Any] | None, claims: list[ClaimView]) -> tuple[list[Finding], CheckStatus, str]:
    locator = f"demo (first {DEMO_SECONDS // 60} min at {DEMO_FPS} fps)"
    if not review or not review.get("available"):
        f = Finding(
            key="demo.unavailable",
            observation="Demo video private, deleted or unreadable",
            source_type=SourceType.DEMO,
            source_locator="demo",
            status=EvidenceStatus.MISSING,
            confidence=0.9,
            verification_method="demo_review",
            criterion="presentation",
        )
        return [f], CheckStatus.NOT_VERIFIABLE, "video unavailable, marked MISSING"
    text = {c.id: c.text for c in claims}
    out, headline = [], ""
    for o in review.get("observations", []):
        if o.get("claim_id") not in text:
            continue
        ts = o.get("timestamp")
        status = (
            EvidenceStatus.CONTRADICTED
            if o.get("contradicted")
            else (EvidenceStatus.OBSERVED if o.get("shown") else EvidenceStatus.MISSING)
        )
        claim = text[o["claim_id"]]
        out.append(
            Finding(
                key="demo.claim",
                claim=claim,
                observation=o.get("note", "")[:300] or "(no note)",
                source_type=SourceType.DEMO,
                source_locator=f"demo#{ts}" if ts else "demo",
                status=status,
                confidence=float(o.get("confidence", 0.5)),
                verification_method="demo_review",
                criterion="presentation",
                limitations=[f"only {locator} reviewed"],
            )
        )
        line = (
            f'claim "{claim[:60]}" -> {"contradicted" if status == EvidenceStatus.CONTRADICTED else "shown"}'
            f"{f' at {ts}' if ts else ''}: {o.get('note', '')[:80]}"
        )
        if status == EvidenceStatus.CONTRADICTED or (not headline and status == EvidenceStatus.OBSERVED):
            headline = line if status == EvidenceStatus.CONTRADICTED or not headline else headline
    shown = sum(f.status == EvidenceStatus.OBSERVED for f in out)
    return (
        out,
        CheckStatus.PARTIALLY_VERIFIED if shown else CheckStatus.NOT_VERIFIABLE,
        headline or f"{shown} of {len(claims)} claims shown",
    )


def collect_evidence(session: Session, sid: uuid.UUID, deps: Deps) -> None:
    sub = session.get_one(Submission, sid)
    profile = _profile_of(session, sub)
    ref = _ref(sub)
    answers = sub.form_answers
    meter = Meter(already_used=_used_tokens(session, sid))
    with checkout(deps, sub) as root:
        snap = Snapshot(root, profile, CodeIndex(root))
        with tracing.span("evidence.collect"):
            findings = inspect_repository(snap)
            tracing.annotate(findings=len(findings))
        with tracing.span("behaviour.ci"):
            ci_f, ci_status, ci_details = ci_findings(
                deps.ci_reader(deps.github), ref, sub.commit_sha or "", root, snap.index.test_functions
            )
            tracing.annotate(status=ci_status.value)
        readme = (root / profile.readme).read_text(errors="replace") if profile.readme else ""
        pieces = chunk_repository(root)
        with tracing.span("behaviour.live_check"):  # inside the checkout: route paths are matched to code
            live_f, live_status, live_details = live_check_findings(
                deps.probe, sub.deployed_url, answers.get("sample_request"), root
            )
            tracing.annotate(status=live_status.value)

    form_text = "\n".join(
        f"{k}: {answers.get(k, '')}"
        for k in ("project_title", "problem_statement", "tech_claims", "dataset_eval", "special_requirements")
    )
    state = runtime.run(
        team.claim_analyst(deps.cheap, meter),
        {
            "readme_block": fence("README", readme or "(no README)", 15_000),
            "form_block": fence("form", form_text, 8_000),
        },
    )
    extracted = (state.get("claims") or {}).get("claims", [])
    session.query(Claim).filter(Claim.submission_id == sid).delete()
    for c in extracted:
        session.add(
            Claim(**_scope(sub), text=c["text"], source=c["source"], source_locator=c["locator"], kind=c["kind"])
        )
    session.flush()
    claims = _claim_views(session, sub)

    video = youtube_url(sub.demo_url)
    if video and claims:
        try:
            demo = runtime.run(
                team.demo_analyst(deps.cheap, meter, video, DEMO_SECONDS, DEMO_FPS),
                {"claims_block": claims_block(claims)},
            ).get("demo")
        except Exception as exc:  # the video itself is the problem: private/removed/unsupported
            from google.genai import errors as genai_errors

            if not (isinstance(exc, genai_errors.ClientError) and exc.code in (400, 403, 404)):
                raise
            demo = None
        demo_f, demo_status, demo_line = _demo_findings(demo, claims)
    else:
        demo_status = CheckStatus.NOT_VERIFIABLE
        demo_line = "no YouTube demo URL, marked MISSING" if not video else "no claims to check against the demo"
        demo_f = [
            Finding(
                key="demo.unavailable",
                observation=demo_line,
                source_type=SourceType.DEMO,
                source_locator="form:demo_url",
                status=EvidenceStatus.MISSING,
                confidence=0.9,
                verification_method="demo_review",
                criterion="presentation",
            )
        ]

    attempt = f"evidence-{uuid.uuid4().hex[:12]}"
    _persist_findings(session, sub, findings + ci_f + live_f + demo_f, attempt)
    for kind, status, details in [
        ("ci", ci_status, ci_details),
        ("live_check", live_status, live_details),
        ("demo", demo_status, {"summary": demo_line}),
    ]:
        session.add(BehaviourCheck(**_scope(sub), type=kind, status=status.value, details=details))
    sub.source_hashes = {**sub.source_hashes, "evidence_attempt": attempt}
    session.flush()
    evidence_pieces = [
        Piece("evidence", f"evidence:{e.id}", f"{e.key} [{e.status}] {e.source_locator}: {e.observation}")
        for e in session.scalars(select(Evidence).where(Evidence.stage_attempt == attempt))
    ]
    with tracing.span("retrieval.index", model=deps.embedder.model):  # for the Judge Copilot (§11.3)
        tracing.annotate(chunks=index_pieces(session, sub, pieces + evidence_pieces, deps.embedder))
    ci_suffix = ", marked NOT_VERIFIABLE, score not reduced" if ci_status == CheckStatus.NOT_VERIFIABLE else ""
    _notify(session, sub, "CI", f"CI: {ci_details['summary']}{ci_suffix}")
    _notify(session, sub, "LIVE_CHECK", f"Live check: {live_details['summary']}")
    _notify(session, sub, "DEMO", f"Demo check: {demo_line}")
    _persist_usage(session, sub, meter, "COLLECTING_EVIDENCE")
    transition(
        session,
        sid,
        State.COLLECTING_EVIDENCE,
        State.EVALUATING,
        message=None,
        detail={"evidence": len(findings) + len(ci_f) + len(live_f) + len(demo_f), "claims": len(claims)},
    )


# --- EVALUATING ------------------------------------------------------------------------


def evaluate(session: Session, sid: uuid.UUID, deps: Deps) -> None:
    sub = session.get_one(Submission, sid)
    cfg = _rubric(session, sub)
    profile = _profile_of(session, sub)
    evidence, claims = _evidence_views(session, sub), _claim_views(session, sub)
    meter = Meter(already_used=_used_tokens(session, sid))
    with checkout(deps, sub) as root:
        tools = team.investigation_tools(root, evidence)
        state = runtime.run(
            team.orchestrator(profile.recommended_adapters, deps.cheap, deps.strong, meter, tools),
            {
                "adapters": profile.recommended_adapters,
                "profile_block": fence(
                    "profile",
                    profile.model_dump_json(
                        include={
                            "languages",
                            "technologies",
                            "categories",
                            "entry_points",
                            "directories",
                            "missing",
                            "limitations",
                        }
                    ),
                ),
                "rubric_block": rubric_block(cfg.rubric),
                "claims_block": claims_block(claims),
                "_evidence": evidence,
                "_claims": claims,
                "_root": root,
            },
        )
        by_id = {e.id: e for e in evidence}
        for v in state.get("verdicts", []):
            row = session.get_one(Claim, next(c.row_id for c in claims if c.id == v["claim_id"]))
            row.verification_status, row.rationale = v["status"], v["rationale"]
            row.evidence_ids = [str(by_id[e].row_id) for e in v["evidence_ids"]]
            for ref in v["code_refs"]:  # code the investigator found with its tools becomes evidence
                path, _, lines = ref.partition("#L")
                a, _, b = lines.partition("-L")
                _persist_findings(
                    session,
                    sub,
                    [
                        Finding(
                            key="investigation.code_ref",
                            claim=row.text,
                            observation=f"Investigator cited {ref} for {v['claim_id']}",
                            source_type=SourceType.CODE,
                            source_locator=ref,
                            status=EvidenceStatus.OBSERVED,
                            confidence=0.6,
                            verification_method="claim_investigation",
                            path=path,
                            line_start=int(a),
                            line_end=int(b or a),
                            file_sha256=sha256_file(root / path),
                        )
                    ],
                    sub.source_hashes["evidence_attempt"],
                )

    scores, flags = validate_rubric(state.get("rubric_result"), cfg.rubric, by_id)
    raw_questions = (state.get("rubric_result") or {}).get("suggested_questions", [])
    evaluation = Evaluation(
        **_scope(sub),
        rubric_version=cfg.rubric_version,
        evaluator_version=EVALUATOR_VERSION,
        prompt_version=runtime.PROMPT_VERSION,
        model=runtime.model_name(deps.strong),
        status="draft",
        flags=flags,
        suggested_questions=[str(q)[:300] for q in raw_questions][:5],
    )
    session.add(evaluation)
    session.flush()
    for s in scores:
        session.add(
            CriterionScore(
                tenant_id=sub.tenant_id,
                event_id=sub.event_id,
                evaluation_id=evaluation.id,
                criterion_id=s.criterion_id,
                score=s.score,
                max_score=s.max,
                rationale=s.rationale,
                confidence=s.confidence,
                evidence_ids=[str(by_id[e].row_id) for e in s.evidence_ids],
                missing_evidence=s.missing,
                suggested_question=s.question,
                pre_critic_score=s.pre_critic,
                flagged=s.flagged,
            )
        )
    counts = Counter(v["status"] for v in state.get("verdicts", []))
    labels = [
        ("VERIFIED", "verified"),
        ("PARTIALLY_VERIFIED", "partially verified"),
        ("UNVERIFIED", "unverified"),
        ("CONTRADICTED", "contradicted"),
        ("MISSING_EVIDENCE", "missing evidence"),
        ("NOT_VERIFIABLE", "not verifiable"),
    ]
    summary = ", ".join(f"{counts[k]} {label}" for k, label in labels if counts[k])
    _notify(session, sub, "CLAIMS", f"Claims: {len(claims)} extracted" + (f", {summary}" if summary else ""))
    _persist_usage(session, sub, meter, "EVALUATING", evaluation.id)
    transition(
        session,
        sid,
        State.EVALUATING,
        State.CRITIQUING,
        message=None,
        keep_lease=False,
        detail={
            "agents": list(dict.fromkeys(c.agent for c in meter.calls)),
            "tools": meter.tools.get("claim_investigator", []),
        },
    )


# --- CRITIQUING ------------------------------------------------------------------------


def _fmt_tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _fmt_duration(start: datetime) -> str:
    secs = int((utcnow() - start).total_seconds())
    return f"{secs // 60} m {secs % 60} s" if secs >= 60 else f"{secs} s"


def critique(session: Session, sid: uuid.UUID, deps: Deps) -> None:
    sub = session.get_one(Submission, sid)
    cfg = _rubric(session, sub)
    evidence = _evidence_views(session, sub)
    by_id, by_row = {e.id: e for e in evidence}, {str(e.row_id): e.id for e in evidence}
    evaluation = session.scalars(
        select(Evaluation)
        .where(Evaluation.submission_id == sid, Evaluation.status == "draft")
        .order_by(Evaluation.created_at.desc())
    ).first()
    if evaluation is None:
        raise StageError("evaluation_missing", False)
    rows = {
        r.criterion_id: r
        for r in session.scalars(select(CriterionScore).where(CriterionScore.evaluation_id == evaluation.id))
    }
    names = {c.id: c.name for c in cfg.rubric}
    scores = [
        Score(
            r.criterion_id,
            names.get(r.criterion_id, r.criterion_id),
            r.max_score,
            r.score,
            r.rationale,
            [by_row[e] for e in r.evidence_ids if e in by_row],
            r.confidence,
            list(r.missing_evidence),
            r.suggested_question,
            r.pre_critic_score,
            flagged=r.flagged,
        )
        for r in rows.values()
    ]
    messages = apply_rules(scores, by_id)

    meter = Meter(already_used=_used_tokens(session, sid))
    scores_block = "\n".join(
        f"{s.criterion_id} {s.score:g}/{s.max:g} (evidence {', '.join(s.evidence_ids) or 'none'}): {s.rationale}"
        for s in scores
    )
    state = runtime.run(
        team.critic(deps.strong, meter),
        {"rubric_block": rubric_block(cfg.rubric), "_evidence": evidence, "scores_block": scores_block},
    )
    messages += apply_critique(scores, state.get("critique"))

    for s in scores:
        r = rows[s.criterion_id]
        r.score, r.flagged = s.score, s.flagged
        r.critic_note = "; ".join(s.notes) or None
    total = round(sum(s.score for s in scores), 1)
    confidence = overall_confidence(scores, list(evaluation.flags))
    questions = list(evaluation.suggested_questions) or [s.question for s in scores if s.question][:3]
    evaluation.total_score, evaluation.confidence, evaluation.status = total, confidence, "ready"
    evaluation.suggested_questions = questions
    evaluation.flags = [*evaluation.flags, *messages]
    _persist_usage(session, sub, meter, "CRITIQUING", evaluation.id)
    session.flush()

    for m in messages[:3]:
        _notify(session, sub, "CRITIC", f"Critic: {m}")
    if not messages:
        _notify(session, sub, "CRITIC", "Critic: all scores supported by cited evidence")
    max_total = sum(c.weight for c in cfg.rubric)
    flagged = sum(s.flagged for s in scores)
    mock, free = is_mock(evaluation.model), is_free(evaluation.model)
    _notify(
        session,
        sub,
        "READY",
        ("MOCK evaluation (offline, no model; scores are placeholders): " if mock else "")
        + (f"[{display(evaluation.model)}] " if free else "")
        + f"Evaluation ready: {total:g}/{max_total} (confidence {confidence}). "
        f"{len(questions)} suggested question{'s' if len(questions) != 1 else ''}"
        + (f", {flagged} score{'s' if flagged != 1 else ''} flagged for judges." if flagged else "."),
    )
    usage = session.execute(
        select(
            func.coalesce(func.sum(UsageRecord.prompt_tokens + UsageRecord.output_tokens), 0),
            func.coalesce(func.sum(UsageRecord.cost_usd), 0.0),
        ).where(UsageRecord.submission_id == sid)
    ).one()
    _notify(
        session,
        sub,
        "COST",
        f"Cost: {_cost(mock, free, int(usage[0]), float(usage[1]))}, end-to-end {_fmt_duration(sub.created_at)}",
    )
    transition(
        session,
        sid,
        State.CRITIQUING,
        State.READY_FOR_REVIEW,
        message=None,
        detail={"total": total, "confidence": confidence, "revisions": len(messages)},
    )


def _cost(mock: bool, free: bool, tokens: int, usd: float) -> str:
    if mock:
        return "$0.00, offline mock (no model calls)"
    return f"{_fmt_tokens(tokens)} tokens, " + ("$0.00 (Gemini API free tier)" if free else f"${usd:.2f}")


# --- runner ----------------------------------------------------------------------------


def _start_evidence(session: Session, sid: uuid.UUID, deps: Deps) -> None:
    transition(session, sid, State.PROFILED, State.COLLECTING_EVIDENCE, message=None, keep_lease=True)
    session.commit()
    collect_evidence(session, sid, deps)


STAGES: list[tuple[State, Callable[[Session, uuid.UUID, Deps], None]]] = [
    (State.RECEIVED, lambda s, sid, d: _profile(s, d.github, sid, d.scratch)),
    (State.PROFILED, _start_evidence),
    (State.COLLECTING_EVIDENCE, collect_evidence),
    (State.EVALUATING, evaluate),
    (State.CRITIQUING, critique),
]


STAGE_SPANS = {
    State.RECEIVED: "submission.profile",
    State.PROFILED: "submission.collect_evidence",
    State.COLLECTING_EVIDENCE: "submission.collect_evidence",
    State.EVALUATING: "submission.evaluate",
    State.CRITIQUING: "submission.critique",
}


def run_next(sessions: sessionmaker[Session], deps: Deps) -> uuid.UUID | None:
    """Advance at most one submission by one stage. Returns its ID, or None if nothing to do."""
    for state, work in STAGES:
        with sessions() as session:
            sweep_exhausted(session, state)
            session.commit()
            sid = claim_next(session, state)
            if sid is None:
                continue
            sub = session.get_one(Submission, sid)
            parent = sub.source_hashes.get("trace")
            try:
                with tracing.span(
                    STAGE_SPANS[state],
                    parent=parent,
                    submission_id=sid,
                    event_id=sub.event_id,
                    tenant_id=sub.tenant_id,
                    stage=state.value,
                    attempt=sub.attempts,
                ) as stage_span:
                    # profiling is deterministic; every later stage may call a model
                    with nullcontext() if state == State.RECEIVED else submission_repo(sub.repo_url):
                        work(session, sid, deps)
                    with tracing.span("evaluation.persist"):
                        session.commit()
                    stage_span.set_attribute("jb.status", "ok")
            except BudgetExceeded as exc:
                session.rollback()
                current = session.get_one(Submission, sid).state
                transition(
                    session,
                    sid,
                    current,
                    State.REQUIRES_HUMAN,
                    message=f"Token budget reached ({exc}); sent to judges for manual review",
                )
                session.commit()
            except StageError as exc:
                session.rollback()
                record_failure(session, sid, exc.category, retryable=exc.retryable, detail=str(exc))
                session.commit()
            except Exception as exc:
                log.exception("stage %s crashed for %s", state, sid)
                session.rollback()
                record_failure(session, sid, "internal_error", retryable=True, detail=type(exc).__name__)
                session.commit()
            return sid
    return None
