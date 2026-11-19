"""`judgebench bench`: run the demo-cast benchmark against the configured model + embedder.

Creates a scratch event ("bench-<timestamp>"), submits every cast repo `runs` times, processes
them concurrently with the real pipeline, then reports retrieval Recall@k/MRR on the labelled
queries and agent trajectory/variance per repo. Real GitHub, real Gemini, real embeddings:
this is what RESULTS.md numbers come from (state the date, models and sample sizes with them).
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session, sessionmaker

from judgebench.agent_eval import Case, CaseReport, evaluate_case
from judgebench.db.models import Event, Submission, Team, Tenant, utcnow
from judgebench.retrieval import Metrics, evaluate, hybrid_search
from judgebench.states import State
from judgebench.worker.stages import Deps

CAST = Path(__file__).resolve().parents[2] / "benchmark" / "cast.yaml"


def run(
    factory: sessionmaker[Session], deps: Deps, runs: int = 3, k: int = 3, only: list[str] | None = None
) -> tuple[Metrics, list[CaseReport]]:
    spec: dict[str, Any] = yaml.safe_load(CAST.read_text())
    if only:
        spec["repos"] = {n: r for n, r in spec["repos"].items() if n in only}
        spec["queries"] = [q for q in spec["queries"] if q["repo"] in only]
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    with factory() as s:
        tenant = s.query(Tenant).filter_by(name="default").first() or Tenant(name="default")
        event = Event(
            tenant=tenant,
            slug=f"bench-{stamp}",
            name=f"Benchmark {stamp}",
            status="closed",
            rubric_version="default-v1",
            config={
                "slug": f"bench-{stamp}",
                "name": "Benchmark",
                "organisation": "bench",
                "deadline": utcnow().isoformat(),
                "telegram_chat_id": 0,
            },
        )
        s.add(event)
        s.flush()
        subs: dict[str, list[Any]] = {}
        for name, repo in spec["repos"].items():
            for i in range(runs):
                label = f"Team {name.capitalize()}" + (f" #{i + 1}" if runs > 1 else "")
                team = Team(tenant_id=event.tenant_id, event_id=event.id, name=label, name_key=label.casefold())
                s.add(team)
                s.flush()
                sub = Submission(
                    tenant_id=event.tenant_id,
                    event_id=event.id,
                    team_id=team.id,
                    form_response_id=f"{name}-{i}",
                    version=1,
                    submitted_at=utcnow(),
                    repo_url=repo["url"],
                    form_answers=dict(repo.get("answers", {})),  # deployed_url, sample_request, demo_url, ...
                    deployed_url=repo.get("answers", {}).get("deployed_url"),
                    demo_url=repo.get("answers", {}).get("demo_url"),
                    state=State.RECEIVED,
                )
                s.add(sub)
                s.flush()
                subs.setdefault(name, []).append(sub.id)
        s.commit()
        event_id = event.id

    reports = []
    for name, repo in spec["repos"].items():
        case = Case(name, list(repo["specialists"]), ["search_code"])
        reports.append(evaluate_case(factory, deps, case, subs[name], repo_files=None))

    with factory() as s:
        results = []
        for q in spec["queries"]:
            sid = subs[q["repo"]][0]
            hits = hybrid_search(s, [event_id], q["q"], deps.embedder, submission_id=sid, k=10)
            results.append(([h.locator for h in hits], set(q["relevant"])))
    return evaluate(results, k), reports
