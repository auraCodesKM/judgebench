"""Telegram Judge Copilot (§6.5). Every message:
  1. resolve the Telegram user ID  2. require an active judge membership  3. derive permitted events
  4. retrieve only inside those events  5. answer from persisted evidence, saying when unsure
  6. log query + response metadata + evidence refs (judge_queries); denials go to audit_events.

Chat text never selects an event. A team outside the judge's events looks exactly like a team
that does not exist ("No team named X in your events"), and the attempt is logged as a denial.
"""

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from judgebench.agents import runtime, team
from judgebench.agents.modes import FreeTierRefused, submission_repo
from judgebench.agents.runtime import Meter, fence
from judgebench.db.models import (
    Claim,
    CriterionScore,
    Evaluation,
    Evidence,
    JudgeMembership,
    JudgeQuery,
    Submission,
    Team,
    UsageRecord,
)
from judgebench.db.scope import EventScope, log_denial, scope_for_telegram_user
from judgebench.retrieval import Embedder, hybrid_search

HELP = (
    "Commands:\n/teams: teams in your events\n/team <name>: score, criteria, flags\n"
    "/evidence <name>: evidence records\n/ask <name> <question>: question the code and evidence\n/help"
)
MAX_REPLY = 3500


@dataclass
class Copilot:
    embedder: Embedder
    model: team.Model
    run_agent: Callable[..., dict[str, object]] = runtime.run

    def handle(self, session: Session, telegram_user_id: int, text: str) -> str:
        scope = scope_for_telegram_user(session, telegram_user_id)
        command, _, rest = text.strip().partition(" ")
        command = command.split("@")[0].lower()
        if not scope.event_ids:
            log_denial(session, scope.actor, None, text[:200])
            session.commit()
            return "You are not an active judge for any event. Ask the organiser to add your Telegram ID."
        handlers: dict[str, Callable[[Session, EventScope, str], str]] = {
            "/teams": self._teams,
            "/team": self._team,
            "/evidence": self._evidence,
            "/ask": self._ask,
        }
        handler = handlers.get(command)
        if handler:
            reply = handler(session, scope, rest.strip())
        else:
            reply = HELP if command in ("/start", "/help") else "Unknown command.\n\n" + HELP
        self._log(session, scope, telegram_user_id, text, reply)
        session.commit()
        return reply[:MAX_REPLY]

    # --- commands ---

    def _teams(self, s: Session, scope: EventScope, rest: str) -> str:
        rows = []
        for t in s.scalars(select(Team).where(Team.event_id.in_(scope.event_ids)).order_by(Team.name_key)):
            sub, ev = _latest(s, t)
            score = f"{ev.total_score:g}" if ev and ev.total_score is not None else "-"
            rows.append(f"{t.name}: {sub.state.value if sub else 'no submission'}, score {score}")
        return "\n".join(rows) or "No teams yet."

    def _resolve(self, s: Session, scope: EventScope, words: str) -> tuple[Team | None, str]:
        """Longest leading run of words that names a team in scope; the rest is the question."""
        parts = words.split()
        for n in range(len(parts), 0, -1):
            if t := scope.find_team(s, " ".join(parts[:n])):
                return t, " ".join(parts[n:])
        return None, words

    def _not_found(self, s: Session, scope: EventScope, words: str) -> str:
        parts = words.split()
        keys = [" ".join(parts[:n]).casefold() for n in range(len(parts), 0, -1)]
        elsewhere = (
            s.scalar(
                select(Team.event_id).where(Team.name_key.in_(keys), Team.event_id.not_in(scope.event_ids)).limit(1)
            )
            if keys
            else None
        )
        if elsewhere:  # a real team the judge may not see: deny + log, but answer exactly like "not found"
            log_denial(s, scope.actor, elsewhere, f"team lookup: {words[:100]}")
        return f"No team named {(parts[0] if parts else '')[:60]!r} in your events."

    def _team(self, s: Session, scope: EventScope, rest: str) -> str:
        t, _ = self._resolve(s, scope, rest)
        if t is None:
            return self._not_found(s, scope, rest)
        sub, ev = _latest(s, t)
        if sub is None:
            return f"{t.name}: no submission yet."
        lines = [f"{t.name} v{sub.version} ({(sub.commit_sha or '')[:7]}): {sub.state.value}"]
        if ev:
            lines.append(f"Score {ev.total_score:g} (confidence {ev.confidence})")
            for c in s.scalars(
                select(CriterionScore)
                .where(CriterionScore.evaluation_id == ev.id)
                .order_by(CriterionScore.criterion_id)
            ):
                flag = " [flagged]" if c.flagged else ""
                lines.append(f"- {c.criterion_id}: {c.score:g}/{c.max_score:g}{flag}")
            claims = Counter(s.scalars(select(Claim.verification_status).where(Claim.submission_id == sub.id)))
            if claims:
                lines.append(
                    "Claims: " + ", ".join(f"{n} {k.lower().replace('_', ' ')}" for k, n in claims.most_common())
                )
            for q in ev.suggested_questions[:3]:
                lines.append(f"? {q}")
        return "\n".join(lines)

    def _evidence(self, s: Session, scope: EventScope, rest: str) -> str:
        t, _ = self._resolve(s, scope, rest)
        if t is None:
            return self._not_found(s, scope, rest)
        sub, _ = _latest(s, t)
        if sub is None:
            return f"{t.name}: no submission yet."
        rows = s.scalars(
            select(Evidence)
            .where(
                Evidence.submission_id == sub.id, Evidence.stage_attempt == sub.source_hashes.get("evidence_attempt")
            )
            .order_by(Evidence.status, Evidence.key)
            .limit(20)
        ).all()
        return (
            f"{t.name} evidence:\n" + "\n".join(f"[{e.status}] {e.source_locator}: {e.observation[:140]}" for e in rows)
            if rows
            else f"{t.name}: no evidence yet."
        )

    def _ask(self, s: Session, scope: EventScope, rest: str) -> str:
        t, question = self._resolve(s, scope, rest)
        if t is None:
            return self._not_found(s, scope, rest)
        if not question:
            return "Usage: /ask <team> <question>"
        sub, _ = _latest(s, t)
        if sub is None:
            return f"{t.name}: no submission yet."
        hits = hybrid_search(s, scope.event_ids, question, self.embedder, submission_id=sub.id, k=6)
        assert all(h.submission_id == sub.id for h in hits)  # scope is enforced in SQL; belt and braces
        if not hits:
            return f"I found nothing in {t.name}'s code or evidence about that."
        excerpts = "\n\n".join(f"[S{i}] {h.locator}\n{h.content[:1500]}" for i, h in enumerate(hits, 1))
        meter = Meter()
        try:
            with submission_repo(sub.repo_url):
                state = self.run_agent(
                    team.copilot(self.model, meter),
                    {
                        "question_block": fence("judge_question", question, 1000),
                        "excerpts_block": fence("excerpts", excerpts),
                    },
                )
        except FreeTierRefused:
            return f"{t.name}: the Gemini API free tier is limited to the demo-cast repos; /evidence still works."
        _usage(s, sub, meter)
        answer = str(state.get("answer") or "No answer.").strip()
        cited = [h for i, h in enumerate(hits, 1) if f"[S{i}]" in answer] or hits[:3]
        s.info["last_refs"] = [str(h.chunk_id) for h in cited]
        return f"{answer}\n\nSources: " + "; ".join(h.locator for h in cited)

    def _log(self, s: Session, scope: EventScope, tg: int, text: str, reply: str) -> None:
        for m in s.scalars(
            select(JudgeMembership)
            .where(JudgeMembership.telegram_user_id == tg, JudgeMembership.status == "active")
            .limit(1)
        ):
            s.add(
                JudgeQuery(
                    tenant_id=m.tenant_id,
                    event_id=m.event_id,
                    judge_membership_id=m.id,
                    query=text[:2000],
                    evidence_refs=s.info.pop("last_refs", []),
                    response_meta={"chars": len(reply), "command": text.split(" ")[0][:20]},
                )
            )


def _latest(s: Session, t: Team) -> tuple[Submission | None, Evaluation | None]:
    sub = s.scalars(select(Submission).where(Submission.team_id == t.id).order_by(Submission.version.desc())).first()
    if sub is None:
        return None, None
    ev = s.scalars(
        select(Evaluation)
        .where(Evaluation.submission_id == sub.id, Evaluation.status == "ready")
        .order_by(Evaluation.created_at.desc())
    ).first()
    return sub, ev


def _usage(s: Session, sub: Submission, meter: Meter) -> None:
    for c in meter.calls:
        s.add(
            UsageRecord(
                tenant_id=sub.tenant_id,
                event_id=sub.event_id,
                submission_id=sub.id,
                model=c.model,
                prompt_tokens=c.prompt_tokens,
                output_tokens=c.output_tokens,
                cost_usd=c.cost_usd,
                latency_ms=c.latency_ms,
                stage="COPILOT",
                agent=c.agent,
                tokens_per_sec=c.tokens_per_sec,
            )
        )
