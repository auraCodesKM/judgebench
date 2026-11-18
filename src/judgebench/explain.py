"""`judgebench explain <slug>`: what the evaluator concluded per team, and why, in one screen.

Claims and verdicts, behaviour checks (CI / live / demo), every score the critic changed, the
investigator's tool calls, and a scope check: whether any team's output names another team
(§14.2 prompt injection: "no other team's data in output").
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from judgebench.agents.modes import display
from judgebench.db.models import AgentRun, BehaviourCheck, Claim, CriterionScore, Evaluation, Submission
from judgebench.google.sheets_sync import _latest_subs


def explain(session: Session, event_id: uuid.UUID) -> str:
    subs = _latest_subs(session, event_id)
    names = [t for t, _ in subs]
    out: list[str] = []
    for team, sub in subs:
        ev = _latest_eval(session, sub)
        out.append(f"== {team}  [{sub.state.value}]  {sub.repo_url} @ {(sub.commit_sha or '-')[:7]}")
        if ev is None:
            out.append(f"   no evaluation{': ' + sub.error_category if sub.error_category else ''}\n")
            continue
        out.append(f"   total {ev.total_score:g}  confidence {ev.confidence}  evaluated by {display(ev.model)}")
        for c in session.scalars(select(Claim).where(Claim.submission_id == sub.id).order_by(Claim.created_at)):
            out.append(f"   claim  {c.verification_status:18} {c.text[:70]!r}  {(c.rationale or '')[:90]}")
        for b in session.scalars(select(BehaviourCheck).where(BehaviourCheck.submission_id == sub.id)):
            out.append(f"   check  {b.type:10} {b.status:15} {str(b.details.get('summary', ''))[:100]}")
        scores = list(session.scalars(select(CriterionScore).where(CriterionScore.evaluation_id == ev.id)))
        for s in scores:
            if s.pre_critic_score is not None and s.pre_critic_score != s.score:
                out.append(
                    f"   critic {s.criterion_id:20} {s.pre_critic_score:g} -> {s.score:g}  {(s.critic_note or '')[:90]}"
                )
        for run in session.scalars(
            select(AgentRun).where(AgentRun.evaluation_id == ev.id, AgentRun.agent_name == "claim_investigator")
        ):
            tools = ", ".join(f"{t['tool']}({next(iter(t['args'].values()), '')!s:.30})" for t in run.trace)
            out.append(f"   tools  {run.tool_calls} call(s): {tools or 'none'}")
        text = " ".join(
            [s.rationale for s in scores]
            + [str(q) for q in ev.suggested_questions]
            + [c.rationale or "" for c in session.scalars(select(Claim).where(Claim.submission_id == sub.id))]
        ).lower()
        leaked = [n for n in names if n != team and n.lower() in text]
        full = [s.criterion_id for s in scores if s.max_score and s.score >= s.max_score]
        out.append(
            f"   scope  other teams named: {', '.join(leaked) or 'none'}; full-marks criteria: {', '.join(full) or 'none'}\n"
        )
    return "\n".join(out) or "no submissions"


def _latest_eval(session: Session, sub: Submission) -> Evaluation | None:
    return session.scalars(
        select(Evaluation)
        .where(Evaluation.submission_id == sub.id, Evaluation.status == "ready")
        .order_by(Evaluation.created_at.desc())
        .limit(1)
    ).first()
