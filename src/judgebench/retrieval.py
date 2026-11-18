"""Hybrid retrieval over a submission's code, README and evidence (§11.3).

  1. Scope filter first: WHERE event_id = ANY(:events) [AND submission_id = :sid]  (§11.4)
  2. Full-text: to_tsvector('simple') with an OR query (exact identifiers, no stemming)
  3. Vector: pgvector cosine distance (HNSW index)
  4. Fuse with reciprocal-rank fusion (k=60). No reranker until retrieval metrics justify one.

The event filter is in the same SQL as the ranking, so nothing outside a judge's events can be
ranked, let alone reach a prompt. Embeddings: Vertex AI in production, a deterministic hashing
embedder in tests (no credentials, no network).
"""

import hashlib
import math
import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from judgebench.adapters.profiler import SKIP_DIRS
from judgebench.db.models import EMBED_DIM, Chunk, Submission

CODE_SUFFIXES = {".py", ".ipynb", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt", ".yaml", ".yml", ".toml"}
DOC_SUFFIXES = {".md", ".rst", ".txt"}
WINDOW, OVERLAP, MAX_CHUNKS, RRF_K = 40, 10, 400, 60
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,40}")


class Embedder(Protocol):
    model: str

    def embed(self, texts: Sequence[str], *, query: bool) -> list[list[float]]: ...


class HashingEmbedder:
    """Deterministic bag-of-tokens feature hashing. Lexical only: a test double, not a semantic model."""

    model = "hashing-768"

    def embed(self, texts: Sequence[str], *, query: bool) -> list[list[float]]:
        out = []
        for t in texts:
            v = [0.0] * EMBED_DIM
            for tok in _tokens(t):
                h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "big")
                v[h % EMBED_DIM] += 1.0 if (h >> 32) & 1 else -1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


class VertexEmbedder:
    def __init__(self, project: str, location: str, model: str) -> None:
        from google import genai

        self._client = genai.Client(vertexai=True, project=project, location=location)
        self.model = model

    def embed(self, texts: Sequence[str], *, query: bool) -> list[list[float]]:
        from google.genai import types

        out: list[list[float]] = []
        for i in range(0, len(texts), 50):
            r = self._client.models.embed_content(
                model=self.model,
                contents=list(texts[i : i + 50]),
                config=types.EmbedContentConfig(
                    task_type="RETRIEVAL_QUERY" if query else "RETRIEVAL_DOCUMENT", output_dimensionality=EMBED_DIM
                ),
            )
            out += [list(e.values or []) for e in (r.embeddings or [])]
        return out


def default_embedder() -> Embedder:
    from judgebench.config import settings

    cfg = settings()
    if cfg.google_cloud_project:
        return VertexEmbedder(cfg.google_cloud_project, cfg.google_cloud_location, cfg.embedding_model)
    import logging

    logging.getLogger(__name__).warning("GOOGLE_CLOUD_PROJECT unset: using the lexical hashing embedder")
    return HashingEmbedder()


def _tokens(t: str) -> list[str]:
    """Identifiers split on snake/camel case, lower-cased: 'similarity_search' -> similarity, search."""
    toks = []
    for word in _TOKEN.findall(t):
        toks.append(word.lower())
        toks += [
            p.lower() for p in re.split(r"_|(?<=[a-z])(?=[A-Z])", word) if len(p) > 1 and p.lower() != word.lower()
        ]
    return toks


@dataclass(frozen=True)
class Piece:
    source_type: str
    locator: str
    content: str


def chunk_repository(root: Path) -> list[Piece]:
    """Code: 40-line windows, 10 overlap. Docs: blank-line sections up to ~1.5k chars."""
    pieces: list[Piece] = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if not p.is_file() or p.is_symlink() or SKIP_DIRS.intersection(rel.parts[:-1]) or p.stat().st_size > 500_000:
            continue
        suffix = p.suffix.lower()
        text_ = p.read_text(errors="replace")
        if suffix in CODE_SUFFIXES:
            lines = text_.splitlines()
            for start in range(0, max(len(lines), 1), WINDOW - OVERLAP):
                body = "\n".join(lines[start : start + WINDOW]).strip()
                if body:
                    end = min(start + WINDOW, len(lines))
                    pieces.append(Piece("code", f"{rel.as_posix()}#L{start + 1}-L{end}", f"{rel.as_posix()}\n{body}"))
                if start + WINDOW >= len(lines):
                    break
        elif suffix in DOC_SUFFIXES or p.name.lower().startswith("readme"):
            buf, n = "", 0
            for para in re.split(r"\n\s*\n", text_):
                if len(buf) + len(para) > 1500 and buf:
                    n += 1
                    pieces.append(Piece("documentation", f"{rel.as_posix()}#{n}", buf.strip()))
                    buf = ""
                buf += para + "\n\n"
            if buf.strip():
                pieces.append(Piece("documentation", f"{rel.as_posix()}#{n + 1}", buf.strip()))
        if len(pieces) >= MAX_CHUNKS:
            break
    return pieces[:MAX_CHUNKS]


def index_pieces(session: Session, sub: Submission, pieces: Iterable[Piece], embedder: Embedder) -> int:
    """Embed + insert; idempotent on (submission, locator, content hash). Caller commits."""
    items = list(pieces)
    if not items:
        return 0
    vectors = embedder.embed([p.content[:8000] for p in items], query=False)
    for p, vec in zip(items, vectors, strict=True):
        session.execute(
            insert(Chunk)
            .values(
                id=uuid.uuid4(),
                tenant_id=sub.tenant_id,
                event_id=sub.event_id,
                submission_id=sub.id,
                source_type=p.source_type,
                locator=p.locator[:500],
                content=p.content[:8000],
                content_sha256=hashlib.sha256(p.content.encode()).hexdigest(),
                embedding=vec,
                embedding_model=embedder.model,
            )
            .on_conflict_do_nothing(index_elements=["submission_id", "locator", "content_sha256"])
        )
    return len(items)


@dataclass(frozen=True)
class Hit:
    chunk_id: uuid.UUID
    submission_id: uuid.UUID
    source_type: str
    locator: str
    content: str
    score: float


def hybrid_search(
    session: Session,
    event_ids: Iterable[uuid.UUID],
    query: str,
    embedder: Embedder,
    *,
    submission_id: uuid.UUID | None = None,
    k: int = 8,
) -> list[Hit]:
    events = [str(e) for e in event_ids]
    if not events or not query.strip():
        return []
    terms = sorted(set(_tokens(query)))[:30]
    tsq = " | ".join(terms) or "zzzz_no_terms"
    [qvec] = embedder.embed([query], query=True)
    rows = session.execute(
        text("""
        WITH scoped AS (
            SELECT id FROM chunks
            WHERE event_id = ANY(CAST(:events AS uuid[]))
              AND (CAST(:sid AS uuid) IS NULL OR submission_id = CAST(:sid AS uuid))
        ),
        fts AS (
            SELECT c.id, row_number() OVER (ORDER BY ts_rank_cd(c.tsv, q) DESC) AS r
            FROM chunks c JOIN scoped s ON s.id = c.id, to_tsquery('simple', :tsq) q
            WHERE c.tsv @@ q ORDER BY ts_rank_cd(c.tsv, q) DESC LIMIT 50
        ),
        vec AS (
            SELECT c.id, row_number() OVER (ORDER BY c.embedding <=> CAST(:qvec AS vector)) AS r
            FROM chunks c JOIN scoped s ON s.id = c.id
            ORDER BY c.embedding <=> CAST(:qvec AS vector) LIMIT 50
        ),
        fused AS (
            SELECT id, sum(1.0 / (:rrf + r)) AS score FROM (SELECT * FROM fts UNION ALL SELECT * FROM vec) u GROUP BY id
        )
        SELECT c.id, c.submission_id, c.source_type, c.locator, c.content, f.score
        FROM fused f JOIN chunks c ON c.id = f.id
        ORDER BY f.score DESC, c.locator LIMIT :k
    """),
        {
            "events": events,
            "sid": str(submission_id) if submission_id else None,
            "tsq": tsq,
            "qvec": str(qvec),
            "rrf": RRF_K,
            "k": k,
        },
    ).all()
    return [Hit(r.id, r.submission_id, r.source_type, r.locator, r.content, float(r.score)) for r in rows]


# --- retrieval evaluation (§15.2) ------------------------------------------------------


@dataclass(frozen=True)
class Metrics:
    queries: int
    recall_at_k: float
    mrr: float
    k: int


def evaluate(results: Sequence[tuple[Sequence[str], set[str]]], k: int) -> Metrics:
    """results: (ranked locators, relevant path prefixes) per query. A hit is relevant if its locator
    starts with one of the expected paths. Recall@k is per query (any relevant in top k), averaged."""
    recall = mrr = 0.0
    for ranked, relevant in results:
        ranks = [i for i, loc in enumerate(ranked[:k], 1) if any(loc.startswith(r) for r in relevant)]
        recall += 1.0 if ranks else 0.0
        mrr += 1.0 / ranks[0] if ranks else 0.0
    n = max(len(results), 1)
    return Metrics(len(results), round(recall / n, 3), round(mrr / n, 3), k)
