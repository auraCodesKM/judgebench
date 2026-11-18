"""ADK agent trajectory tests (§15.2a): routing, tool trajectory (ADK TrajectoryEvaluator), tool
arguments, budgets, and score variance across concurrent repeated runs."""

import random
import threading
from typing import Any

import pytest
from google.genai import types
from sqlalchemy.orm import Session, sessionmaker

from judgebench.agent_eval import Case, evaluate_case, render
from judgebench.db import models as m
from tests import fixtures_repos as repos
from tests.fake_llm import Seen, evaluator, fake
from tests.test_m2_flow import deps, submit

pytestmark = pytest.mark.db
RUNS = 4
CASES = [
    (Case("alpha-rag", ["rag_specialist", "generic_specialist"], ["search_code"]), repos.ALPHA_RAG),
    (Case("gamma-agent", ["agentic_specialist", "generic_specialist"], ["search_code"]), repos.GAMMA_MULTI_AGENT),
    (Case("ml", ["ml_specialist", "generic_specialist"], ["search_code"]), repos.ML_CLASSIFIER),
]


def runs(session: Session, name: str) -> list[Any]:
    return [submit(session, f"{name} run {i}").id for i in range(RUNS)]


@pytest.mark.parametrize("case,files", CASES, ids=[c.name for c, _ in CASES])
def test_trajectory_routing_budget_and_variance(
    session_factory: sessionmaker[Session], session: Session, case: Case, files: dict[str, str]
) -> None:
    report = evaluate_case(session_factory, deps(files, fake()), case, runs(session, case.name), set(files))
    print("\n" + render([report]))
    assert report.passed, render([report])
    assert report.runs == RUNS and report.total_variance == 0.0  # deterministic fake: no variance


def test_variance_is_measured_across_runs(session_factory: sessionmaker[Session], session: Session) -> None:
    rng, lock = random.Random(7), threading.Lock()

    def jittery(s: Seen) -> dict[str, Any]:
        out = evaluator(s)
        with lock:
            for c in out["criteria"]:
                if c["criterion_id"] != "model_quality":
                    c["score"] = max(0, c["score"] + rng.choice([-2, -1, 0, 1, 2]))
        return out

    case = CASES[0][0]
    report = evaluate_case(
        session_factory,
        deps(repos.ALPHA_RAG, fake(rubric_evaluator=jittery)),
        case,
        runs(session, "jitter"),
        set(repos.ALPHA_RAG),
    )
    print("\n" + render([report]))
    assert report.total_variance > 0 and max(report.criterion_variance.values()) > 0
    assert report.criterion_variance["model_quality"] == 0.0  # capped by the critic every time


def test_bad_tool_arguments_fail_the_case(session_factory: sessionmaker[Session], session: Session) -> None:
    def wanders(s: Seen) -> Any:
        if s.turn == 0:
            return types.FunctionCall(
                name="read_file", args={"path": "../../etc/passwd", "start_line": 1, "end_line": 5}
            )
        return {"verdicts": []}

    case = Case("alpha-bad", ["rag_specialist", "generic_specialist"], ["search_code"])
    report = evaluate_case(
        session_factory,
        deps(repos.ALPHA_RAG, fake(claim_investigator=wanders)),
        case,
        [submit(session, "bad run").id],
        set(repos.ALPHA_RAG),
    )
    assert not report.passed and report.tool_args_valid == 0.0 and report.trajectory_score == 0.0
    # and the tool itself refused to read outside the snapshot
    run = session.query(m.AgentRun).filter_by(agent_name="claim_investigator").one()
    assert "no such file in this submission" in run.trace[0]["observation"]


def test_bench_runner_smoke(session_factory: sessionmaker[Session]) -> None:
    from judgebench import bench

    metrics, reports = bench.run(session_factory, deps(repos.ALPHA_RAG, fake()), runs=1)
    assert metrics.queries == 16 and len(reports) == 6
    assert all(r.runs == 1 and not r.failures for r in reports)


def test_bench_only_and_cast_answers(session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch) -> None:
    import yaml

    from judgebench import bench

    spec = yaml.safe_load(bench.CAST.read_text())
    spec["repos"]["beta"]["answers"] = {"deployed_url": "https://beta.example.app/", "sample_request": "GET /health"}
    monkeypatch.setattr(bench, "CAST", type("P", (), {"read_text": lambda self: yaml.safe_dump(spec)})())
    _, reports = bench.run(session_factory, deps(repos.ALPHA_RAG, fake()), runs=1, only=["beta"])
    assert [r.name for r in reports] == ["beta"]
    with session_factory() as s:
        sub = s.query(m.Submission).filter_by(form_response_id="beta-0").one()
        assert sub.deployed_url == "https://beta.example.app/" and sub.form_answers["sample_request"] == "GET /health"
