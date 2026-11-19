"""Biggest concern and template questions come from this team's evidence, specific and sourced."""

from types import SimpleNamespace as N

from judgebench.judging import render_question, sentence, template_questions, top_concern


def e(key: str, status: str, obs: str, loc: str = "repository", claim: str | None = None) -> N:
    return N(key=key, status=status, observation=obs, source_locator=loc, claim=claim)


def c(text: str, status: str, rationale: str = "") -> N:
    return N(text=text, verification_status=status, rationale=rationale)


BETA_EV = [
    e(
        "live.sample_query",
        "VERIFIED",
        "POST /ask -> 200 in 8528 ms (application/json); route found in code",
        "live:POST /ask",
    ),
    e("rag.retrieval_evaluation", "MISSING", "No retrieval evaluation found"),
    e(
        "rag.retrieval_on_request_path",
        "INFERRED",
        "Retriever defined in retriever.py but never called from a handler.",
        "retriever.py#L3",
    ),
]
BETA_CLAIMS = [
    c(
        "Our RAG system answers in real time",
        "CONTRADICTED",
        "[CONTRADICTED 3/3] Rule: the live check measured 8.5 s for POST /ask, not real-time.",
    ),
    c("98% accuracy", "UNVERIFIED", "[UNVERIFIED 3/3] No evaluation code."),
]


def test_beta_questions_are_specific_and_sourced() -> None:
    qs = template_questions(BETA_CLAIMS, BETA_EV, "https://beta.vercel.app")
    assert qs[0] == (
        'Your README claims "Our RAG system answers in real time"; the live check measured 8.5 s for POST /ask. '
        "What is your latency target, and where does the time go?",
        "live check POST /ask",
    )
    assert qs[1][0] == 'Where does "98% accuracy" come from? We found no script that computes it.'
    assert any("Where is retrieval called" in q for q, _ in qs)
    assert render_question(*qs[0], template=True).endswith("(from live check POST /ask; template)")


def test_concern_prefers_the_strongest_negative_evidence() -> None:
    concern = top_concern(BETA_CLAIMS, BETA_EV, [], {})
    assert concern.startswith('Claim contradicted: "Our RAG system answers in real time".') and "8.5 s" in concern
    zeta = [
        e(
            "ci.green_without_tests",
            "OBSERVED",
            'GitHub Actions "ci" passed on c8352c3 but the workflow runs no tests',
            "ci:run/9",
        )
    ]
    assert top_concern([], zeta, [], {}) == '"ci" passed on c8352c3, but the workflow runs no tests'
    assert template_questions([], zeta, None)[0][0].startswith("Your CI passes but runs no test command")


def test_sentence_strips_vote_and_rule_prefixes() -> None:
    assert sentence("[CONTRADICTED 3/3] Rule: measured 8.5 s. More detail here.") == "Measured 8.5 s."


def test_internal_ids_never_reach_judges() -> None:
    from judgebench.judging import dejargon

    assert dejargon("No test functions were found (E9).") == "No test functions were found."
    assert dejargon("CI is missing (E1, E4) and tests too.") == "CI is missing and tests too."
    assert dejargon("C1 claims real time, but the app is slow.") == "real time, but the app is slow."
    assert (
        sentence("[UNVERIFIED 3/3] C3 claims 98% accuracy, but no eval exists (E2).")
        == "98% accuracy, but no eval exists."
    )


def test_more_id_forms_are_removed() -> None:
    from judgebench.judging import dejargon

    assert dejargon("How do you test? (Evidence: E1, E2, E9) (from CI run 7)") == "How do you test? (from CI run 7)"
    assert dejargon("What about bullets? [E7] (from README.md)") == "What about bullets? (from README.md)"
    assert dejargon("Evidence E15 shows the retriever is unused.") == "Evidence shows the retriever is unused."
