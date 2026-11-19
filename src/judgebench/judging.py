"""What a judge reads first, built deterministically from stored evidence (no model, $0):
readable finding names, the team's biggest concern, and grounded questions to ask.

Works on DB rows (Evidence, Claim, CriterionScore) so the Sheet, the Telegram summary card and the
offline evaluator all say the same thing. Every question names the check or file that prompted it.
"""

import re
from typing import Any
from urllib.parse import urlsplit

NAMES = {
    "ci.tests_passed": "CI tests passed",
    "ci.failed": "CI failed",
    "ci.green_without_tests": "CI passes without tests",
    "ci.workflow_without_tests": "CI workflow runs no tests",
    "ci.configured": "CI configured",
    "ci.runs": "CI runs",
    "live.health": "Live app reachable",
    "live.sample_query": "Live sample request",
    "live.unreachable": "Live app unreachable",
    "live.blocked": "Live check blocked",
    "live.deployed_app": "Deployed app",
    "demo.unavailable": "Demo video",
    "demo.claim": "Claim shown in demo",
    "rag.retrieval_on_request_path": "Retrieval wired into requests",
    "rag.retrieval_evaluation": "Retrieval evaluation",
    "rag.vector_store": "Vector store",
    "rag.embedding": "Embeddings",
    "rag.chunking": "Chunking",
    "rag.reranking": "Re-ranking",
    "agentic.delegation": "Agent delegation",
    "agentic.agent_definitions": "Agents defined",
    "agentic.tools": "Agent tools",
    "agentic.state": "Agent state",
    "agentic.limits": "Agent limits",
    "ml.training": "Model training",
    "ml.data_split": "Train/test split",
    "ml.metrics": "Metrics",
    "ml.metrics_on_holdout": "Metrics on held-out data",
    "ml.baseline": "Baseline",
    "generic.tests": "Tests",
    "generic.readme": "README",
    "generic.entry_points": "Entry points",
    "generic.api_routes": "API routes",
    "generic.ci_config": "CI configuration",
    "generic.container": "Container / deploy config",
    "generic.lock_file": "Dependency lock file",
    "investigation.code_ref": "Code the investigator cited",
    "investigation.verdict_votes": "Verdict votes",
}
_SPEED = re.compile(r"real[- ]?time|instant|low[- ]latency|sub[- ]?second", re.I)
_MS = re.compile(r"-> (\d{3}) in (\d+) ms")
_PREFIX = re.compile(r"^(?:\[[^\]]*\]\s*|Rule:\s*)+")


def clip(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def sentence(text: str, n: int = 180) -> str:
    """First sentence, without vote/template prefixes."""
    t = _PREFIX.sub("", " ".join((text or "").split()))
    first = re.split(r"(?<=[.!?])\s", t, maxsplit=1)[0]
    return clip(first, n)


def finding_name(e: Any) -> str:
    return clip(e.claim, 120) if e.claim else NAMES.get(e.key, e.key.split(".", 1)[-1].replace("_", " ").capitalize())


def _where(e: Any) -> str:
    loc = e.source_locator or ""
    return loc[5:] if loc.startswith("live:") else ("demo video" if loc.startswith(("demo", "form:demo")) else loc)


def top_concern(claims: list[Any], evidence: list[Any], scores: list[Any], names: dict[str, str]) -> str:
    """One line from the strongest negative evidence, most serious first."""
    by = {e.key: e for e in evidence}
    if c := next((c for c in claims if c.verification_status == "CONTRADICTED"), None):
        return clip(f'Claim contradicted: "{clip(c.text, 70)}". {sentence(c.rationale or "", 120)}', 220)
    for key in ("live.sample_query", "live.health"):
        if (e := by.get(key)) and e.status == "CONTRADICTED":
            return clip(f"Live app failed: {e.observation}", 200)
    if e := by.get("ci.failed"):
        return clip(e.observation.removeprefix("GitHub Actions "), 200)
    if e := by.get("ci.green_without_tests"):
        return clip(e.observation.removeprefix("GitHub Actions ").replace(" but ", ", but "), 200)
    if s := next((s for s in scores if s.flagged), None):
        return clip(
            f"{names.get(s.criterion_id, s.criterion_id)}: {sentence(s.critic_note or 'flagged by the critic')}", 200
        )
    for key in ("rag.retrieval_evaluation", "ml.metrics_on_holdout", "generic.tests", "rag.retrieval_on_request_path"):
        if (e := by.get(key)) and e.status in ("MISSING", "INFERRED"):
            return clip(e.observation, 200)
    if weakest := min(scores, key=lambda s: s.score / s.max_score if s.max_score else 1, default=None):
        name = names.get(weakest.criterion_id, weakest.criterion_id)
        return clip(f"Weakest: {name} {weakest.score:g}/{weakest.max_score:g}. {sentence(weakest.rationale, 140)}", 200)
    return ""


def template_questions(claims: list[Any], evidence: list[Any], deployed_url: str | None) -> list[tuple[str, str]]:
    """(question, what prompted it), most important first. Specific to this team's evidence."""
    by = {e.key: e for e in evidence}
    out: list[tuple[str, str]] = []
    sample = by.get("live.sample_query")
    m = _MS.search(sample.observation) if sample else None
    for c in claims:
        text = clip(c.text, 80)
        if c.verification_status == "CONTRADICTED" and _SPEED.search(c.text) and m and sample:
            out.append(
                (
                    f'Your README claims "{text}"; the live check measured {int(m.group(2)) / 1000:.1f} s for '
                    f"{_where(sample)}. What is your latency target, and where does the time go?",
                    f"live check {_where(sample)}",
                )
            )
        elif c.verification_status == "CONTRADICTED":
            out.append(
                (
                    f'You claim "{text}", but {sentence(c.rationale or "", 120).rstrip(".")}. Can you show us?',
                    "claim check",
                )
            )
        elif "%" in c.text and c.verification_status in ("UNVERIFIED", "MISSING_EVIDENCE"):
            out.append((f'Where does "{text}" come from? We found no script that computes it.', "README + code search"))
    if e := by.get("ci.green_without_tests"):
        out.append(
            (
                f"Your CI passes but runs no test command ({_where(e).removeprefix('ci:')}). How do you know the code works?",
                "CI workflow",
            )
        )
    if (e := by.get("rag.retrieval_on_request_path")) and e.status in ("INFERRED", "MISSING"):
        out.append((f"{sentence(e.observation, 120)} Where is retrieval called when a question comes in?", _where(e)))
    if (e := by.get("rag.retrieval_evaluation")) and e.status == "MISSING":
        out.append(("How did you measure retrieval quality? We found no evaluation code.", "code search"))
    if (e := by.get("ml.metrics_on_holdout")) and e.status == "MISSING":
        out.append(
            ("How was the model evaluated on held-out data? We found no metrics on a test split.", "code search")
        )
    if (e := by.get("agentic.delegation")) and e.status == "MISSING":
        out.append(("We found agents but no delegation between them. Which agent hands work to which?", "code search"))
    if by.get("live.unreachable") and deployed_url:
        host = urlsplit(deployed_url).hostname or deployed_url
        out.append((f"Your app at {host} did not respond during our check. Can you demo it live?", "live check"))
    if (e := by.get("generic.tests")) and e.status == "MISSING":
        out.append(("We found no test functions. What would you test first, and why?", "repository"))
    if by.get("demo.unavailable"):
        out.append(("No demo video was linked. Can you walk us through the main flow live?", "submission form"))
    seen, unique = set(), []
    for q, src in out:
        if q not in seen:
            seen.add(q)
            unique.append((q, src))
    return unique


def render_question(q: str, source: str, template: bool) -> str:
    return f"{q} (from {source}{'; template' if template else ''})"
