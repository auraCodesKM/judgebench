"""Worker RECEIVED -> PROFILED: pinned fetch, retries, resume, no duplicate work."""

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from judgebench.db import models as m
from judgebench.states import State
from judgebench.worker.github import GitHub, RepoError, RepoRef, Tarball
from judgebench.worker.pipeline import run_profile_stage
from tests.conftest import make_event, make_submission, make_team
from tests.security import tarballs as tb

pytestmark = pytest.mark.db
SHA = "3f2c1ab" + "0" * 33
RAG_REPO = tb.tar_gz(
    {
        "README.md": "# Alpha\n",
        "requirements.txt": "langchain\nfaiss-cpu\nfastapi\n",
        "app/main.py": "from fastapi import FastAPI\nfrom langgraph.graph import StateGraph\napp = FastAPI()\n",
        "tests/test_app.py": "def test_x():\n    assert True\n",
    }
)


class FakeGitHub(GitHub):
    def __init__(self, failures: list[RepoError | Exception] | None = None) -> None:
        self.failures = failures or []
        self.resolves = 0
        self.untils: list[datetime] = []
        self.downloads: list[str] = []

    def resolve_commit(self, ref: RepoRef, until: datetime) -> str:
        self.resolves += 1
        self.untils.append(until)
        return SHA

    def download_tarball(self, ref: RepoRef, sha: str, dest: Path) -> Tarball:
        self.downloads.append(sha)
        if self.failures:
            raise self.failures.pop(0)
        dest.write_bytes(RAG_REPO)
        return Tarball(dest, "deadbeef", len(RAG_REPO), False)


@pytest.fixture
def sub(session: Session) -> m.Submission:
    s = make_submission(session, make_team(session, make_event(session, "ev"), "Alpha"))
    session.commit()
    return s


def test_profiles_and_announces(session_factory: sessionmaker[Session], session: Session, sub: m.Submission) -> None:
    assert run_profile_stage(session_factory, FakeGitHub()) == sub.id
    session.refresh(sub)
    assert sub.state == State.PROFILED and sub.commit_sha == SHA

    row = session.query(m.RepositoryProfileRow).one()
    assert set(row.categories) == {"llm_rag", "agentic"}
    assert row.profile["recommended_adapters"] == ["agentic", "rag"]
    assert session.query(m.SubmissionArtefact).one().details["files_written"] == 4
    [note] = session.query(m.Notification).filter_by(stage="PROFILED").all()
    assert note.text.startswith("Profiled (commit 3f2c1ab): Python")
    assert "adapters: Agentic, RAG" in note.text


def test_queue_empty_and_no_duplicate_work(
    session_factory: sessionmaker[Session], session: Session, sub: m.Submission
) -> None:
    gh = FakeGitHub()
    run_profile_stage(session_factory, gh)
    assert run_profile_stage(session_factory, gh) is None
    assert gh.downloads == [SHA]
    assert session.query(m.RepositoryProfileRow).count() == 1


def test_transient_error_retries_then_succeeds_with_same_commit(
    session_factory: sessionmaker[Session], session: Session, sub: m.Submission
) -> None:
    gh = FakeGitHub([RepoError("github_unavailable", True, "HTTP 502")])
    run_profile_stage(session_factory, gh)
    session.refresh(sub)
    assert sub.state == State.RECEIVED and sub.attempts == 1 and sub.commit_sha == SHA

    run_profile_stage(session_factory, gh)
    session.refresh(sub)
    assert sub.state == State.PROFILED and sub.attempts == 0  # reset for the next stage
    assert gh.resolves == 1  # commit pinned on the first attempt, reused on retry
    assert gh.downloads == [SHA, SHA]


def test_crash_mid_stage_leaves_no_partial_rows(
    session_factory: sessionmaker[Session], session: Session, sub: m.Submission
) -> None:
    run_profile_stage(session_factory, FakeGitHub([RuntimeError("worker killed")]))
    session.refresh(sub)
    assert sub.state == State.RECEIVED and sub.error_category == "internal_error"
    assert session.query(m.RepositoryProfileRow).count() == 0
    assert session.query(m.Notification).count() == 0


def test_missing_repo_fails_without_retry(
    session_factory: sessionmaker[Session], session: Session, sub: m.Submission
) -> None:
    run_profile_stage(session_factory, FakeGitHub([RepoError("repo_not_found", False, "private")]))
    session.refresh(sub)
    assert sub.state == State.FAILED and sub.retryable is False
    assert run_profile_stage(session_factory, FakeGitHub()) is None


def test_commit_pinned_to_latest_at_or_before_submission_time(
    session_factory: sessionmaker[Session], session: Session, sub: m.Submission
) -> None:
    gh = FakeGitHub()
    run_profile_stage(session_factory, gh)
    assert gh.untils == [sub.submitted_at]


def test_resolve_commit_queries_until_submission_timestamp() -> None:
    import httpx

    from judgebench.worker.github import GitHub, RepoRef

    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=[{"sha": SHA}])

    gh = GitHub("tok", httpx.Client(transport=httpx.MockTransport(handler)))
    submitted = datetime.fromisoformat("2026-11-22T10:15:30.250+05:30")
    assert gh.resolve_commit(RepoRef("acme", "demo", "main"), until=submitted) == SHA
    [req] = seen
    assert req.url.path == "/repos/acme/demo/commits"
    assert dict(req.url.params) == {"until": "2026-11-22T04:45:30Z", "per_page": "1", "sha": "main"}
    assert req.headers["authorization"] == "Bearer tok"


def test_no_commit_before_submission_is_not_retryable() -> None:
    import httpx

    from judgebench.worker.github import GitHub, RepoRef

    gh = GitHub(None, httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))))
    with pytest.raises(RepoError) as exc:
        gh.resolve_commit(RepoRef("acme", "demo"), until=datetime.fromisoformat("2026-11-22T10:00:00+00:00"))
    assert exc.value.category == "no_commit_before_submission" and not exc.value.retryable
