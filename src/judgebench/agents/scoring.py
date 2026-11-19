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
                evidence_confidence(found.evidence_ids, evidence),  # computed, never the model's self-report
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
            reason = "nothing shows how they evaluated their model"
        elif s.score > s.max / 2 and not any(e.status in SUPPORTING for e in cited):
            reason = "nothing in the evidence backs it up"
        if reason:
            new = float(math.floor(s.max * UNSUPPORTED_CAP))
            messages.append(f"I lowered {s.name.lower()} from {s.score:g} to {new:g} (out of {s.max:g}): {reason}.")
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
            why = rev.reason.strip().rstrip(".")
            messages.append(
                f"I lowered {s.name.lower()} from {s.score:g} to {new:g} (out of {s.max:g}): {why[:1].lower() + why[1:]}."
            )
            s.score = new
        s.flagged = s.flagged or rev.flag_for_judges
        s.notes.append(rev.reason)
    return messages


# How much a cited evidence item supports a judgement. A contradiction is as informative as an observation.
STATUS_WEIGHT = {"VERIFIED": 1.0, "OBSERVED": 0.7, "CONTRADICTED": 0.7, "INFERRED": 0.4, "MISSING": 0.3}


def evidence_confidence(ids: list[str], evidence: dict[str, EvidenceView]) -> float:
    """Criterion confidence from the evidence it cites, in code: mean status weight, +0.2 if a cited item
    is behaviour-verified (CI that ran real tests, a passing live check), +0.1 for two or more independent
    source types. Nothing cited: 0.1. The model's own confidence is ignored (it is not calibrated)."""
    cited = [evidence[i] for i in dict.fromkeys(ids) if i in evidence]
    if not cited:
        return 0.1
    conf = sum(STATUS_WEIGHT.get(e.status, 0.3) for e in cited) / len(cited)
    if any(e.behavioural and e.status == "VERIFIED" for e in cited):
        conf += 0.2
    if len({e.source_type for e in cited}) >= 2:
        conf += 0.1
    return round(min(conf, 1.0), 2)


def overall_confidence(scores: list[Score], flags: list[str], behavioural: bool = True) -> str:
    """From the (evidence-computed) criteria: the max-weighted mean (>=0.7 high, >=0.4 medium, else low).
    Without any behaviour-verified evidence in the submission it is at most medium; flagged scores and
    evaluation flags only lower it (any: at most medium; 3+: low)."""
    weight = sum(s.max for s in scores) or 1.0
    mean = sum(s.confidence * s.max for s in scores) / weight
    level = 2 if mean >= 0.7 else 1 if mean >= 0.4 else 0
    flagged = sum(s.flagged for s in scores) + len(flags)
    if flagged >= 3:
        level = 0
    elif flagged or not behavioural:
        level = min(level, 1)
    return ("low", "medium", "high")[level]
