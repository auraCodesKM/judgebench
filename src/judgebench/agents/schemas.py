"""Structured agent outputs. Code validates and clamps every one of these before persisting."""

import re
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, Field


def cap(n: int) -> BeforeValidator:
    """Models don't reliably honour length limits in structured output: cut, never fail the stage."""
    return BeforeValidator(lambda v: v[:n] if isinstance(v, str | list) else v)


Unit = Annotated[float, BeforeValidator(lambda v: min(max(float(v), 0.0), 1.0) if isinstance(v, int | float) else 0.0)]
Clock = Annotated[
    str | None, BeforeValidator(lambda v: v if isinstance(v, str) and re.fullmatch(r"\d{1,2}:\d{2}", v) else None)
]
Str120 = Annotated[str, cap(120)]
Str300 = Annotated[str, cap(300)]
Str400 = Annotated[str, cap(400)]
Str600 = Annotated[str, cap(600)]
Str800 = Annotated[str, cap(800)]
Str1200 = Annotated[str, cap(1200)]

ClaimKind = Literal["accuracy", "performance", "scale", "testing", "architecture", "feature", "data"]
ClaimStatus = Literal[
    "VERIFIED", "PARTIALLY_VERIFIED", "UNVERIFIED", "CONTRADICTED", "MISSING_EVIDENCE", "NOT_VERIFIABLE"
]


class ExtractedClaim(BaseModel):
    text: Str300
    source: Literal["readme", "form"]
    locator: Str120 = Field(description="README heading or form field name")
    kind: ClaimKind


class ClaimList(BaseModel):
    claims: Annotated[list[ExtractedClaim], cap(15)]


class DemoObservation(BaseModel):
    claim_id: str = Field(description="C1, C2, ...")
    shown: bool
    contradicted: bool
    timestamp: Clock = None
    note: Str300
    confidence: Unit = 0.0


class DemoReview(BaseModel):
    available: bool
    observations: Annotated[list[DemoObservation], cap(15)]


class SpecialistAssessment(BaseModel):
    summary: Str800
    strengths: Annotated[list[str], cap(5)]
    gaps: Annotated[list[str], cap(5)]
    evidence_ids: Annotated[list[str], cap(20)]


class ClaimVerdict(BaseModel):
    claim_id: str
    status: ClaimStatus
    evidence_ids: list[str] = []
    code_refs: list[str] = Field(default=[], description='Code locations found with tools, "path#L10-L20"')
    rationale: Str600


class ClaimVerdicts(BaseModel):
    verdicts: list[ClaimVerdict]


class CriterionResult(BaseModel):
    criterion_id: str
    score: float
    rationale: Str1200
    evidence_ids: list[str] = []
    confidence: Unit = 0.0
    missing_evidence: Annotated[list[str], cap(5)] = []
    suggested_question: Str300 | None = None


class SuggestedQuestion(BaseModel):
    question: Str300 = Field(description="Specific to this team; quote the claim or finding")
    evidence_ids: list[str] = Field(description="The evidence that prompted it (E3, ...); required")


class RubricResult(BaseModel):
    criteria: list[CriterionResult]
    suggested_questions: Annotated[list[SuggestedQuestion], cap(3)] = []


class Revision(BaseModel):
    criterion_id: str
    revised_score: float
    reason: Str400
    flag_for_judges: bool = True


class Critique(BaseModel):
    revisions: Annotated[list[Revision], cap(10)] = []
    notes: Annotated[list[str], cap(5)] = []
