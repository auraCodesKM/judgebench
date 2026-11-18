"""Agreement report: AI vs mean human score per criterion, with the human-vs-human baseline."""

import pytest
from sqlalchemy.orm import Session, sessionmaker

from judgebench import agreement
from judgebench.agents.offline import offline_model
from judgebench.db import models as m
from tests import fixtures_repos as repos
from tests.fake_llm import fake
from tests.test_m2_flow import deps, drive, submit

pytestmark = pytest.mark.db


def review(session: Session, ev: m.Evaluation, judge: str, crit: str, score: float) -> None:
    session.add(
        m.JudgeReview(
            tenant_id=ev.tenant_id,
            event_id=ev.event_id,
            evaluation_id=ev.id,
            judge_label=judge,
            criterion_id=crit,
            human_scores={crit: score},
        )
    )


def test_agreement_numbers(session_factory: sessionmaker[Session], session: Session) -> None:
    subs = [submit(session, f"Team {i}") for i in range(3)]
    drive(session_factory, deps(repos.ALPHA_RAG, fake()))
    evs = [session.query(m.Evaluation).filter_by(submission_id=s.id, status="ready").one() for s in subs]
    crit = session.query(m.CriterionScore).filter_by(evaluation_id=evs[0].id, criterion_id="model_quality").one()
    ai, mx = crit.score, crit.max_score
    for i, ev in enumerate(evs):
        review(session, ev, "A", "model_quality", ai + i)  # off by 0, 1, 2
        review(session, ev, "B", "model_quality", ai + i + 2)  # judges disagree by 2 every time
    review(session, evs[0], "A", "overall", 70)  # no AI counterpart: ignored
    session.commit()

    rows, mock = agreement.report(session, evs[0].event_id)
    mq = next(r for r in rows if r.criterion == "model_quality")
    assert mock == 0 and mq.pairs == 3
    assert mq.mae == 2.0  # |ai - mean(ai+i, ai+i+2)| = i+1 -> mean 2
    assert mq.judge_pairs == 3 and mq.judge_mae_pct == round(100 * 2 / mx, 1)
    assert mq.spearman is None  # AI scores identical across teams: no rank spread
    assert rows[-1].criterion == "ALL" and rows[-1].pairs == 3


def test_mock_evaluations_are_flagged(session_factory: sessionmaker[Session], session: Session) -> None:
    sub = submit(session, "Team Mock")
    drive(session_factory, deps(repos.ALPHA_RAG, offline_model()))
    ev = session.query(m.Evaluation).filter_by(submission_id=sub.id, status="ready").one()
    review(session, ev, "A", "ai_technical_depth", 10)
    session.commit()
    rows, mock = agreement.report(session, ev.event_id)
    assert mock == 1 and "MOCK" in agreement.render(rows, mock)
