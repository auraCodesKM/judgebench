"""The agents (§10.3). Which parts are agents and which are code:

code   profiler, adapters, CI reader, live-check planning, status rules, score clamps, critic rules
agent  claim_analyst       extracts claims from README + form            (cheap model, schema output)
agent  demo_analyst        bounded questions about the demo video          (cheap model, video input)
agent  <x>_specialist      interprets one adapter's findings               (cheap model)
agent  claim_investigator  ReAct: reason -> tool -> observe -> stop       (strong model, tools, budget)
agent  rubric_evaluator    criterion-level scores citing evidence IDs      (strong model)
agent  critic              checks scores against evidence; may only lower  (strong model)
agent  orchestrator        custom ADK BaseAgent: routes to specialists chosen by the profiler
                            (hierarchical delegation decided by code, not by the model), clamps
                            investigator verdicts, then hands off to the evaluator.
"""

import re
from collections import Counter
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.adk.models.base_llm import BaseLlm
from google.genai import types
from pydantic import ConfigDict

from judgebench.adapters.profiler import SKIP_DIRS
from judgebench.agents.runtime import PREAMBLE, Meter, callbacks, fence, model_name, parse_json, text_parts
from judgebench.agents.schemas import ClaimList, ClaimVerdicts, Critique, DemoReview, RubricResult, SpecialistAssessment
from judgebench.agents.views import ClaimView, EvidenceView, claims_block, clamp_verdict, evidence_block
from judgebench.config import settings

Model = str | BaseLlm
INVESTIGATOR_TOOL_BUDGET = settings().investigator_tool_budget  # 8; 6 for tight free quotas (demo takes)
VERDICT_CONFIG = types.GenerateContentConfig(temperature=0)  # claim verdicts: no sampling noise
VERDICT_RULES = (
    "Statuses: VERIFIED (a behavioural source - CI that ran real tests or a successful live check - AND matching "
    "code), PARTIALLY_VERIFIED (one of the two), UNVERIFIED (claimed, not shown), CONTRADICTED (evidence disagrees), "
    "MISSING_EVIDENCE (expected artefact absent), NOT_VERIFIABLE (needs credentials/hardware we lack). A demo alone "
    "never makes a claim VERIFIED. The status must agree with your rationale."
)
TEXT_SUFFIXES = {
    ".py",
    ".ipynb",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".md",
    ".txt",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".cfg",
    ".ini",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".sh",
    ".sql",
    ".html",
}
CODE_REF = re.compile(r"^(?P<path>[A-Za-z0-9_./-]{1,300})#L(?P<a>\d{1,6})(?:-L(?P<b>\d{1,6}))?$")

SPECIALIST_FOCUS = {
    "rag": "retrieval-augmented generation: chunking, embeddings, vector store, retrieval on the request path, "
    "reranking, grounding/citations, retrieval evaluation",
    "agentic": "agentic systems: agent definitions, tools, delegation between agents, state, limits and retries, "
    "whether 'multi-agent' is real or a single LLM call",
    "ml": "traditional ML: training code, data splits, leakage risk, baselines, metric choice, metrics on held-out data",
    "generic": "general engineering: entry points, tests, CI, documentation, deployment config",
}


def claim_analyst(model: Model, meter: Meter) -> LlmAgent:
    def inject(state: dict[str, Any]) -> list[types.Part]:
        return text_parts("Extract the team's claims from these materials.", state["readme_block"], state["form_block"])

    return LlmAgent(
        name="claim_analyst",
        model=model,
        include_contents="none",
        output_schema=ClaimList,
        output_key="claims",
        instruction=PREAMBLE + "Extract up to 15 concrete, checkable claims the team makes about their project "
        "(accuracy, performance, scale, testing, architecture, features, data). Quote or tightly paraphrase. "
        "Do not judge them. Skip marketing language that makes no checkable claim.",
        **callbacks(meter, model_name(model), inject),
    )


def demo_analyst(model: Model, meter: Meter, video_url: str, end_offset_s: int, fps: float) -> LlmAgent:
    def inject(state: dict[str, Any]) -> list[types.Part]:
        video = types.Part(
            file_data=types.FileData(file_uri=video_url, mime_type="video/*"),
            video_metadata=types.VideoMetadata(start_offset="0s", end_offset=f"{end_offset_s}s", fps=fps),
        )
        return [
            video,
            *text_parts(
                "For each claim below answer only: does the demo show it working (and at what mm:ss)? Does anything "
                "in the demo contradict it? If the video cannot be watched, set available=false.",
                state["claims_block"],
            ),
        ]

    return LlmAgent(
        name="demo_analyst",
        model=model,
        include_contents="none",
        output_schema=DemoReview,
        output_key="demo",
        instruction=PREAMBLE + "You review a hackathon demo video against the team's own claims. Answer only the "
        "bounded questions asked. A demo can show a feature working; it does not prove reliability.",
        **callbacks(meter, model_name(model), inject),
    )


def specialist(adapter: str, model: Model, meter: Meter) -> LlmAgent:
    prefixes = (f"{adapter}.", "generic.") if adapter != "generic" else ("generic.", "ci.", "live.")

    def inject(state: dict[str, Any]) -> list[types.Part]:
        ev: list[EvidenceView] = state["_evidence"]
        return text_parts(
            f"Assess the {adapter} aspects of this submission.", state["profile_block"], evidence_block(ev, prefixes)
        )

    return LlmAgent(
        name=f"{adapter}_specialist",
        model=model,
        include_contents="none",
        output_schema=SpecialistAssessment,
        output_key=f"assessment_{adapter}",
        instruction=PREAMBLE + f"You are a reviewer specialised in {SPECIALIST_FOCUS[adapter]}. Using only the "
        "evidence given, summarise what is actually implemented, its strengths and its gaps. Code that exists but "
        "is not on a path that runs is not an implementation. Cite evidence IDs.",
        **callbacks(meter, model_name(model), inject),
    )


def investigation_tools(root: Path, evidence: list[EvidenceView]) -> list[Any]:
    """Tools bound to ONE submission's snapshot and evidence. No IDs in, so no scope out."""
    root = root.resolve()

    def files() -> list[Path]:
        out = []
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root)
            if (
                p.is_file()
                and not p.is_symlink()
                and p.suffix in TEXT_SUFFIXES
                and p.stat().st_size < 1_000_000
                and not SKIP_DIRS.intersection(rel.parts[:-1])
            ):
                out.append(p)
            if len(out) >= 3000:
                break
        return out

    def search_code(text: str) -> dict[str, Any]:
        """Case-insensitive literal search across this submission's source files.
        Returns up to 20 hits as 'path:line: snippet'."""
        needle, hits = text.strip().lower()[:100], []
        if len(needle) < 2:
            return {"error": "search text too short"}
        for p in files():
            for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
                if needle in line.lower():
                    hits.append(f"{p.relative_to(root).as_posix()}:{n}: {line.strip()[:160]}")
                    if len(hits) >= 20:
                        return {"result": fence("code_search", "\n".join(hits))}
        return {"result": fence("code_search", "\n".join(hits) or "(no matches)")}

    def read_file(path: str, start_line: int, end_line: int) -> dict[str, Any]:
        """Read lines start_line..end_line (inclusive, at most 120 lines) of one file in this submission."""
        target = (root / path).resolve()
        if not target.is_relative_to(root) or not target.is_file() or target.is_symlink():
            return {"error": "no such file in this submission"}
        lines = target.read_text(errors="replace").splitlines()
        a, b = max(1, start_line), min(len(lines), end_line, max(1, start_line) + 119)
        body = "\n".join(f"{i}: {lines[i - 1]}" for i in range(a, b + 1))
        return {"result": fence(f"file:{path}", body or "(empty range)")}

    def list_evidence(source_type: str) -> dict[str, Any]:
        """List evidence records for this submission. source_type: code, ci, live_check, demo, documentation,
        form, or empty for all."""
        rows = [e for e in evidence if not source_type or e.source_type == source_type]
        return {"result": evidence_block(rows)}

    return [search_code, read_file, list_evidence]


def claim_investigator(model: Model, meter: Meter, tools: list[Any]) -> LlmAgent:
    meter.max_tool_calls["claim_investigator"] = INVESTIGATOR_TOOL_BUDGET

    def inject(state: dict[str, Any]) -> list[types.Part]:
        return text_parts("Investigate these claims.", state["claims_block"], evidence_block(state["_evidence"]))

    return LlmAgent(
        name="claim_investigator",
        model=model,
        include_contents="none",
        tools=tools,
        output_key="verdicts_raw",
        generate_content_config=VERDICT_CONFIG,
        instruction=PREAMBLE + "Investigate each claim with a reason-act-observe loop: decide what would prove or "
        f"disprove it, call a tool to look, read the observation, and repeat. You have at most "
        f"{INVESTIGATOR_TOOL_BUDGET} tool calls in total; request every lookup you already know you need in the "
        "same turn (they run together), and stop as soon as every claim can be classified. "
        + VERDICT_RULES
        + ' Final answer: only JSON {"verdicts": [{"claim_id": "C1", "status": "...", '
        '"evidence_ids": ["E3"], "code_refs": ["path#L10-L20"], "rationale": "..."}]}.',
        **callbacks(meter, model_name(model), inject),
    )


def claim_judge(model: Model, meter: Meter, n: int) -> LlmAgent:
    """Votes 2..N of the claim verdict (self-consistency): the same claims and evidence plus the
    investigator's own tool observations, no tools of its own, temperature 0."""

    def inject(state: dict[str, Any]) -> list[types.Part]:
        seen = "\n".join(
            f"{t['tool']}({', '.join(f'{k}={v}' for k, v in t['args'].items())}) -> {t.get('observation', '')}"
            for t in meter.tools.get("claim_investigator", [])
        )
        return text_parts(
            "Classify each claim.",
            claims_block([c for c in state["_claims"] if c.id in set(state.get("vote_claims", []))])
            if state.get("vote_claims")
            else state["claims_block"],
            evidence_block(state["_evidence"]),
            fence("investigator_observations", seen or "(none)"),
        )

    return LlmAgent(
        name=f"claim_judge_{n}",
        model=model,
        include_contents="none",
        output_key=f"verdicts_raw_{n}",
        generate_content_config=VERDICT_CONFIG,
        instruction=PREAMBLE + VERDICT_RULES + " Decide from the evidence and the observations given; you have no "
        'tools. Final answer: only JSON {"verdicts": [{"claim_id": "C1", "status": "...", "evidence_ids": ["E3"], '
        '"code_refs": ["path#L10-L20"], "rationale": "..."}]}.',
        **callbacks(meter, model_name(model), inject),
    )


def rubric_evaluator(model: Model, meter: Meter) -> LlmAgent:
    def inject(state: dict[str, Any]) -> list[types.Part]:
        assessments = "\n\n".join(
            f"{k.removeprefix('assessment_')}: {v}" for k, v in state.items() if k.startswith("assessment_")
        )
        return text_parts(
            state["rubric_block"],
            evidence_block(state["_evidence"]),
            state["verified_claims_block"],
            fence("specialist_assessments", assessments or "(none)"),
        )

    return LlmAgent(
        name="rubric_evaluator",
        model=model,
        include_contents="none",
        output_schema=RubricResult,
        output_key="rubric_result",
        instruction=PREAMBLE + "Score every rubric criterion exactly once. Each score must be justified by cited "
        "evidence IDs; if evidence is missing say what is missing and score conservatively. A failed or impossible "
        "check (NOT_VERIFIABLE, CHECK_FAILED) is not by itself a reason to lower a score. Each rationale is one plain "
        "sentence a judge can read in five seconds. Give up to 3 suggested judge questions, each specific to this "
        "team: quote the claim or the finding that prompted it, use only numbers that appear in the evidence, and "
        'cite its evidence ID (shape: "Your README claims <claim>; <check> found <what the evidence says>. <question>?"). '
        "Never generic questions.",
        **callbacks(meter, model_name(model), inject),
    )


def critic(model: Model, meter: Meter) -> LlmAgent:
    def inject(state: dict[str, Any]) -> list[types.Part]:
        return text_parts(
            state["rubric_block"], evidence_block(state["_evidence"]), fence("scores", state["scores_block"])
        )

    return LlmAgent(
        name="critic",
        model=model,
        include_contents="none",
        output_schema=Critique,
        output_key="critique",
        instruction=PREAMBLE + "You review proposed criterion scores. For each, check the cited evidence actually "
        "supports the score and rationale. Flag unsupported conclusions, overlap between criteria, and claims treated "
        "as verified that are not. You may only LOWER a score or flag it for judges; you can never raise one.",
        **callbacks(meter, model_name(model), inject),
    )


def copilot(model: Model, meter: Meter) -> LlmAgent:
    def inject(state: dict[str, Any]) -> list[types.Part]:
        return text_parts(
            "Answer the judge's question using only these excerpts.", state["question_block"], state["excerpts_block"]
        )

    return LlmAgent(
        name="copilot",
        model=model,
        include_contents="none",
        output_key="answer",
        instruction=PREAMBLE.replace("(E1, E2, ...)", "(S1, S2, ...)") + "You help a hackathon judge question one "
        "team's submission. Answer briefly and concretely from the excerpts only, citing them as [S1], [S2]. Say "
        "what the code shows, not what the README claims, and say plainly when the excerpts do not answer the question. "
        "Never give a score or a verdict on who should win.",
        **callbacks(meter, model_name(model), inject),
    )


class Orchestrator(BaseAgent):
    """Hierarchical delegation. Routing comes from session.state['adapters'] (set by code from the
    profiler), so participant text can never add or remove a specialist."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        selected = set(ctx.session.state.get("adapters", []))
        by_name = {a.name: a for a in self.sub_agents}
        specific: list[str] = [a for a in ("rag", "agentic", "ml") if a in selected]
        for name in [f"{a}_specialist" for a in specific or ["generic"]]:  # generic only when nothing specific
            if name in by_name:
                async for event in by_name[name].run_async(ctx):
                    yield event
        async for event in by_name["claim_investigator"].run_async(ctx):
            yield event
        judges = sorted(n for n in by_name if n.startswith("claim_judge_"))
        contested = contested_claims(ctx.session.state) if judges else []
        if contested:  # only claims whose evidence disagrees get the extra votes; the rest need one call
            yield Event(
                author=self.name,
                invocation_id=ctx.invocation_id,
                actions=EventActions(state_delta={"vote_claims": contested}),
            )
            for judge in judges:
                async for event in by_name[judge].run_async(ctx):
                    yield event
        verdicts = clamped_verdicts(ctx.session.state)
        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            actions=EventActions(
                state_delta={"verdicts": verdicts, "verified_claims_block": verdicts_block(ctx.session.state, verdicts)}
            ),
        )
        async for event in by_name["rubric_evaluator"].run_async(ctx):
            yield event


_SUPPORT, _AGAINST = {"OBSERVED", "VERIFIED"}, {"MISSING", "CONTRADICTED"}


def contested_claims(state: Any) -> list[str]:
    """Claims worth more votes: the investigator called them CONTRADICTED, or the evidence it cited
    points both ways. Unanimous evidence needs no second opinion."""
    evidence = {e.id: e for e in state["_evidence"]}
    out = []
    for cid, v in _clamped_vote(state, "verdicts_raw").items():
        statuses = {evidence[e].status for e in v["evidence_ids"] if e in evidence}
        if v["status"] == "CONTRADICTED" or (statuses & _SUPPORT and statuses & _AGAINST):
            out.append(cid)
    return out


def _clamped_vote(state: Any, key: str) -> dict[str, dict[str, Any]]:
    claims: list[ClaimView] = state["_claims"]
    evidence = {e.id: e for e in state["_evidence"]}
    root: Path = state["_root"]
    try:
        raw = ClaimVerdicts.model_validate(parse_json(state.get(key, "")))
    except ValueError:
        raw = ClaimVerdicts(verdicts=[])
    known = {c.id for c in claims}
    out: dict[str, dict[str, Any]] = {}
    for v in raw.verdicts:
        if v.claim_id not in known or v.claim_id in out:
            continue
        refs = [r for r in v.code_refs if valid_code_ref(root, r)][:5]
        clamped = clamp_verdict(v, evidence, bool(refs))
        out[v.claim_id] = clamped.model_dump() | {"code_refs": refs}
    return out


def clamped_verdicts(state: Any) -> list[dict[str, Any]]:
    """Each vote is clamped by the §7.5 rules on its own; the majority status wins (no majority:
    UNVERIFIED). With one vote (offline / tests) this is the plain clamped verdict. The split is kept
    on the verdict ("CONTRADICTED 3/3") so judges see how sure the panel was."""
    claims: list[ClaimView] = state["_claims"]
    data = state.to_dict() if hasattr(state, "to_dict") else dict(state)
    judge_keys = sorted(k for k in data if k.startswith("verdicts_raw_"))
    contested = set(data.get("vote_claims", [])) if "vote_claims" in data else None  # None: all were voted on
    by_key = {k: _clamped_vote(state, k) for k in ["verdicts_raw", *judge_keys]}
    out = []
    for c in claims:
        voted = judge_keys if contested is None or c.id in contested else []
        votes = [by_key["verdicts_raw"], *(by_key[k] for k in voted)]
        ballots = [v[c.id] for v in votes if c.id in v]
        statuses = [b["status"] for b in ballots] + ["UNVERIFIED"] * (len(votes) - len(ballots))
        split = Counter(statuses)
        top, n = split.most_common(1)[0]
        majority = len(votes) == 1 or n * 2 > len(votes)
        if majority:
            pick = next((b for b in ballots if b["status"] == top), None)
            others = ", ".join(f"{st} {k}" for st, k in split.most_common() if st != top)
            votes_txt = f"{top} {n}/{len(votes)}" + (f" ({others})" if others else "")
        else:
            top, pick = "UNVERIFIED", None
            votes_txt = "no majority (" + ", ".join(f"{st} {k}" for st, k in split.most_common()) + ")"
        pick = pick or {
            "claim_id": c.id,
            "status": top,
            "evidence_ids": [],
            "code_refs": [],
            "rationale": "No majority among the votes." if ballots else "Not investigated within the tool budget.",
        }
        out.append(latency_rule(c, pick | {"status": top, "votes": votes_txt if len(votes) > 1 else None}, state))
    return out


SLOW_MS = 3000  # a "real-time" / "instant" claim against a measured answer slower than this is contradicted
_SPEED_CLAIM = re.compile(
    r"real[- ]?time|instant|low[- ]latency|sub[- ]?second|under \d+ ?(ms|milliseconds|seconds?)", re.I
)
_LATENCY = re.compile(r"-> (\d{3}) in (\d+) ms")


def latency_rule(claim: ClaimView, verdict: dict[str, Any], state: Any) -> dict[str, Any]:
    """Deterministic, like the §7.5 clamps: a speed claim plus a successful live sample query measured
    slower than SLOW_MS is CONTRADICTED, citing that measurement, whatever the model voted."""
    if not _SPEED_CLAIM.search(claim.text):
        return verdict
    for e in state["_evidence"]:
        m = _LATENCY.search(e.observation) if e.key == "live.sample_query" else None
        if m and m.group(1).startswith("2") and int(m.group(2)) > SLOW_MS:
            secs = int(m.group(2)) / 1000
            note = f"Rule: the live check measured {secs:.1f} s for {e.locator.removeprefix('live:')}, not real-time."
            return verdict | {
                "status": "CONTRADICTED",
                "evidence_ids": list(dict.fromkeys([*verdict.get("evidence_ids", []), e.id])),
                "rationale": note
                if verdict["status"] == "CONTRADICTED"
                else f"{note} (model said {verdict['status']}.)",
            }
    return verdict


def verdicts_block(state: Any, verdicts: list[dict[str, Any]]) -> str:
    text = {c.id: c.text for c in state["_claims"]}
    return fence(
        "claim_verdicts",
        "\n".join(
            f"{v['claim_id']} {v['status']} (evidence {', '.join(v['evidence_ids']) or 'none'}): "
            f"{text.get(v['claim_id'], '')} -- {v['rationale']}"
            for v in verdicts
        )
        or "(no claims)",
    )


def valid_code_ref(root: Path, ref: str) -> bool:
    m = CODE_REF.match(ref)
    if not m:
        return False
    target = (root / m["path"]).resolve()
    if not target.is_relative_to(root.resolve()) or not target.is_file():
        return False
    n = len(target.read_text(errors="replace").splitlines())
    a, b = int(m["a"]), int(m["b"] or m["a"])
    return 1 <= a <= b <= n


def orchestrator(
    adapters: list[str], cheap: Model, strong: Model, meter: Meter, tools: list[Any], votes: int = 1
) -> Orchestrator:
    specific: list[str] = [a for a in ("rag", "agentic", "ml") if a in adapters]
    specialists = [specialist(a, cheap, meter) for a in specific or ["generic"]]
    return Orchestrator(
        name="orchestrator",
        sub_agents=[
            *specialists,
            claim_investigator(strong, meter, tools),
            *[claim_judge(strong, meter, n) for n in range(2, votes + 1)],
            rubric_evaluator(strong, meter),
        ],
    )
