"""Deterministic scoring policy (§8.3) and critic rules (§10.3 #6). The model proposes; this code
validates, clamps and has the last word. The LLM critic can only lower a score or flag it."""

import math
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from judgebench.agents.schemas import CriterionResult, Critique, RubricResult
from judgebench.agents.views import EvidenceView
from judgebench.event_config import Criterion

UNSUPPORTED_CAP = 0.45  # share of max an uncited score is cut to (16/20 -> 9/20)
EVAL_EVIDENCE = {"rag.retrieval_evaluation", "ml.metrics_on_holdout", "ml.metrics"}
SUPPORTING = {"OBSERVED", "VERIFIED"}


@dataclass
class Score:
    criterion_id: str
    name: str
    max: float
    score: float
    rationale: str
    evidence_ids: list[str]
    confidence: float
    missing: list[str] = field(default_factory=list)
    question: str | None = None
    pre_critic: float | None = None
    notes: list[str] = field(default_factory=list)
    flagged: bool = False


def validate_rubric(
    raw: Any, rubric: list[Criterion], evidence: dict[str, EvidenceView]
) -> tuple[list[Score], list[str]]:
    flags: list[str] = []
    try:
        result = RubricResult.model_validate(raw)
    except ValidationError:
        result = RubricResult(criteria=[])
        flags.append("evaluator output invalid")
    got: dict[str, CriterionResult] = {}
    for r in result.criteria:
        got.setdefault(r.criterion_id, r)
    scores = []
    for c in rubric:
        found = got.get(c.id)
        if found is None:
            flags.append(f"no score for {c.id}")
            scores.append(Score(c.id, c.name, c.weight, 0.0, "Evaluator returned no score.", [], 0.0, flagged=True))
            continue
        value = min(max(found.score, 0.0), float(c.weight))
        scores.append(
            Score(
                c.id,
                c.name,
                c.weight,
                value,
                found.rationale,
                [e for e in found.evidence_ids if e in evidence],
                found.confidence,
                found.missing_evidence,
                found.suggested_question,
                pre_critic=value,
            )
        )
    return scores, flags


def apply_rules(scores: list[Score], evidence: dict[str, EvidenceView]) -> list[str]:
    """Rule 1: a score above half of max must cite supporting evidence. Rule 2: model quality above
    half must cite evaluation evidence. Violations are cut to UNSUPPORTED_CAP and flagged."""
    messages = []
    for s in scores:
        cited = [evidence[e] for e in s.evidence_ids if e in evidence]
        reason = None
        if (
            s.criterion_id == "model_quality"
            and s.score > s.max / 2
            and not any(e.key in EVAL_EVIDENCE and e.status in SUPPORTING for e in cited)
        ):
            reason = "cited no evaluation evidence"
        elif s.score > s.max / 2 and not any(e.status in SUPPORTING for e in cited):
            reason = "cited no supporting evidence"
        if reason:
            new = float(math.floor(s.max * UNSUPPORTED_CAP))
            messages.append(
                f'"{s.name} {s.score:g}/{s.max:g}" {reason} -> revised to {new:g}/{s.max:g}, flagged for judges'
            )
            s.score, s.flagged = new, True
            s.notes.append(reason)
    return messages


def apply_critique(scores: list[Score], raw: Any) -> list[str]:
    try:
        critique = Critique.model_validate(raw)
    except ValidationError:
        return []
    by_id = {s.criterion_id: s for s in scores}
    messages = []
    for rev in critique.revisions:
        s = by_id.get(rev.criterion_id)
        if s is None:
            continue
        if rev.revised_score < s.score:  # never raise
            new = max(0.0, rev.revised_score)
            messages.append(
                f'"{s.name} {s.score:g}/{s.max:g}" {rev.reason} -> revised to {new:g}/{s.max:g}'
                + (", flagged for judges" if rev.flag_for_judges else "")
            )
            s.score = new
        s.flagged = s.flagged or rev.flag_for_judges
        s.notes.append(rev.reason)
    return messages


def overall_confidence(scores: list[Score], flags: list[str]) -> str:
    """Derived from the criteria: the max-weighted mean of criterion confidence (>=0.7 high, >=0.4
    medium, else low). Flagged scores and evaluation flags can only lower it (any: at most medium; 3+: low)."""
    weight = sum(s.max for s in scores) or 1.0
    mean = sum(s.confidence * s.max for s in scores) / weight
    level = 2 if mean >= 0.7 else 1 if mean >= 0.4 else 0
    flagged = sum(s.flagged for s in scores) + len(flags)
    if flagged >= 3:
        level = 0
    elif flagged:
        level = min(level, 1)
    return ("low", "medium", "high")[level]
