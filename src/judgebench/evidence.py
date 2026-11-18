"""Normalised evidence (§9). Adapters, CI, live check and demo all emit `Finding`s; the worker
persists them as append-only `evidence` rows with provenance. Statuses are enums, never free text."""

import hashlib
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

EVALUATOR_VERSION = "0.2.0"


class SourceType(StrEnum):
    CODE = "code"
    DOCUMENTATION = "documentation"
    CI = "ci"
    LIVE_CHECK = "live_check"
    DEMO = "demo"
    FORM = "form"


class EvidenceStatus(StrEnum):
    OBSERVED = "OBSERVED"
    VERIFIED = "VERIFIED"
    INFERRED = "INFERRED"
    MISSING = "MISSING"
    CONTRADICTED = "CONTRADICTED"


class CheckStatus(StrEnum):
    """Verification status of a behaviour check (§7.5, §8.3). Separate from score."""

    VERIFIED = "VERIFIED"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    NOT_VERIFIABLE = "NOT_VERIFIABLE"
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    CHECK_FAILED = "CHECK_FAILED"


class Finding(BaseModel):
    key: str = Field(pattern=r"^[a-z_]+\.[a-z0-9_.]+$")  # "<source>.<what>", stable across runs
    observation: str
    source_type: SourceType
    source_locator: str  # "app/rag.py#L10-L24", "ci:run/123", "demo#01:42", "live:GET /health"
    status: EvidenceStatus
    confidence: float = Field(ge=0, le=1)
    verification_method: str  # static_analysis | ci_read | live_check | demo_review | form | repository_inspection
    criterion: str | None = None
    claim: str | None = None
    limitations: list[str] = []
    path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    file_sha256: str | None = None
    behavioural: bool = False  # a behaviour source: CI that ran real tests, or a successful live check


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
