"""Structured agent outputs. Code validates and clamps every one of these before persisting."""

from typing import Literal

from pydantic import BaseModel, Field

ClaimKind = Literal["accuracy", "performance", "scale", "testing", "architecture", "feature", "data"]
ClaimStatus = Literal[
    "VERIFIED", "PARTIALLY_VERIFIED", "UNVERIFIED", "CONTRADICTED", "MISSING_EVIDENCE", "NOT_VERIFIABLE"
]


class ExtractedClaim(BaseModel):
    text: str = Field(max_length=300)
    source: Literal["readme", "form"]
    locator: str = Field(max_length=120, description="README heading or form field name")
    kind: ClaimKind


class ClaimList(BaseModel):
    claims: list[ExtractedClaim] = Field(max_length=15)


class DemoObservation(BaseModel):
    claim_id: str = Field(description="C1, C2, ...")
    shown: bool
    contradicted: bool
    timestamp: str | None = Field(default=None, pattern=r"^\d{1,2}:\d{2}$")
    note: str = Field(max_length=300)
    confidence: float = Field(ge=0, le=1)


class DemoReview(BaseModel):
    available: bool
    observations: list[DemoObservation] = Field(max_length=15)


class SpecialistAssessment(BaseModel):
    summary: str = Field(max_length=800)
    strengths: list[str] = Field(max_length=5)
    gaps: list[str] = Field(max_length=5)
    evidence_ids: list[str] = Field(max_length=20)


class ClaimVerdict(BaseModel):
    claim_id: str
    status: ClaimStatus
    evidence_ids: list[str] = []
    code_refs: list[str] = Field(default=[], description='Code locations found with tools, "path#L10-L20"')
    rationale: str = Field(max_length=600)


class ClaimVerdicts(BaseModel):
    verdicts: list[ClaimVerdict]


class CriterionResult(BaseModel):
    criterion_id: str
    score: float
    rationale: str = Field(max_length=1200)
    evidence_ids: list[str] = []
    confidence: float = Field(ge=0, le=1)
    missing_evidence: list[str] = Field(default=[], max_length=5)
    suggested_question: str | None = Field(default=None, max_length=300)


class SuggestedQuestion(BaseModel):
    question: str = Field(max_length=300, description="Specific to this team; quote the claim or finding")
    evidence_ids: list[str] = Field(description="The evidence that prompted it (E3, ...); required")


class RubricResult(BaseModel):
    criteria: list[CriterionResult]
    suggested_questions: list[SuggestedQuestion] = Field(default=[], max_length=3)


class Revision(BaseModel):
    criterion_id: str
    revised_score: float
    reason: str = Field(max_length=400)
    flag_for_judges: bool = True


class Critique(BaseModel):
    revisions: list[Revision] = Field(default=[], max_length=10)
    notes: list[str] = Field(default=[], max_length=5)
