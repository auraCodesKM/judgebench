"""CI evidence: real tests vs green-without-tests, failures, no runs (§7.5, demo cast Alpha/Zeta)."""

from pathlib import Path
from typing import Any

from judgebench.evidence import CheckStatus, EvidenceStatus
from judgebench.worker.ci import CiReader, ci_findings, read_workflows
from judgebench.worker.github import RepoRef
from tests import fixtures_repos as repos

SHA = "3f2c1ab" + "0" * 33
REF = RepoRef("acme", "demo")


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


class FakeReader(CiReader):
    def __init__(
        self, runs: list[dict[str, Any]], jobs: list[dict[str, Any]] | None = None, tests: tuple[str, int] | None = None
    ) -> None:
        self._runs, self._jobs, self._tests = runs, jobs or [], tests

    def runs(self, ref: RepoRef, sha: str) -> list[dict[str, Any]]:
        return self._runs

    def jobs(self, ref: RepoRef, run_id: int) -> list[dict[str, Any]]:
        return self._jobs

    def log_test_count(self, ref: RepoRef, job_id: int) -> tuple[str, int] | None:
        return self._tests


def test_workflow_parsing_finds_test_commands(tmp_path: Path) -> None:
    [wf] = read_workflows(write(tmp_path, repos.ALPHA_RAG))
    assert wf.test_commands == ["pytest -q"]


def test_green_ci_with_real_tests_is_behavioural(tmp_path: Path) -> None:
    reader = FakeReader(
        [{"id": 7, "name": "test", "path": ".github/workflows/test.yml", "conclusion": "success"}],
        [{"id": 70, "name": "test", "conclusion": "success"}],
        ("pytest", 14),
    )
    findings, status, details = ci_findings(reader, REF, SHA, write(tmp_path, repos.ALPHA_RAG))
    [f] = findings
    assert status == CheckStatus.VERIFIED and f.behavioural and f.status == EvidenceStatus.VERIFIED
    assert f.observation == 'GitHub Actions "test" passed on 3f2c1ab (pytest, 14 tests)'
    assert details["summary"] == '"test" passed on 3f2c1ab (pytest, 14 tests)'


def test_green_ci_that_runs_no_tests_is_flagged(tmp_path: Path) -> None:
    reader = FakeReader([{"id": 9, "name": "ci", "path": ".github/workflows/ci.yml", "conclusion": "success"}])
    findings, status, _ = ci_findings(reader, REF, SHA, write(tmp_path, repos.ZETA_EMPTY_CI))
    keys = {f.key: f for f in findings}
    assert status == CheckStatus.NOT_VERIFIABLE  # green, but not a behaviour source
    assert not keys["ci.green_without_tests"].behavioural
    assert "runs no tests" in keys["ci.green_without_tests"].observation
    assert 'echo "tests passed"' in keys["ci.workflow_without_tests"].observation


def test_failed_ci_is_check_failed_not_penalty(tmp_path: Path) -> None:
    reader = FakeReader(
        [{"id": 3, "name": "test", "path": ".github/workflows/test.yml", "conclusion": "failure"}],
        [{"id": 30, "name": "unit", "conclusion": "failure"}],
    )
    findings, status, _ = ci_findings(reader, REF, SHA, write(tmp_path, repos.ALPHA_RAG))
    assert status == CheckStatus.CHECK_FAILED
    assert findings[0].observation == 'GitHub Actions "test" failed on 3f2c1ab (jobs: unit)'


def test_no_ci_and_no_runs_are_not_verifiable(tmp_path: Path) -> None:
    _, status, details = ci_findings(FakeReader([]), REF, SHA, write(tmp_path / "a", repos.ML_CLASSIFIER))
    assert status == CheckStatus.NOT_VERIFIABLE and details["summary"] == "no CI configured"
    _, status, details = ci_findings(FakeReader([]), REF, SHA, write(tmp_path / "b", repos.ALPHA_RAG))
    assert status == CheckStatus.NOT_VERIFIABLE and details["summary"] == "no workflow runs on 3f2c1ab"


def test_hostile_workflow_yaml_is_contained(tmp_path: Path) -> None:
    bomb = (
        "a: &a [x,x,x,x,x,x,x,x,x]\nb: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a]\nc: &c [*b,*b,*b,*b,*b,*b,*b,*b,*b]\njobs: *c\n"
    )
    write(
        tmp_path,
        {
            ".github/workflows/x.yml": bomb,
            ".github/workflows/big.yml": "x" * 70_000,
            ".github/workflows/bad.yml": ":\n- [unclosed",
        },
    )
    assert [w.commands for w in read_workflows(tmp_path)] == [{}]  # bomb parsed safely, others skipped


def test_log_summary_parsing() -> None:
    import httpx

    from judgebench.worker.github import GitHub

    log = "noise\n============ 14 passed, 1 warning in 2.31s ============\n"
    gh = GitHub("t", httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=log))))
    assert CiReader(gh).log_test_count(REF, 1) == ("pytest", 14)
