"""Hybrid retrieval: event-scoped, measured with Recall@k / MRR on labelled queries (§11.3, §15.2)."""

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from judgebench.db import models as m
from judgebench.retrieval import HashingEmbedder, Piece, chunk_repository, evaluate, hybrid_search, index_pieces
from tests import fixtures_repos as repos
from tests.conftest import make_event, make_submission, make_team

pytestmark = pytest.mark.db
EMB = HashingEmbedder()

# (team, question a judge might ask, files that answer it)
LABELLED = [
    ("alpha", "where does retrieval happen on the request path?", {"app/rag.py"}),
    ("alpha", "how is recall@5 computed?", {"eval/eval_retrieval.py"}),
    ("alpha", "which endpoint answers questions", {"app/main.py"}),
    ("alpha", "chunk size for the text splitter", {"app/rag.py"}),
    ("alpha", "what tests exist for sources in answers", {"tests/test_rag.py"}),
    ("beta", "is the chroma retriever used by the streamlit app?", {"retriever.py", "app.py"}),
    ("beta", "what accuracy do they claim", {"README.md"}),
    ("gamma", "how do the researcher and writer agents hand off", {"graph.py"}),
    ("gamma", "is there a recursion limit", {"graph.py"}),
    ("ml", "how is the data split into train and test", {"train.py"}),
    ("ml", "is there a baseline model", {"train.py"}),
    ("zeta", "what does the CI workflow run", {".github/workflows/ci.yml"}),
]
CAST = {
    "alpha": repos.ALPHA_RAG,
    "beta": repos.BETA_RAG_UNWIRED,
    "gamma": repos.GAMMA_MULTI_AGENT,
    "ml": repos.ML_CLASSIFIER,
    "zeta": repos.ZETA_EMPTY_CI,
}


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body)
    return root


@pytest.fixture
def indexed(session: Session, tmp_path: Path) -> dict[str, m.Submission]:
    event = make_event(session, "bench")
    subs = {}
    for name, files in CAST.items():
        sub = make_submission(session, make_team(session, event, name), response_id=name)
        index_pieces(session, sub, chunk_repository(write(tmp_path / name, files)), EMB)
        subs[name] = sub
    session.commit()
    return subs


def test_recall_and_mrr_on_labelled_queries(session: Session, indexed: dict[str, m.Submission]) -> None:
    results = []
    for team, q, relevant in LABELLED:
        sub = indexed[team]
        hits = hybrid_search(session, [sub.event_id], q, EMB, submission_id=sub.id, k=5)
        results.append(([h.locator for h in hits], relevant))
    metrics = evaluate(results, k=3)
    print(
        f"\nretrieval (hashing embedder, {metrics.queries} queries): Recall@3={metrics.recall_at_k} MRR={metrics.mrr}"
    )
    # Lexical-only embedder: these are floors, not targets. Real numbers come from Vertex embeddings.
    assert metrics.recall_at_k >= 0.75 and metrics.mrr >= 0.6


def test_event_filter_is_applied_before_ranking(session: Session, indexed: dict[str, m.Submission]) -> None:
    other = make_event(session, "other")
    secret = make_submission(session, make_team(session, other, "spy"), response_id="spy")
    index_pieces(
        session,
        secret,
        [Piece("code", "secret.py#L1-L1", "similarity_search retrieval recall SECRET_OTHER_EVENT")],
        EMB,
    )
    session.commit()
    alpha = indexed["alpha"]
    hits = hybrid_search(session, [alpha.event_id], "similarity_search SECRET_OTHER_EVENT", EMB, k=50)
    assert hits and all(h.submission_id != secret.id for h in hits)
    assert hybrid_search(session, [], "anything", EMB) == []


def test_indexing_is_idempotent(session: Session, indexed: dict[str, m.Submission], tmp_path: Path) -> None:
    before = session.query(m.Chunk).count()
    index_pieces(session, indexed["alpha"], chunk_repository(write(tmp_path / "again", repos.ALPHA_RAG)), EMB)
    session.commit()
    assert session.query(m.Chunk).count() == before


def test_metric_arithmetic() -> None:
    mt = evaluate([(["a#1", "b#1"], {"b"}), (["x"], {"y"})], k=2)
    assert (mt.recall_at_k, mt.mrr) == (0.5, 0.25)
