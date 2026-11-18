"""RAG / agentic / ML / generic inspectors and static wiring analysis (§7.3, §7.5)."""

from pathlib import Path

from judgebench.adapters.base import Snapshot
from judgebench.adapters.inspectors import inspect_repository
from judgebench.adapters.profiler import profile_repository
from judgebench.adapters.wiring import CodeIndex
from judgebench.evidence import EvidenceStatus as S
from judgebench.evidence import Finding
from tests import fixtures_repos as repos


def snapshot(tmp_path: Path, files: dict[str, str]) -> Snapshot:
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    profile = profile_repository(tmp_path, commit_sha="a" * 40, limitations=[])
    return Snapshot(tmp_path, profile, CodeIndex(tmp_path))


def by_key(findings: list[Finding]) -> dict[str, Finding]:
    return {f.key: f for f in findings}


def test_rag_retrieval_wired_to_request_handler(tmp_path: Path) -> None:
    f = by_key(inspect_repository(snapshot(tmp_path, repos.ALPHA_RAG)))
    assert f["rag.retrieval_on_request_path"].status == S.OBSERVED
    assert f["rag.retrieval_on_request_path"].source_locator.startswith("app/rag.py#L")
    assert f["rag.retrieval_on_request_path"].file_sha256
    assert f["rag.chunking"].status == S.OBSERVED and f["rag.embedding"].status == S.OBSERVED
    assert f["rag.retrieval_evaluation"].status == S.OBSERVED
    assert f["generic.tests"].observation.startswith("2 test functions")


def test_rag_import_is_not_an_implementation(tmp_path: Path) -> None:
    f = by_key(inspect_repository(snapshot(tmp_path, repos.BETA_RAG_UNWIRED)))
    wired = f["rag.retrieval_on_request_path"]
    assert wired.status == S.INFERRED and "no entry point" in wired.observation
    assert f["rag.retrieval_evaluation"].status == S.MISSING
    assert f["generic.tests"].status == S.MISSING


def test_multi_agent_delegation_detected(tmp_path: Path) -> None:
    f = by_key(inspect_repository(snapshot(tmp_path, repos.GAMMA_MULTI_AGENT)))
    assert f["agentic.agent_definitions"].observation.startswith("2 agent definition")
    assert f["agentic.delegation"].status == S.OBSERVED
    assert f["agentic.tools"].status == S.OBSERVED
    assert f["agentic.state"].status == S.OBSERVED
    assert f["agentic.limits"].status == S.OBSERVED


def test_single_llm_call_is_not_multi_agent(tmp_path: Path) -> None:
    f = by_key(inspect_repository(snapshot(tmp_path, repos.ZETA_EMPTY_CI | {"requirements.txt": "langgraph\n"})))
    assert f["agentic.delegation"].status == S.MISSING


def test_ml_split_metrics_on_holdout_and_baseline(tmp_path: Path) -> None:
    f = by_key(inspect_repository(snapshot(tmp_path, repos.ML_CLASSIFIER)))
    for key in ("ml.training", "ml.data_split", "ml.metrics", "ml.baseline", "ml.metrics_on_holdout"):
        assert f[key].status == S.OBSERVED, key


def test_generic_fallback_for_unfamiliar_repo(tmp_path: Path) -> None:
    snap = snapshot(tmp_path, {"main.go": "package main\nfunc main() {}\n", "README.md": "# Go thing\n"})
    f = by_key(inspect_repository(snap))
    assert snap.profile.recommended_adapters == ["generic"]
    assert f["generic.readme"].status == S.OBSERVED
    assert not any(k.startswith(("rag.", "agentic.", "ml.")) for k in f)


def test_reachability_follows_callbacks_and_handlers(tmp_path: Path) -> None:
    idx = snapshot(
        tmp_path,
        {
            "ui.py": "import gradio as gr\n\ndef respond(q):\n    return helper(q)\n\ndef helper(q):\n    return q\n\n"
            "def unused():\n    return 1\n\ngr.Interface(fn=respond).launch()\n",
        },
    ).index
    live = idx.reachable()
    assert {"ui.py:respond", "ui.py:helper"} <= live
    assert "ui.py:unused" not in live


def test_hand_rolled_retrieval_is_rag_and_llm_sdk_is_not_agentic(tmp_path: Path) -> None:
    """Regressions found on the real demo cast: Alpha (pure-Python BM25) was 'generic';
    Beta (langchain + openai) was 'agentic'."""
    alpha = snapshot(
        tmp_path / "a",
        {
            "app/bm25.py": "class BM25:\n    def scores(self, q):\n        return []\n",
            "app/rag.py": "class Retriever:\n    def retrieve(self, q):\n        return []\n\ndef load_chunks():\n    return []\n",
        },
    )
    assert "rag" in alpha.profile.recommended_adapters and "custom-retrieval" in alpha.profile.technologies
    beta = snapshot(tmp_path / "b", {"requirements.txt": "langchain\nopenai\n", "app.py": "import openai\n"})
    assert "agentic" not in beta.profile.recommended_adapters
