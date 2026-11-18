"""Scripted test handlers for the offline FakeLlm. They deliberately misbehave in places (over-claim
VERIFIED, cite nothing for model_quality) so the code-level clamps and critic rules are exercised."""

import re
from typing import Any

from google.genai import types

from judgebench.agents.offline import FakeLlm, Handler, Seen, claims_from_readme

__all__ = ["FakeLlm", "Seen", "fake"]


# --- default "honest" behaviour --------------------------------------------------------


def demo_review(s: Seen) -> dict[str, Any]:
    obs = []
    for cid in s.ids("C"):
        line = next((ln for ln in s.text.splitlines() if ln.startswith(cid + " ")), "")
        if "real" in line.lower():
            obs.append(
                {
                    "claim_id": cid,
                    "shown": False,
                    "contradicted": True,
                    "timestamp": "1:42",
                    "note": "demo shows ~8 s response",
                    "confidence": 0.8,
                }
            )
        else:
            obs.append(
                {
                    "claim_id": cid,
                    "shown": True,
                    "contradicted": False,
                    "timestamp": "0:35",
                    "note": "feature shown working",
                    "confidence": 0.7,
                }
            )
    return {"available": True, "observations": obs}


def specialist(s: Seen) -> dict[str, Any]:
    return {"summary": f"{s.agent} reviewed the evidence.", "strengths": [], "gaps": [], "evidence_ids": s.ids("E")[:3]}


def investigator(s: Seen) -> dict[str, Any] | types.FunctionCall:
    if s.turn == 0:  # act: look at the code once, then decide
        return types.FunctionCall(name="search_code", args={"text": "similarity_search"})
    # Over-claims VERIFIED for everything; the code-level clamp must correct it.
    return {
        "verdicts": [
            {
                "claim_id": c,
                "status": "VERIFIED",
                "evidence_ids": s.ids("E")[:4],
                "code_refs": [],
                "rationale": "looked at the code",
            }
            for c in s.ids("C")
        ]
    }


def evaluator(s: Seen) -> dict[str, Any]:
    ev = s.ids("E")
    # model_quality cites nothing at 16/20: the critic rule must cut it.
    return {
        "criteria": [
            {
                "criterion_id": cid,
                "score": 16 if cid == "model_quality" else round(mx * 0.6),
                "rationale": f"{cid} assessed",
                "evidence_ids": [] if cid == "model_quality" else ev[:2],
                "confidence": 0.6,
                "missing_evidence": [],
                "suggested_question": f"Ask about {cid}?",
            }
            for cid, mx in s.criteria()
        ],
        "suggested_questions": [
            "How did you evaluate retrieval?",
            "What happens without the GPU?",
            "Show a failure case.",
        ],
    }


def copilot(s: Seen) -> str:
    srcs = re.findall(r"\[S\d+\] (\S+)", s.text)
    return f"The retriever is called in the request handler [S1] ({srcs[0] if srcs else '?'})."


def no_revisions(s: Seen) -> dict[str, Any]:
    return {"revisions": [], "notes": []}


DEFAULT: dict[str, Handler] = {
    "claim_analyst": claims_from_readme,
    "demo_analyst": demo_review,
    "claim_investigator": investigator,
    "rubric_evaluator": evaluator,
    "critic": no_revisions,
    "copilot": copilot,
    **{f"{a}_specialist": specialist for a in ("rag", "agentic", "ml", "generic")},
}


def fake(**overrides: Handler) -> FakeLlm:
    return FakeLlm(handlers={**DEFAULT, **overrides}, seen=[])
