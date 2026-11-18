"""`judgebench agreement <slug>`: AI vs human criterion scores (§15.2 "Evaluation", §15.3).

Pairs every numeric Judge Reviews score with the AI's final score for the same team and criterion.
Reports, per criterion and overall: pairs, mean absolute error (points and % of max), share within
10% of max, Spearman rank correlation, and the same MAE between human judges as the baseline for
how much people disagree with each other. Mock (offline) evaluations are flagged, never hidden.
"""

import statistics
import uuid
from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations

from sqlalchemy import select
from sqlalchemy.orm import Session

from judgebench.agents.modes import is_mock
from judgebench.db.models import CriterionScore, Evaluation, JudgeReview


@dataclass
class Row:
    criterion: str
    pairs: int
    mae: float | None
    mae_pct: float | None
    within_10pct: float | None
    spearman: float | None
    judge_pairs: int
    judge_mae_pct: float | None


def _spearman(x: list[float], y: list[float]) -> float | None:
    if len(x) < 3 or len(set(x)) < 2 or len(set(y)) < 2:
        return None
    return round(statistics.correlation(x, y, method="ranked"), 3)


def report(session: Session, event_id: uuid.UUID) -> tuple[list[Row], int]:
    """Returns rows (one per criterion, then "ALL") and the number of mock evaluations involved."""
    reviews = session.scalars(select(JudgeReview).where(JudgeReview.event_id == event_id)).all()
    human: dict[tuple[uuid.UUID, str], dict[str, float]] = defaultdict(
        dict
    )  # (evaluation, criterion) -> judge -> score
    for r in reviews:
        for crit, score in r.human_scores.items():
            human[(r.evaluation_id, crit)][r.judge_label or str(r.judge_membership_id)] = float(score)
    ai = {
        (c.evaluation_id, c.criterion_id): c
        for c in session.scalars(select(CriterionScore).where(CriterionScore.evaluation_id.in_({k[0] for k in human})))
    }
    mock = sum(
        is_mock(e.model) for e in session.scalars(select(Evaluation).where(Evaluation.id.in_({k[0] for k in human})))
    )

    per: dict[str, list[tuple[float, float, float]]] = defaultdict(list)  # (ai, human mean, max)
    judges: dict[str, list[float]] = defaultdict(list)  # |judge a - judge b| / max
    for (eid, crit), by_judge in human.items():
        c = ai.get((eid, crit))
        if c is None or not c.max_score:
            continue  # "overall" or a criterion the AI did not score
        per[crit].append((c.score, statistics.mean(by_judge.values()), c.max_score))
        judges[crit] += [abs(a - b) / c.max_score for a, b in combinations(by_judge.values(), 2)]

    def row(name: str, pts: list[tuple[float, float, float]], jd: list[float]) -> Row:
        if not pts:
            return Row(name, 0, None, None, None, None, len(jd), None)
        err = [abs(a - h) for a, h, _ in pts]
        pct = [e / mx for e, (_, _, mx) in zip(err, pts, strict=True)]
        return Row(
            name,
            len(pts),
            round(statistics.mean(err), 2),
            round(100 * statistics.mean(pct), 1),
            round(sum(p <= 0.10 for p in pct) / len(pct), 2),
            _spearman([a / mx for a, _, mx in pts], [h / mx for _, h, mx in pts]),
            len(jd),
            round(100 * statistics.mean(jd), 1) if jd else None,
        )

    rows = [row(c, per[c], judges[c]) for c in sorted(per)]
    rows.append(row("ALL", [p for v in per.values() for p in v], [d for v in judges.values() for d in v]))
    return rows, mock


def render(rows: list[Row], mock: int) -> str:
    def f(v: float | None, suffix: str = "") -> str:
        return "-" if v is None else f"{v:g}{suffix}"

    out = []
    if mock:
        out.append(f"WARNING: {mock} evaluation(s) are MOCK (LLM_MODE=offline). Agreement with them is not a result.")
    out.append(
        f"{'criterion':24} {'pairs':>5} {'MAE':>6} {'MAE%':>6} {'<=10%':>6} {'rho':>6}   {'judge pairs':>11} {'judge MAE%':>10}"
    )
    for r in rows:
        out.append(
            f"{r.criterion:24} {r.pairs:>5} {f(r.mae):>6} {f(r.mae_pct, '%'):>6} {f(r.within_10pct):>6} "
            f"{f(r.spearman):>6}   {r.judge_pairs:>11} {f(r.judge_mae_pct, '%'):>10}"
        )
    out.append("rho needs >=3 pairs with spread. Judge MAE% is the human-vs-human baseline. Small samples: state n.")
    return "\n".join(out)
