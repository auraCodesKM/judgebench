"""GitHub CI evidence (§7.5 source 1). The team's code ran on GitHub's machines, not ours.

Two independent reads, then a cross-check:
1. Static: parse `.github/workflows/*.yml` from the snapshot and list the `run:` commands per job.
   A workflow whose commands contain no test runner is "CI without tests", whatever its colour.
2. API: workflow runs for the pinned commit + their jobs/steps, and a capped tail of each job log
   for a test-summary line (e.g. pytest's "14 passed in 2.1s"). Logs are participant-controlled
   text, so only numbers are regex-extracted from them; raw log text is never stored or prompted.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

from judgebench.evidence import CheckStatus, EvidenceStatus, Finding, SourceType
from judgebench.worker.github import API, GitHub, RepoError, RepoRef

MAX_WORKFLOW_BYTES = 64 * 1024
MAX_LOG_BYTES = 2 * 1024 * 1024
MAX_RUNS = 10
LOG_HOST_SUFFIXES = (".blob.core.windows.net", ".actions.githubusercontent.com", "github.com")
TEST_COMMAND = re.compile(
    r"\b(pytest|py\.test|unittest|nose2|tox|nox|jest|vitest|mocha|ava|go test|cargo test|mvn( -\S+)* (test|verify)"
    r"|gradle(w)? (test|check)|dotnet test|rspec|phpunit|ctest|bun test|deno test"
    r"|(npm|pnpm|yarn)( run)? test|make (test|check))\b"
)
SUMMARIES = [  # (regex, what it counts) on log lines; numbers only
    (re.compile(r"=+ .*?(\d+) passed.*? in [\d.]+s"), "pytest"),
    (re.compile(r"Ran (\d+) tests? in [\d.]+s"), "unittest"),
    (re.compile(r"Tests:\s+(?:\d+ failed, )?(\d+) passed"), "jest"),
    (re.compile(r"test result: \w+\. (\d+) passed"), "cargo"),
    (re.compile(r"Tests run: (\d+)"), "maven"),
]


@dataclass
class WorkflowFile:
    path: str
    name: str
    commands: dict[str, list[str]] = field(default_factory=dict)  # job -> run commands

    @property
    def test_commands(self) -> list[str]:
        return [c for cmds in self.commands.values() for c in cmds if TEST_COMMAND.search(c)]


def read_workflows(root: Path) -> list[WorkflowFile]:
    out = []
    for p in sorted((root / ".github" / "workflows").glob("*.y*ml")):
        if p.is_symlink() or not p.is_file() or p.stat().st_size > MAX_WORKFLOW_BYTES:
            continue
        try:
            doc = yaml.safe_load(p.read_text(errors="replace"))
        except yaml.YAMLError:
            continue
        if not isinstance(doc, dict):
            continue
        wf = WorkflowFile(p.relative_to(root).as_posix(), str(doc.get("name") or p.stem))
        jobs = doc.get("jobs")
        for job_id, job in list(jobs.items() if isinstance(jobs, dict) else [])[:50]:
            steps = job.get("steps") if isinstance(job, dict) else None
            steps = steps if isinstance(steps, list) else []
            wf.commands[str(job_id)] = [str(s["run"])[:500] for s in steps[:100] if isinstance(s, dict) and "run" in s]
        out.append(wf)
    return out


class CiReader:
    """GitHub Actions API, read-only, fixed hosts (api.github.com + GitHub's log blob storage)."""

    def __init__(self, github: GitHub) -> None:
        self._gh = github

    def _json(self, url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        resp = self._gh.get(url, params)
        if resp.status_code != 200:
            raise RepoError("github_unavailable", True, f"HTTP {resp.status_code}")
        data = resp.json()
        return data if isinstance(data, dict) else {}

    def runs(self, ref: RepoRef, sha: str) -> list[dict[str, Any]]:
        data = self._json(
            f"{API}/repos/{ref.owner}/{ref.repo}/actions/runs", {"head_sha": sha, "per_page": str(MAX_RUNS)}
        )
        return list(data.get("workflow_runs", []))[:MAX_RUNS]

    def jobs(self, ref: RepoRef, run_id: int) -> list[dict[str, Any]]:
        return list(self._json(f"{API}/repos/{ref.owner}/{ref.repo}/actions/runs/{run_id}/jobs").get("jobs", []))

    def log_test_count(self, ref: RepoRef, job_id: int) -> tuple[str, int] | None:
        """(runner, tests passed) parsed from the job log tail, or None. Best effort: any failure -> None."""
        try:
            resp = self._gh.http.get(f"{API}/repos/{ref.owner}/{ref.repo}/actions/jobs/{job_id}/logs")
            if resp.status_code in (301, 302, 307, 308):
                url = httpx.URL(resp.headers.get("location", ""))
                if url.scheme != "https" or not url.host.endswith(LOG_HOST_SUFFIXES):
                    return None
                with httpx.Client(timeout=30) as plain, plain.stream("GET", url) as log:  # signed URL: no auth header
                    buf = b""
                    for chunk in log.iter_bytes(65536):
                        buf = (buf + chunk)[-MAX_LOG_BYTES:]
                    text = buf.decode("utf-8", "replace")
            elif resp.status_code == 200:
                text = resp.text[-MAX_LOG_BYTES:]
            else:
                return None
        except httpx.HTTPError:
            return None
        for line in reversed(text.splitlines()[-400:]):
            for rx, runner in SUMMARIES:
                if m := rx.search(line):
                    return runner, int(m.group(1))
        return None


def ci_findings(
    reader: CiReader | None, ref: RepoRef, sha: str, root: Path
) -> tuple[list[Finding], CheckStatus, dict[str, Any]]:
    """Returns (findings, CI check status, details for behaviour_checks + Telegram)."""
    workflows = read_workflows(root)
    tested = {w.path for w in workflows if w.test_commands}
    details: dict[str, Any] = {
        "workflows": [{"path": w.path, "name": w.name, "test_commands": w.test_commands[:5]} for w in workflows]
    }
    if not workflows:
        f = Finding(
            key="ci.configured",
            observation="No GitHub Actions workflows in the repository",
            source_type=SourceType.CI,
            source_locator=".github/workflows",
            status=EvidenceStatus.MISSING,
            confidence=0.95,
            verification_method="ci_read",
            criterion="engineering",
        )
        return [f], CheckStatus.NOT_VERIFIABLE, details | {"summary": "no CI configured"}

    findings = []
    for w in workflows:
        if not w.test_commands:
            cmds = [c for cs in w.commands.values() for c in cs][:3]
            findings.append(
                Finding(
                    key="ci.workflow_without_tests",
                    criterion="engineering",
                    source_type=SourceType.CI,
                    observation=f'Workflow "{w.name}" runs no test command (steps: {"; ".join(cmds) or "none"})',
                    source_locator=w.path,
                    status=EvidenceStatus.OBSERVED,
                    confidence=0.9,
                    verification_method="ci_read",
                    path=w.path,
                )
            )
    if reader is None:
        details["summary"] = "CI runs not read (no GitHub access)"
        return findings, CheckStatus.NOT_ATTEMPTED, details

    runs = reader.runs(ref, sha)
    if not runs:
        findings.append(
            Finding(
                key="ci.runs",
                observation=f"No workflow runs found for commit {sha[:7]}",
                source_type=SourceType.CI,
                source_locator=f"ci:commit/{sha[:7]}",
                status=EvidenceStatus.MISSING,
                confidence=0.9,
                verification_method="ci_read",
                criterion="engineering",
            )
        )
        details["summary"] = f"no workflow runs on {sha[:7]}"
        return findings, CheckStatus.NOT_VERIFIABLE, details

    status, lines = CheckStatus.NOT_VERIFIABLE, []
    for run in runs:
        name, path, conclusion = str(run.get("name", "?")), str(run.get("path", "")), run.get("conclusion")
        locator = f"ci:run/{run.get('id')}"
        has_tests = path in tested
        if conclusion == "success":
            count = None
            if has_tests:
                for job in reader.jobs(ref, int(run["id"]))[:5]:
                    if job.get("conclusion") == "success" and (count := reader.log_test_count(ref, int(job["id"]))):
                        break
            behavioural = has_tests and (count is None or count[1] > 0)
            tests_txt = (
                (f"{count[0]}, {count[1]} tests" if count else "test command present") if has_tests else "no tests"
            )
            obs = f'GitHub Actions "{name}" passed on {sha[:7]} ({tests_txt})'
            if not has_tests:
                obs = f'GitHub Actions "{name}" passed on {sha[:7]} but the workflow runs no tests'
            findings.append(
                Finding(
                    key="ci.green_without_tests" if not has_tests else "ci.tests_passed",
                    observation=obs,
                    source_type=SourceType.CI,
                    source_locator=locator,
                    criterion="engineering",
                    status=EvidenceStatus.VERIFIED if behavioural else EvidenceStatus.OBSERVED,
                    confidence=0.9,
                    verification_method="ci_read",
                    behavioural=behavioural,
                )
            )
            if behavioural:
                status = CheckStatus.VERIFIED
            lines.append(obs.removeprefix("GitHub Actions "))
        elif conclusion in ("failure", "timed_out", "startup_failure"):
            failed = [j.get("name", "?") for j in reader.jobs(ref, int(run["id"])) if j.get("conclusion") == "failure"][
                :3
            ]
            obs = f'GitHub Actions "{name}" failed on {sha[:7]}' + (f" (jobs: {', '.join(failed)})" if failed else "")
            findings.append(
                Finding(
                    key="ci.failed",
                    observation=obs,
                    source_type=SourceType.CI,
                    source_locator=locator,
                    status=EvidenceStatus.OBSERVED,
                    confidence=0.9,
                    verification_method="ci_read",
                    criterion="engineering",
                )
            )
            if status is not CheckStatus.VERIFIED:
                status = CheckStatus.CHECK_FAILED
            lines.append(obs.removeprefix("GitHub Actions "))
    details["summary"] = "; ".join(lines[:3]) or "runs still in progress"
    return findings, status, details
