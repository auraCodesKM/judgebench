"""What agents see: evidence and claims rendered with short IDs (E1.., C1..) inside fences, and
the mapping back to database rows. Also the deterministic policy that clamps agent verdicts."""

import uuid
from dataclasses import dataclass

from judgebench.agents.runtime import fence
from judgebench.agents.schemas import ClaimVerdict
from judgebench.event_config import Criterion


@dataclass(frozen=True)
class EvidenceView:
    id: str  # E1
    row_id: uuid.UUID
    key: str
    source_type: str
    status: str
    locator: str
    observation: str
    behavioural: bool
    criterion: str | None


@dataclass(frozen=True)
class ClaimView:
    id: str  # C1
    row_id: uuid.UUID
    text: str
    source: str
    kind: str


def evidence_block(ev: list[EvidenceView], prefixes: tuple[str, ...] = ()) -> str:
    rows = [e for e in ev if not prefixes or e.key.startswith(prefixes)]
    lines = [
        f"{e.id} [{e.source_type}/{e.status}{'/behavioural' if e.behavioural else ''}] {e.key} @ {e.locator}: "
        f"{e.observation}"
        for e in rows
    ]
    return fence("evidence", "\n".join(lines) or "(no evidence)")


def claims_block(claims: list[ClaimView]) -> str:
    return fence("claims", "\n".join(f"{c.id} ({c.source}, {c.kind}): {c.text}" for c in claims) or "(no claims)")


def rubric_block(rubric: list[Criterion]) -> str:
    """Ours, not participant text: no fence."""
    return "Rubric (score each criterion from 0 to its max):\n" + "\n".join(
        f"- {c.id} (max {c.weight}): {c.name}" for c in rubric
    )


BEHAVIOURAL_OK = {"VERIFIED"}
CODE_OK = {"OBSERVED", "VERIFIED"}


def clamp_verdict(v: ClaimVerdict, evidence: dict[str, EvidenceView], has_new_code_ref: bool) -> ClaimVerdict:
    """§7.5 status rules, enforced in code whatever the model said:
    VERIFIED needs a behavioural source (CI with real tests / successful live check) AND code evidence.
    A demo never verifies on its own. CONTRADICTED needs at least one cited evidence item."""
    cited = [evidence[e] for e in v.evidence_ids if e in evidence]
    behavioural = any(e.behavioural and e.status in BEHAVIOURAL_OK for e in cited)
    code = has_new_code_ref or any(e.source_type == "code" and e.status in CODE_OK for e in cited)
    status = v.status
    if status == "VERIFIED" and not (behavioural and code):
        status = "PARTIALLY_VERIFIED" if (behavioural or code) else "UNVERIFIED"
    elif (status == "PARTIALLY_VERIFIED" and not (behavioural or code)) or (status == "CONTRADICTED" and not cited):
        status = "UNVERIFIED"
    return v.model_copy(update={"status": status, "evidence_ids": [e.id for e in cited]})
