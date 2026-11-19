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
        'You say "Our RAG system answers in real time", yet it took 8.5 s for me. What\'s your target?',
        "the live check",
    )
    assert all(len(q.split()) <= 20 for q, _ in qs)  # short enough to ask out loud
    assert qs[1][0] == 'Where does "98% accuracy" come from? I couldn\'t find anything that measures it.'
    assert any("nothing calls it" in q for q, _ in qs)
    assert render_question(*qs[0], template=True).endswith("(from the live check; template)")


def test_concern_prefers_the_strongest_negative_evidence() -> None:
    concern = top_concern(BETA_CLAIMS, BETA_EV, [], {})
    assert concern.startswith('They say "Our RAG system answers in real time", but') and "8.5 s" in concern
    assert len(concern.split()) <= 21
    zeta = [
        e(
            "ci.green_without_tests",
            "OBSERVED",
            'GitHub Actions "ci" passed on c8352c3 but the workflow runs no tests',
            "ci:run/9",
        )
    ]
    assert top_concern([], zeta, [], {}) == "CI is green, but it doesn't run any tests."
    assert (
        template_questions([], zeta, None)[0][0]
        == "Your CI is green but runs no tests. What would catch a bug before a demo?"
    )


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


def test_paths_keep_their_leading_dot() -> None:
    from judgebench.judging import dejargon

    assert (
        dejargon("Why no lock file? (E9) (from .github/workflows/test.yml)")
        == "Why no lock file? (from .github/workflows/test.yml)"
    )


def test_overlong_model_output_is_cut_not_rejected() -> None:
    from judgebench.agents.schemas import RubricResult

    r = RubricResult.model_validate(
        {
            "criteria": [
                {
                    "criterion_id": "x",
                    "score": 1,
                    "rationale": "y" * 5000,
                    "confidence": 0.5,
                    "missing_evidence": ["m"] * 9,
                }
            ],
            "suggested_questions": [{"question": "q" * 900, "evidence_ids": ["E1"]}] * 6,
        }
    )
    assert len(r.criteria[0].rationale) == 1200 and len(r.criteria[0].missing_evidence) == 5
    assert len(r.suggested_questions) == 3 and len(r.suggested_questions[0].question) == 300


def test_raw_evidence_lines_become_plain_words() -> None:
    from judgebench.judging import dejargon

    q = ("Your README claims Answers in real time (under 1 second).; live.sample_query found POST /ask -> 200 in "
         "8554 ms (application/json); route found in code.")  # fmt: skip
    assert dejargon(q) == (
        "Your README claims Answers in real time (under 1 second); the live sample request found POST /ask "
        "answered in 8.6 s; route found in code."
    )
    assert dejargon("see app.py and rag.py") == "see app.py and rag.py"  # file names untouched
