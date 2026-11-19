"""Claim verdicts by majority of 3 (self-consistency) in model modes: each vote clamped by the §7.5
rules on its own, majority wins, no majority = UNVERIFIED, the split is recorded on the claim and as
evidence, and the extra votes see the investigator's own observations."""

from typing import Any

import pytest
from google.genai import types
from sqlalchemy.orm import Session, sessionmaker

from judgebench.db import models as m
from judgebench.states import State
from tests import fixtures_repos as repos
from tests.fake_llm import Seen, fake
from tests.test_m2_flow import deps, drive, submit

pytestmark = pytest.mark.db


def verdicts(status_by_claim: dict[str, str]) -> Any:
    def handler(s: Seen) -> Any:
        ids = s.ids("C")
        return {
            "verdicts": [
                {
                    "claim_id": c,
                    "status": status_by_claim.get(c, status_by_claim.get("*", "UNVERIFIED")),
                    "evidence_ids": s.ids("E")[:1],
                    "rationale": f"{s.agent} on {c}",
                }
                for c in ids
            ]
        }

    return handler


def investigator(status: str) -> Any:
    def handler(s: Seen) -> Any:
        if s.turn == 0:
            return types.FunctionCall(name="search_code", args={"text": "retriever"})
        return verdicts({"*": status})(s)

    return handler


def run(session_factory: sessionmaker[Session], session: Session, **handlers: Any) -> tuple[m.Submission, list[Seen]]:
    sub = submit(session, "Team Beta")
    llm = fake(**handlers)
    d = deps(repos.BETA_RAG_UNWIRED, llm)
    d.votes = 3
    drive(session_factory, d)
    session.refresh(sub)
    assert sub.state == State.READY_FOR_REVIEW
    return sub, llm.seen


def test_majority_wins_and_the_split_is_recorded(session_factory: sessionmaker[Session], session: Session) -> None:
    sub, seen = run(
        session_factory,
        session,
        claim_investigator=investigator("CONTRADICTED"),
        claim_judge_2=verdicts({"*": "CONTRADICTED"}),
        claim_judge_3=verdicts({"*": "UNVERIFIED"}),
    )
    claims = session.query(m.Claim).filter_by(submission_id=sub.id).all()
    assert claims and all(c.verification_status == "CONTRADICTED" for c in claims)
    assert all(c.rationale.startswith("[CONTRADICTED 2/3 (UNVERIFIED 1)] claim_investigator on") for c in claims)
    votes = session.query(m.Evidence).filter_by(submission_id=sub.id, key="investigation.verdict_votes").all()
    assert len(votes) == len(claims) and all(v.status == "CONTRADICTED" for v in votes)
    judge = next(s for s in seen if s.agent == "claim_judge_2")
    assert "investigator_observations" in judge.text and "search_code(text=retriever)" in judge.text
    assert judge.tools == []  # judges have no tools


def test_no_majority_is_unverified_and_each_vote_is_clamped(
    session_factory: sessionmaker[Session], session: Session
) -> None:
    sub, _ = run(
        session_factory,
        session,
        claim_investigator=investigator("CONTRADICTED"),
        claim_judge_2=verdicts({"*": "VERIFIED"}),  # no behavioural evidence: clamped before counting
        claim_judge_3=verdicts({"*": "MISSING_EVIDENCE"}),
    )
    claims = session.query(m.Claim).filter_by(submission_id=sub.id).all()
    assert claims and all(c.verification_status == "UNVERIFIED" for c in claims)
    assert all(
        c.rationale.startswith("[no majority (") and "No majority among the votes." in c.rationale for c in claims
    )


def test_offline_and_single_vote_runs_add_no_judges(session_factory: sessionmaker[Session], session: Session) -> None:
    sub = submit(session, "Team Solo")
    llm = fake()
    drive(session_factory, deps(repos.BETA_RAG_UNWIRED, llm))  # votes default 1
    assert not any(s.agent.startswith("claim_judge") for s in llm.seen)
    assert all(not c.rationale.startswith("[") for c in session.query(m.Claim).filter_by(submission_id=sub.id))
