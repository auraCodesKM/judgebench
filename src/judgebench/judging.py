"""What a judge reads first, built deterministically from stored evidence (no model, $0):
readable finding names, the team's biggest concern, and grounded questions to ask.

Works on DB rows (Evidence, Claim, CriterionScore) so the Sheet, the Telegram summary card and the
offline evaluator all say the same thing. Every question names the check or file that prompted it.
"""

import re
from typing import Any

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


_IDS = re.compile(
    r"\s*[(\[](?:evidence:?\s*)?(?:[ECS]\d+(?:,\s*|\s+and\s+)?)+[)\]]"  # (E1, E5) [E7] (Evidence: E1)
    r"|(?<=[Ee]vidence)\s+[EC]\d+(?:,\s*[EC]\d+)*"  # "Evidence E15 shows" -> "Evidence shows"
    r"|\b(?:claim )?[EC]\d+\b(?: claims?)?\s*",
    re.I,
)


_KEY = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in sorted(NAMES, key=len, reverse=True)) + r")\b"
)  # exact keys only
_ARROW = re.compile(r"-> (\d{3}) in (\d+) ms(?: \([^)]*\))?")


def dejargon(text: str) -> str:
    """Drop internal references a judge can't use: "(E9)", "(E1, E4)", "C1 claims ...", evidence keys
    ("live.sample_query" -> "the live sample request"), raw check lines ("-> 200 in 8554 ms" -> "answered in 8.6 s")."""
    t = _KEY.sub(
        lambda m: "the " + NAMES.get(m.group(0), m.group(0).split(".")[-1].replace("_", " ")).lower(), text or ""
    )
    t = _ARROW.sub(lambda m: f"answered in {int(m.group(2)) / 1000:.1f} s", t)
    t = _IDS.sub(" ", t)
    t = re.sub(r"([.!?])([;,])", r"\2", t)  # "second).;" -> "second);"
    return re.sub(r"\s+([.,;:])(?=\s|$)", r"\1", " ".join(t.split()))  # "x ." -> "x.", never ".github"


def clip(text: str, n: int) -> str:
    text = dejargon(text)
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def sentence(text: str, n: int = 180) -> str:
    """First sentence, without vote/template prefixes."""
    t = dejargon(_PREFIX.sub("", " ".join((text or "").split())))  # strip IDs before capitalising
    first = re.split(r"(?<=[.!?])\s", t, maxsplit=1)[0]
    return clip(first[:1].upper() + first[1:], n)


def finding_name(e: Any) -> str:
    return clip(e.claim, 120) if e.claim else NAMES.get(e.key, e.key.split(".", 1)[-1].replace("_", " ").capitalize())


def _where(e: Any) -> str:
    loc = e.source_locator or ""
    return loc[5:] if loc.startswith("live:") else ("demo video" if loc.startswith(("demo", "form:demo")) else loc)


STATUS_LABEL = {
    "VERIFIED": "Confirmed",
    "OBSERVED": "Partly confirmed",
    "INFERRED": "Not checkable",
    "MISSING": "Missing",
    "CONTRADICTED": "Contradicted",
}


def words(text: str, n: int = 20) -> str:
    """At most n words; a trailing "…" if cut."""
    w = (text or "").split()
    return " ".join(w) if len(w) <= n else " ".join(w[:n]).rstrip(".,;:") + "…"


def lower_first(text: str) -> str:
    return text[:1].lower() + text[1:] if text[:2] != text[:2].upper() else text


def plain_claim(text: str, n: int = 8) -> str:
    """A claim as a judge would say it: short, no markdown, no trailing full stop."""
    t = re.sub(r"\s*\([^)]*\)?", "", re.sub(r"[*_`#]", "", text or "")).strip().rstrip(".:;")
    return words(t, n)


def contradiction(claim: Any, team: str | None = None) -> str:
    """'they say "answers in real time", but their app took 8.6 s when I tried it.'"""
    why = sentence(claim.rationale or "", 160).rstrip(".")
    if " but " in f" {why.lower()} ":  # the reason already turns on "but": its own sentence reads better
        return f'they say "{plain_claim(claim.text)}". {why}.'
    return f'they say "{plain_claim(claim.text)}", but {lower_first(why)}.'


def contradicted(claims: list[Any]) -> list[Any]:
    """Contradicted claims, one per distinct reason, best first: measured (rule) findings, then the
    shortest claim (it quotes cleanest)."""
    out: dict[str, Any] = {}
    ranked = sorted(
        (c for c in claims if c.verification_status == "CONTRADICTED"),
        key=lambda c: (not str(c.rationale or "").lstrip("[").split("] ")[-1].startswith("Rule:"), len(c.text)),
    )
    for c in ranked:
        out.setdefault(sentence(c.rationale or "", 160).lower(), c)
    return list(out.values())


def top_concern(claims: list[Any], evidence: list[Any], scores: list[Any], names: dict[str, str]) -> str:
    """The one thing that stood out, from the strongest negative evidence. At most 20 words."""
    by = {e.key: e for e in evidence}
    if found := contradicted(claims):
        text = contradiction(found[0])
        if len(text.split()) > 20:  # too long with the quote: just say what was found
            text = sentence(found[0].rationale or "", 160)
        return words(text[:1].upper() + text[1:])
    for key in ("live.sample_query", "live.health"):
        if (e := by.get(key)) and e.status == "CONTRADICTED":
            return words(f"Their live app gave the wrong answer when I tried it: {sentence(e.observation, 80)}")
    if by.get("ci.failed"):
        return "Their CI is failing on the submitted commit."
    if by.get("ci.green_without_tests"):
        return "CI is green, but it doesn't run any tests."
    if s := next((s for s in scores if s.flagged), None):
        return words(
            f"{names.get(s.criterion_id, s.criterion_id)}: {sentence(s.critic_note or 'score not backed by evidence')}"
        )
    if (e := by.get("rag.retrieval_evaluation")) and e.status == "MISSING":
        return "No sign they measured how well their retrieval works."
    if (e := by.get("ml.metrics_on_holdout")) and e.status == "MISSING":
        return "No metrics on held-out data, so the accuracy is hard to trust."
    if (e := by.get("generic.tests")) and e.status == "MISSING":
        return "There are no tests in the repo."
    if (e := by.get("rag.retrieval_on_request_path")) and e.status in ("MISSING", "INFERRED"):
        return "The retriever exists, but nothing calls it when a question comes in."
    if weakest := min(scores, key=lambda s: s.score / s.max_score if s.max_score else 1, default=None):
        name = names.get(weakest.criterion_id, weakest.criterion_id)
        return words(f"Weakest area: {name.lower()} ({weakest.score:g}/{weakest.max_score:g}).")
    return ""


def template_questions(claims: list[Any], evidence: list[Any], deployed_url: str | None) -> list[tuple[str, str]]:
    """(question, what prompted it), most important first. Plain, specific, at most 20 words."""
    by = {e.key: e for e in evidence}
    out: list[tuple[str, str]] = []
    sample = by.get("live.sample_query")
    m = _MS.search(sample.observation) if sample else None
    first = contradicted(claims)  # the same order as the alerts and the biggest concern
    rest = sorted((c for c in claims if c not in first), key=lambda c: len(c.text))
    for c in [*first, *rest]:
        pct = re.search(r"\d+(?:\.\d+)?%\s*\w+", c.text)
        claim = pct.group(0) if pct else plain_claim(c.text)
        if c.verification_status == "CONTRADICTED" and _SPEED.search(c.text) and m and sample:
            secs = int(m.group(2)) / 1000
            out.append(
                (
                    f'You say "{claim}", yet it took {secs:.1f} s for me. What\'s your target?',
                    "the live check",
                )
            )
        elif c.verification_status == "CONTRADICTED" and re.search(r"test", c.text, re.I):
            out.append((f'You say "{claim}", but I found no tests. What did you actually test?', "the repo"))
        elif "%" in c.text and c.verification_status in ("UNVERIFIED", "MISSING_EVIDENCE", "CONTRADICTED"):
            out.append((f'Where does "{claim}" come from? I couldn\'t find anything that measures it.', "the repo"))
    if by.get("ci.green_without_tests"):
        out.append(("Your CI is green but runs no tests. What would catch a bug before a demo?", "the CI workflow"))
    if (e := by.get("rag.retrieval_on_request_path")) and e.status in ("INFERRED", "MISSING"):
        out.append(
            ("Your retriever exists, but nothing calls it. How does a question actually get answered?", "the code")
        )
    if (e := by.get("rag.retrieval_evaluation")) and e.status == "MISSING":
        out.append(("How do you know retrieval finds the right passages?", "the repo"))
    if (e := by.get("ml.metrics_on_holdout")) and e.status == "MISSING":
        out.append(("How did the model do on data it never saw in training?", "the repo"))
    if (e := by.get("agentic.delegation")) and e.status == "MISSING":
        out.append(("Which agent hands work to which, and how?", "the code"))
    if by.get("live.unreachable") and deployed_url:
        out.append(("Your app didn't respond when I tried it. Can you show it running?", "the live check"))
    if (e := by.get("generic.tests")) and e.status == "MISSING":
        out.append(("There are no tests. What would you test first?", "the repo"))
    if by.get("demo.unavailable"):
        out.append(("No demo video. Can you walk us through the main flow?", "the submission form"))
    seen, unique = set(), []
    for q, src in out:
        if q not in seen:
            seen.add(q)
            unique.append((q, src))
    return unique


def question_text(rendered: str) -> str:
    """A stored question without its "(from …)" source note: what the Leaderboard and card show."""
    return re.sub(r"\s*\(from [^)]*\)\s*$", "", rendered or "").strip()


def friendly_model(label: str | None) -> str:
    """ "gemini-3.1-flash-lite (Gemini API free tier)" -> "gemini-3.1-flash-lite": the exact model, as on the Sheet."""
    name = (label or "").split(" (")[0]
    return "no AI model (offline placeholder scores)" if name == "offline-mock" else name or "an unknown model"


def render_question(q: str, source: str, template: bool) -> str:
    return f"{dejargon(q)} (from {source}{'; template' if template else ''})"
