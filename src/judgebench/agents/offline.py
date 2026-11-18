"""Offline stand-in for Gemini behind ADK's BaseLlm (LLM_MODE=offline, the default).

Zero network, zero cost. The whole pipeline runs (routing, tools, clamps, critic rules, Sheets,
Telegram) but every agent answer is a deterministic heuristic, labelled MOCK wherever it surfaces.
Handlers are keyed by agent name and see the full request; tests reuse FakeLlm with their own handlers.
"""

import json
import re
from collections.abc import AsyncGenerator, Callable
from typing import Any

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

OFFLINE_MODEL = "offline-mock"
MOCK = "[offline mock] "
Handler = Callable[["Seen"], dict[str, Any] | str | types.FunctionCall]
_NAME = re.compile(r'internal name is "([^"]+)"')
_EVIDENCE = re.compile(r"^(E\d+) \[[a-z_]+/([A-Z_]+)", re.M)


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


def _nothing(s: Seen) -> dict[str, Any]:
    return {}


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
        out = self.handlers.get(seen.agent, _nothing)(seen)
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


# --- offline handlers: deterministic, conservative, never claim to have seen what they did not ----


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


def _no_video(s: Seen) -> dict[str, Any]:
    return {"available": False, "observations": []}  # offline: the video is never watched


def _specialist(s: Seen) -> dict[str, Any]:
    return {
        "summary": MOCK + "no model review; evidence listed as collected.",
        "strengths": [],
        "gaps": [],
        "evidence_ids": s.ids("E")[:3],
    }


def _investigator(s: Seen) -> dict[str, Any]:
    return {
        "verdicts": [
            {"claim_id": c, "status": "UNVERIFIED", "rationale": MOCK + "claim not assessed by a model"}
            for c in s.ids("C")
        ]
    }


def _evaluator(s: Seen) -> dict[str, Any]:
    """Score = max x share of collected evidence that is OBSERVED/VERIFIED. A placeholder, not a judgement."""
    ev = _EVIDENCE.findall(s.text)
    good = [i for i, st in ev if st in ("OBSERVED", "VERIFIED")]
    share = len(good) / len(ev) if ev else 0.0
    return {
        "criteria": [
            {
                "criterion_id": cid,
                "score": round(mx * share),
                "rationale": MOCK + f"heuristic: {len(good)} of {len(ev)} evidence items observed; not a judgement",
                "evidence_ids": good[:4],
                "confidence": 0.1,
            }
            for cid, mx in s.criteria()
        ],
        "suggested_questions": [],
    }


def _copilot(s: Seen) -> str:
    srcs = re.findall(r"\[(S\d+)\] (\S+)", s.text)[:3]
    if not srcs:
        return MOCK + "no model available and no matching sources."
    return MOCK + "no model available. Most relevant sources: " + ", ".join(f"[{i}] {p}" for i, p in srcs)


OFFLINE: dict[str, Handler] = {
    "claim_analyst": claims_from_readme,
    "demo_analyst": _no_video,
    "claim_investigator": _investigator,
    "rubric_evaluator": _evaluator,
    "critic": lambda s: {"revisions": [], "notes": []},  # deterministic critic rules still apply
    "copilot": _copilot,
    **{f"{a}_specialist": _specialist for a in ("rag", "agentic", "ml", "generic")},
}


def offline_model() -> FakeLlm:
    return FakeLlm(model=OFFLINE_MODEL, handlers=OFFLINE, seen=[])


def configured(name: str) -> "str | FakeLlm":
    """The model agents should use: the named Gemini model only when LLM_MODE=vertex."""
    from judgebench.config import settings

    return name if settings().llm_mode == "vertex" else offline_model()


def is_mock(model: str | None) -> bool:
    return model == OFFLINE_MODEL
