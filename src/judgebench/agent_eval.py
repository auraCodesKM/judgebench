"""Agent trajectory evaluation (§15.2a), run in CI like any other test.

For each benchmark case the whole pipeline runs N times concurrently (N submissions of the same
repo, N worker threads). Per case we report:
  - routing: did the orchestrator call exactly the expected specialists, then investigator, then evaluator
  - tool trajectory: ADK's TrajectoryEvaluator (IN_ORDER, args ignored) on the investigator's tool calls
  - tool arguments: share of read_file/list_evidence calls that name a real file / valid source type
  - budget: max investigator tool calls and tokens per run, against the configured limits
  - variance: per-criterion and total score variance across the N runs
"""

import statistics
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

from google.adk.evaluation.eval_case import IntermediateData, Invocation
from google.adk.evaluation.eval_metrics import EvalMetric, ToolTrajectoryCriterion
from google.adk.evaluation.trajectory_evaluator import TrajectoryEvaluator
from google.genai import types
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from judgebench.agents.team import INVESTIGATOR_TOOL_BUDGET
from judgebench.db.models import AuditEvent, CriterionScore, Evaluation, Submission, UsageRecord
from judgebench.worker.stages import Deps, run_next

SOURCE_TYPES = {"", "code", "ci", "live_check", "demo", "documentation", "form"}


@dataclass
class Case:
    name: str
    expected_specialists: list[str]
    expected_tools: list[str]  # investigator tool names, in order (other calls may be interleaved)


@dataclass
class CaseReport:
    name: str
    runs: int
    routing_ok: float
    trajectory_score: float
    tool_args_valid: float
    max_tool_calls: int
    max_tokens: int
    total_mean: float
    total_variance: float
    criterion_variance: dict[str, float] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return (
            self.routing_ok == 1.0
            and self.trajectory_score == 1.0
            and self.tool_args_valid == 1.0
            and self.max_tool_calls <= INVESTIGATOR_TOOL_BUDGET
            and not self.failures
        )


def _trajectory(expected: list[str], actual: list[dict[str, Any]]) -> float:
    def inv(names: list[str]) -> Invocation:
        return Invocation(
            invocation_id=str(uuid.uuid4()),
            user_content=types.Content(parts=[types.Part(text="x")]),
            intermediate_data=IntermediateData(tool_uses=[types.FunctionCall(name=n, args={}) for n in names]),
        )

    metric = EvalMetric(
        metric_name="tool_trajectory_avg_score",
        criterion=ToolTrajectoryCriterion(
            threshold=1.0, match_type=ToolTrajectoryCriterion.MatchType.IN_ORDER, ignore_args=True
        ),
    )
    result = TrajectoryEvaluator(eval_metric=metric).evaluate_invocations(
        [inv([t["tool"] for t in actual])], [inv(expected)]
    )
    return float(result.overall_score or 0.0)


def _args_valid(calls: list[dict[str, Any]], files: set[str]) -> list[bool]:
    out = []
    for c in calls:
        if c["tool"] == "read_file":
            out.append(c["args"].get("path") in files)
        elif c["tool"] == "list_evidence":
            out.append(c["args"].get("source_type", "") in SOURCE_TYPES)
        else:
            out.append(bool(str(c["args"].get("text", "")).strip()))
    return out


def evaluate_case(
    factory: sessionmaker[Session],
    deps: Deps,
    case: Case,
    submissions: list[uuid.UUID],
    repo_files: set[str],
    workers: int = 4,
) -> CaseReport:
    """`submissions` are N fresh RECEIVED submissions of the same repo; they are processed concurrently."""

    def worker() -> None:
        while run_next(factory, deps) is not None:
            pass

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    routing, traj, args_ok, tool_counts, tokens, totals = [], [], [], [], [], []
    per_crit: dict[str, list[float]] = {}
    failures = []
    expected_agents = [*case.expected_specialists, "claim_investigator", "rubric_evaluator"]
    with factory() as s:
        for sid in submissions:
            sub = s.get_one(Submission, sid)
            audit = s.scalars(
                select(AuditEvent).where(AuditEvent.submission_id == sid, AuditEvent.action == "state:CRITIQUING")
            ).first()
            ev = s.scalars(
                select(Evaluation).where(Evaluation.submission_id == sid, Evaluation.status == "ready")
            ).first()
            if audit is None or ev is None:
                failures.append(f"{sid}: ended in {sub.state.value} ({sub.error_category})")
                continue
            routing.append(audit.detail.get("agents") == expected_agents)
            tools = audit.detail.get("tools", [])
            traj.append(_trajectory(case.expected_tools, tools))
            args_ok += _args_valid(tools, repo_files)
            tool_counts.append(len(tools))
            tokens.append(
                sum(
                    r.prompt_tokens + r.output_tokens
                    for r in s.scalars(select(UsageRecord).where(UsageRecord.submission_id == sid))
                )
            )
            totals.append(ev.total_score or 0.0)
            for c in s.scalars(select(CriterionScore).where(CriterionScore.evaluation_id == ev.id)):
                per_crit.setdefault(c.criterion_id, []).append(c.score)
    n = max(len(totals), 1)
    return CaseReport(
        case.name,
        len(submissions),
        sum(routing) / n if routing else 0.0,
        statistics.mean(traj) if traj else 0.0,
        sum(args_ok) / len(args_ok) if args_ok else 1.0,
        max(tool_counts, default=0),
        max(tokens, default=0),
        round(statistics.mean(totals), 2) if totals else 0.0,
        round(statistics.pvariance(totals), 3) if totals else 0.0,
        {k: round(statistics.pvariance(v), 3) for k, v in sorted(per_crit.items())},
        failures,
    )


def render(reports: list[CaseReport]) -> str:
    lines = [
        f"{'case':12} {'runs':>4} {'route':>6} {'traj':>5} {'args':>5} {'tools':>5} {'tokens':>7} "
        f"{'total':>6} {'var':>6}  verdict"
    ]
    for r in reports:
        lines.append(
            f"{r.name:12} {r.runs:>4} {r.routing_ok:>6.2f} {r.trajectory_score:>5.2f} {r.tool_args_valid:>5.2f} "
            f"{r.max_tool_calls:>5} {r.max_tokens:>7} {r.total_mean:>6.1f} {r.total_variance:>6.2f}  "
            f"{'PASS' if r.passed else 'FAIL'}"
        )
        worst = max(r.criterion_variance.items(), key=lambda kv: kv[1], default=None)
        if worst and worst[1] > 0:
            lines.append(f"{'':12} most variable criterion: {worst[0]} (variance {worst[1]})")
        lines += [f"{'':12} ! {f}" for f in r.failures]
    return "\n".join(lines)
