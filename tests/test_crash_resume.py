"""Kill -9 the worker mid-evaluation, restart: resumes the interrupted stage only, with no duplicate
evidence, evaluations or Telegram messages (§16.3, §20.3 item 7)."""

import os
import signal
import subprocess
import sys
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from judgebench.db import models as m
from judgebench.states import State
from tests import fixtures_repos as repos
from tests.conftest import TEST_DB_URL
from tests.fake_llm import fake
from tests.test_m2_flow import deps, drive, submit

pytestmark = pytest.mark.db


def test_sigkill_mid_evaluation_then_resume(session_factory: sessionmaker[Session], session: Session,
                                            tmp_path: Path) -> None:
    sub = submit(session, "Team Alpha")
    flag = tmp_path / "flag"
    worker = subprocess.Popen([sys.executable, "-m", "tests.crash_worker", TEST_DB_URL, str(flag)],
                              cwd=Path(__file__).parents[1], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 90
    while not flag.exists():
        assert worker.poll() is None, worker.stderr.read().decode()[-2000:] if worker.stderr else "worker died"
        assert time.monotonic() < deadline, "worker never reached the evaluator"
        time.sleep(0.1)
    os.kill(worker.pid, signal.SIGKILL)  # no cleanup, no rollback handler, no finally blocks
    worker.wait(10)

    session.expire_all()
    session.refresh(sub)
    assert sub.state == State.EVALUATING and sub.lease_until is not None  # died holding the lease
    assert session.query(m.Evaluation).count() == 0  # nothing half-written
    before = Counter(n.stage for n in session.query(m.Notification).filter_by(submission_id=sub.id))

    # Lease expires (time passes), a new worker starts.
    session.query(m.Submission).filter_by(id=sub.id).update({"lease_until": m.utcnow() - timedelta(seconds=1)})
    session.commit()
    llm = fake()
    drive(session_factory, deps(repos.ALPHA_RAG, llm))

    session.expire_all()
    session.refresh(sub)
    assert sub.state == State.READY_FOR_REVIEW
    assert not any(s.agent == "claim_analyst" for s in llm.seen)  # evidence stage was not redone
    assert {e.stage_attempt for e in session.query(m.Evidence).filter_by(submission_id=sub.id)} == {
        sub.source_hashes["evidence_attempt"]}
    assert session.query(m.Evaluation).filter_by(submission_id=sub.id).count() == 1
    after = Counter(n.stage for n in session.query(m.Notification).filter_by(submission_id=sub.id))
    assert all(v == 1 for k, v in after.items() if k != "CRITIC"), after  # one message per stage
    assert {k: after[k] for k in before} == dict(before)  # nothing announced twice
    retries = session.query(m.AuditEvent).filter_by(submission_id=sub.id, action="state:CRITIQUING").count()
    assert retries == 1
