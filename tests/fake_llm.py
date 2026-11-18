"""Scripted stand-in for Gemini behind ADK's BaseLlm. Handlers are keyed by agent name and see the
full request, so tests can both drive behaviour and inspect exactly what each agent was shown."""

import json
import re
from collections.abc import AsyncGenerator, Callable
from typing import Any

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

Handler = Callable[["Seen"], dict[str, Any] | str | types.FunctionCall]
_NAME = re.compile(r'internal name is "([^"]+)"')


class Seen:
    """One model request as the fake saw it."""

    def __init__(self, req: LlmRequest, turn: int) -> None:
        self.agent = m.group(1) if (m := _NAME.search(str(req.config.system_instruction or ""))) else "?"
        self.system = str(req.config.system_instruction or "")
        self.turn = turn  # 0 for the agent's first call, 1 after its first tool result, ...
        self.text = "\n".join(p.text for c in req.contents for p in (c.parts or []) if p.text)
        self.tool_results = [
            p.function_response.response for c in req.contents for p in (c.parts or []) if p.function_response
        ]
        self.has_video = any(p.file_data for c in req.contents for p in (c.parts or []))
        self.tools = sorted((req.tools_dict or {}).keys())

    def ids(self, prefix: str) -> list[str]:
        return sorted(set(re.findall(rf"\b{prefix}\d+\b", self.text)), key=lambda x: int(x[1:]))

    def criteria(self) -> list[tuple[str, int]]:
        return [(m.group(1), int(m.group(2))) for m in re.finditer(r"^- ([a-z_]+) \(max (\d+)\)", self.text, re.M)]


class FakeLlm(BaseLlm):
    model: str = "fake-gemini"
    handlers: dict[str, Handler] = {}
    seen: list[Seen] = []

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        turn = sum(1 for c in llm_request.contents for p in (c.parts or []) if p.function_response)
        seen = Seen(llm_request, turn)
        self.seen.append(seen)
        out = self.handlers.get(seen.agent, lambda s: {})(seen)
        if isinstance(out, types.FunctionCall):
            part = types.Part(function_call=out)
        else:
            part = types.Part(text=out if isinstance(out, str) else json.dumps(out))
        yield LlmResponse(
            content=types.Content(role="model", parts=[part]),
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=len(seen.text) // 4 + 100, candidates_token_count=50
            ),
        )


# --- default "honest" behaviour --------------------------------------------------------


def claims_from_readme(s: Seen) -> dict[str, Any]:
    claims = []
    for pattern, kind in [
        (r"\d+% (?:accuracy|F1)[^.\n]*", "accuracy"),
        (r"real[- ]time[^.\n]*", "performance"),
        (r"[Ff]ully tested[^.\n]*", "testing"),
        (r"multi-agent[^.\n]*", "architecture"),
        (r"recall@\d+", "accuracy"),
        (r"citations", "feature"),
    ]:
        if m := re.search(pattern, s.text):
            claims.append({"text": m.group(0)[:200], "source": "readme", "locator": "README.md", "kind": kind})
    return {"claims": claims}


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
