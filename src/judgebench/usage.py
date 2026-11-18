"""LLM-native metrics per submission and per agent (§16.1), from `usage_records` (one row per model
call: model, tokens, latency, tokens/sec, estimated cost, stage, agent)."""

import statistics
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from judgebench.db.models import AuditEvent, Submission, Team, UsageRecord


@dataclass(frozen=True)
class Usage:
    label: str
    calls: int
    prompt_tokens: int
    output_tokens: int
    cost_usd: float
    p50_ms: int
    p95_ms: int
    tokens_per_sec: float | None
    end_to_end_s: int | None = None


def _summ(label: str, rows: list[UsageRecord], e2e: int | None = None) -> Usage:
    lat = sorted(r.latency_ms or 0 for r in rows)
    tps = [r.tokens_per_sec for r in rows if r.tokens_per_sec]
    return Usage(
        label,
        len(rows),
        sum(r.prompt_tokens for r in rows),
        sum(r.output_tokens for r in rows),
        round(sum(r.cost_usd for r in rows), 4),
        int(statistics.median(lat)) if lat else 0,
        lat[max(0, round(0.95 * len(lat)) - 1)] if lat else 0,
        round(statistics.mean(tps), 1) if tps else None,
        e2e,
    )


def report(session: Session, event_id: uuid.UUID) -> tuple[list[Usage], list[Usage]]:
    """(per submission, per agent) for one event."""
    rows = session.scalars(select(UsageRecord).where(UsageRecord.event_id == event_id)).all()
    per_sub = []
    for sub, team in session.execute(
        select(Submission, Team.name)
        .join(Team, Team.id == Submission.team_id)
        .where(Submission.event_id == event_id)
        .order_by(Team.name_key, Submission.version)
    ):
        done = session.scalar(
            select(AuditEvent.created_at).where(
                AuditEvent.submission_id == sub.id, AuditEvent.action == "state:READY_FOR_REVIEW"
            )
        )
        e2e = int((done - sub.created_at).total_seconds()) if done else None
        per_sub.append(_summ(f"{team} v{sub.version}", [r for r in rows if r.submission_id == sub.id], e2e))
    agents = sorted({r.agent or "?" for r in rows})
    return per_sub, [_summ(a, [r for r in rows if (r.agent or "?") == a]) for a in agents]


def render(per_sub: list[Usage], per_agent: list[Usage]) -> str:
    def line(u: Usage) -> str:
        e2e = f"{u.end_to_end_s}s" if u.end_to_end_s is not None else "-"
        tps = f"{u.tokens_per_sec:g}" if u.tokens_per_sec else "-"
        return (
            f"{u.label[:28]:28} {u.calls:>5} {u.prompt_tokens:>9} {u.output_tokens:>8} {u.cost_usd:>8.4f} "
            f"{u.p50_ms:>7} {u.p95_ms:>7} {tps:>7} {e2e:>7}"
        )

    head = f"{'':28} {'calls':>5} {'in tok':>9} {'out tok':>8} {'USD':>8} {'p50 ms':>7} {'p95 ms':>7} {'tok/s':>7} {'e2e':>7}"
    return "\n".join(["Per submission", head, *map(line, per_sub), "", "Per agent", head, *map(line, per_agent)])
