"""explain: one screen per team with verdicts, checks, critic revisions, tools and a scope check."""

import pytest
from sqlalchemy.orm import Session, sessionmaker

from judgebench.explain import explain
from tests import fixtures_repos as repos
from tests.fake_llm import fake
from tests.test_m2_flow import deps, drive, submit

pytestmark = pytest.mark.db


def test_explain_shows_critic_tools_and_scope(session_factory: sessionmaker[Session], session: Session) -> None:
    a = submit(session, "Team Alpha")
    submit(session, "Team Beta")
    drive(session_factory, deps(repos.ALPHA_RAG, fake()))
    text = explain(session, a.event_id)
    assert text.count("== Team ") == 2
    assert "critic model_quality        16 -> 9" in text  # uncited score cut to floor(45% of 20)
    assert "check  ci         VERIFIED" in text
    assert "tools  1 call(s): search_code(similarity_search)" in text
    assert text.count("other teams named: none") == 2


def test_explain_names_a_leak(session_factory: sessionmaker[Session], session: Session) -> None:
    def leaky(s):  # type: ignore[no-untyped-def]
        from tests.fake_llm import evaluator

        out = evaluator(s)
        out["suggested_questions"] = ["Why is Team Beta ahead of you?"]
        return out

    a = submit(session, "Team Alpha")
    submit(session, "Team Beta")
    drive(session_factory, deps(repos.ALPHA_RAG, fake(rubric_evaluator=leaky)))
    assert "other teams named: Team Beta" in explain(session, a.event_id)
