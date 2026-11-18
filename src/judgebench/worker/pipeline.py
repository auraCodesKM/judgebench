"""Worker stages. M1: RECEIVED -> PROFILED (fetch pinned tarball, extract, profile).

Each stage: claim with a lease -> do the work -> persist results + transition in one
transaction. A crash anywhere before that commit leaves the submission in its previous state,
and the expired lease lets the next run resume it. The resolved commit SHA is committed first,
so a retry fetches the same commit even if the team pushes again.
"""

import logging
import tempfile
import uuid
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from judgebench.adapters.profiler import RepositoryProfile, profile_repository
from judgebench.db.models import RepositoryProfileRow, Submission, SubmissionArtefact
from judgebench.states import State
from judgebench.worker.extract import extract_tarball
from judgebench.worker.github import GitHub, RepoError, parse_repo_url
from judgebench.workflow import claim_next, record_failure, sweep_exhausted, transition

log = logging.getLogger(__name__)


def profiled_message(p: RepositoryProfile) -> str:
    langs = sorted(p.languages, key=lambda k: -p.languages[k])[:3]
    stack = ", ".join(langs + p.technologies[:6]) or "no recognised stack"
    adapters = ", ".join(a.upper() if len(a) <= 3 else a.capitalize() for a in p.recommended_adapters)
    msg = f"Profiled (commit {p.commit_sha[:7]}): {stack} -> adapters: {adapters}"
    if p.limitations:
        msg += f" ({len(p.limitations)} limitation{'s' if len(p.limitations) > 1 else ''} recorded)"
    return msg


def _profile(session: Session, github: GitHub, sid: uuid.UUID, scratch: Path | None) -> None:
    sub = session.get_one(Submission, sid)
    ref = parse_repo_url(sub.repo_url)
    if ref is None:
        raise RepoError("invalid_repo_url", False)
    if sub.commit_sha is None:
        sub.commit_sha = github.resolve_commit(ref, until=sub.submitted_at)
        session.commit()  # pin before fetching; retries reuse it
    sha = sub.commit_sha

    with tempfile.TemporaryDirectory(dir=scratch, prefix="sub-") as tmp:
        tarball = github.download_tarball(ref, sha, Path(tmp) / "repo.tar.gz")
        with tarball.path.open("rb") as f:
            result = extract_tarball(f, Path(tmp) / "src")
        limitations = list(result.limitations)
        if tarball.truncated:
            limitations.insert(0, "repository tarball exceeded download cap; truncated")
        profile = profile_repository(Path(tmp) / "src", commit_sha=sha, limitations=limitations)

    scope = {"tenant_id": sub.tenant_id, "event_id": sub.event_id, "submission_id": sid}
    session.add(
        SubmissionArtefact(
            **scope,
            type="repo_tarball",
            sha256=tarball.sha256,
            size_bytes=tarball.size,
            details={
                "commit_sha": sha,
                "files_written": result.files_written,
                "rejected": len(result.rejected),
                "skipped": len(result.skipped),
                "truncated": result.truncated or tarball.truncated,
            },
        )
    )
    session.execute(
        insert(RepositoryProfileRow)
        .values(
            id=uuid.uuid4(),
            **scope,
            commit_sha=sha,
            profiler_version=profile.profiler_version,
            categories=profile.categories,
            technologies=profile.technologies,
            profile=profile.model_dump(mode="json"),
        )
        .on_conflict_do_nothing(index_elements=["submission_id", "commit_sha", "profiler_version"])
    )
    sub.source_hashes = {**sub.source_hashes, "repo_tarball_sha256": tarball.sha256}
    transition(
        session,
        sid,
        State.RECEIVED,
        State.PROFILED,
        message=profiled_message(profile),
        detail={"categories": profile.categories, "adapters": profile.recommended_adapters},
    )


def run_profile_stage(sessions: sessionmaker[Session], github: GitHub, scratch: Path | None = None) -> uuid.UUID | None:
    """Process at most one RECEIVED submission. Returns its ID, or None if the queue is empty."""
    with sessions() as session:
        sweep_exhausted(session, State.RECEIVED)
        session.commit()
        sid = claim_next(session, State.RECEIVED)
        if sid is None:
            return None
        try:
            _profile(session, github, sid, scratch)
            session.commit()
        except RepoError as exc:
            session.rollback()
            record_failure(session, sid, exc.category, retryable=exc.retryable, detail=str(exc))
            session.commit()
        except Exception as exc:
            log.exception("profile stage crashed for %s", sid)
            session.rollback()
            record_failure(session, sid, "internal_error", retryable=True, detail=type(exc).__name__)
            session.commit()
        return sid
