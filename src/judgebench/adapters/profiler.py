"""Deterministic repository profiler (§7.2).

Reads files as text and parses Python with `ast.parse` only. Never imports, installs or runs
anything from the repository. README/docs prose is never used for classification: only code,
manifests and file layout count, so prompt-injection text can't steer adapter selection.
"""

import ast
import json
import os
import re
import tomllib
from collections import Counter, defaultdict
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

PROFILER_VERSION = "0.1.0"
RULES_VERSION = "2026-11-18"


class Category(StrEnum):
    ML = "traditional_ml"
    RAG = "llm_rag"
    AGENTIC = "agentic"
    VISION = "vision_multimodal"
    INFRA = "ai_infrastructure"


ADAPTER_FOR = {
    Category.ML: "ml",
    Category.RAG: "rag",
    Category.AGENTIC: "agentic",
    Category.VISION: "vision",
    Category.INFRA: "infra",
}
CATEGORY_THRESHOLD = 0.3

ML, RAG, AG, VIS, INF = Category.ML, Category.RAG, Category.AGENTIC, Category.VISION, Category.INFRA

# Normalised package/import name -> (technology, category weights). Versioned by RULES_VERSION.
# An import is evidence a library is present, not that it's used well (§7.4).
RULES: dict[str, tuple[str, dict[Category, float]]] = {
    "torch": ("pytorch", {ML: 0.3, VIS: 0.1}),
    "tensorflow": ("tensorflow", {ML: 0.3}),
    "keras": ("keras", {ML: 0.3}),
    "jax": ("jax", {ML: 0.3}),
    "sklearn": ("scikit-learn", {ML: 0.5}),
    "scikit_learn": ("scikit-learn", {ML: 0.5}),
    "xgboost": ("xgboost", {ML: 0.5}),
    "lightgbm": ("lightgbm", {ML: 0.5}),
    "catboost": ("catboost", {ML: 0.5}),
    "statsmodels": ("statsmodels", {ML: 0.3}),
    "prophet": ("prophet", {ML: 0.4}),
    "pandas": ("pandas", {ML: 0.1}),
    "transformers": ("hf-transformers", {ML: 0.2, RAG: 0.1}),
    "datasets": ("hf-datasets", {ML: 0.2}),
    "peft": ("peft", {ML: 0.3}),
    "langchain": ("langchain", {RAG: 0.3, AG: 0.2}),
    "llama_index": ("llamaindex", {RAG: 0.5}),
    "haystack": ("haystack", {RAG: 0.5}),
    "dspy": ("dspy", {RAG: 0.3, AG: 0.2}),
    "faiss": ("faiss", {RAG: 0.5}),
    "faiss_cpu": ("faiss", {RAG: 0.5}),
    "faiss_gpu": ("faiss", {RAG: 0.5}),
    "chromadb": ("chroma", {RAG: 0.5}),
    "pinecone": ("pinecone", {RAG: 0.5}),
    "pinecone_client": ("pinecone", {RAG: 0.5}),
    "weaviate": ("weaviate", {RAG: 0.5}),
    "weaviate_client": ("weaviate", {RAG: 0.5}),
    "qdrant_client": ("qdrant", {RAG: 0.5}),
    "pgvector": ("pgvector", {RAG: 0.5}),
    "pymilvus": ("milvus", {RAG: 0.5}),
    "lancedb": ("lancedb", {RAG: 0.5}),
    "sentence_transformers": ("sentence-transformers", {RAG: 0.3}),
    "rank_bm25": ("bm25", {RAG: 0.3}),
    "cohere": ("cohere", {RAG: 0.1}),
    "openai": ("openai", {RAG: 0.1, AG: 0.1}),
    "anthropic": ("anthropic", {RAG: 0.1, AG: 0.1}),
    "google_genai": ("gemini", {RAG: 0.1, AG: 0.1}),
    "google_generativeai": ("gemini", {RAG: 0.1, AG: 0.1}),
    "vertexai": ("vertex-ai", {RAG: 0.1, AG: 0.1}),
    "ollama": ("ollama", {RAG: 0.1}),
    "langgraph": ("langgraph", {AG: 0.6}),
    "crewai": ("crewai", {AG: 0.6}),
    "autogen": ("autogen", {AG: 0.6}),
    "pyautogen": ("autogen", {AG: 0.6}),
    "autogen_agentchat": ("autogen", {AG: 0.6}),
    "google_adk": ("google-adk", {AG: 0.6}),
    "smolagents": ("smolagents", {AG: 0.6}),
    "openai_agents": ("openai-agents", {AG: 0.6}),
    "mcp": ("mcp", {AG: 0.3}),
    "letta": ("letta", {AG: 0.5}),
    "cv2": ("opencv", {VIS: 0.5}),
    "opencv_python": ("opencv", {VIS: 0.5}),
    "opencv_python_headless": ("opencv", {VIS: 0.5}),
    "torchvision": ("torchvision", {VIS: 0.4}),
    "ultralytics": ("ultralytics", {VIS: 0.6}),
    "timm": ("timm", {VIS: 0.3}),
    "mediapipe": ("mediapipe", {VIS: 0.5}),
    "whisper": ("whisper", {VIS: 0.4}),
    "openai_whisper": ("whisper", {VIS: 0.4}),
    "pytesseract": ("tesseract", {VIS: 0.4}),
    "easyocr": ("easyocr", {VIS: 0.5}),
    "pil": ("pillow", {VIS: 0.1}),
    "pillow": ("pillow", {VIS: 0.1}),
    "librosa": ("librosa", {VIS: 0.3}),
    "vllm": ("vllm", {INF: 0.6}),
    "tritonclient": ("triton", {INF: 0.5}),
    "bentoml": ("bentoml", {INF: 0.5}),
    "ray": ("ray", {INF: 0.3}),
    "onnxruntime": ("onnxruntime", {INF: 0.3}),
    "mlflow": ("mlflow", {INF: 0.3}),
    "locust": ("locust", {INF: 0.3}),
    "tensorrt": ("tensorrt", {INF: 0.5}),
    "fastapi": ("fastapi", {}),
    "flask": ("flask", {}),
    "django": ("django", {}),
    "streamlit": ("streamlit", {}),
    "gradio": ("gradio", {}),
    # npm (scope stripped, see _norm)
    "tfjs": ("tensorflow", {ML: 0.3}),
    "genai": ("gemini", {RAG: 0.1, AG: 0.1}),
    "generative_ai": ("gemini", {RAG: 0.1, AG: 0.1}),
    "ai": ("vercel-ai-sdk", {RAG: 0.1, AG: 0.1}),
}

LANGUAGES = {
    ".py": "Python",
    ".ipynb": "Jupyter",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".cpp": "C++",
    ".cc": "C++",
    ".hpp": "C++",
    ".c": "C",
    ".h": "C",
    ".cs": "C#",
    ".rb": "Ruby",
    ".php": "PHP",
    ".swift": "Swift",
    ".dart": "Dart",
    ".r": "R",
    ".sql": "SQL",
    ".sh": "Shell",
    ".html": "HTML",
    ".css": "CSS",
}
MANIFESTS = {
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "pipfile",
    "package.json",
    "environment.yml",
    "environment.yaml",
    "go.mod",
    "cargo.toml",
    "pom.xml",
    "build.gradle",
}
LOCK_FILES = {
    "poetry.lock",
    "uv.lock",
    "pipfile.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lockb",
    "conda-lock.yml",
    "requirements.lock",
    "cargo.lock",
    "go.sum",
}
DEPLOY = {
    "app.yaml",
    "cloudbuild.yaml",
    "vercel.json",
    "netlify.toml",
    "fly.toml",
    "render.yaml",
    "procfile",
    "serverless.yml",
    "railway.json",
    "railway.toml",
}
ENTRY_NAMES = {
    "main.py",
    "app.py",
    "server.py",
    "manage.py",
    "streamlit_app.py",
    "index.js",
    "server.js",
    "main.ts",
    "index.ts",
    "main.go",
}
MODEL_EXT = {".pt", ".pth", ".onnx", ".safetensors", ".h5", ".ckpt", ".pkl", ".joblib", ".gguf"}
DATA_EXT = {".csv", ".parquet", ".jsonl", ".tsv", ".arrow"}
DIR_ROLES = {
    "training": {"train", "training"},
    "inference": {"infer", "inference", "serve", "serving"},
    "evaluation": {"eval", "evals", "evaluation", "benchmark", "benchmarks"},
    "tests": {"test", "tests", "__tests__", "spec"},
}
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "site-packages", "__pycache__", "dist", "build", ".next"}
MAX_PARSE_BYTES = 1024 * 1024
MAX_PARSED_FILES = 3000
_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._\-]*)")


class RepositoryProfile(BaseModel):
    profiler_version: str = PROFILER_VERSION
    rules_version: str = RULES_VERSION
    commit_sha: str
    file_count: int
    total_bytes: int
    languages: dict[str, int]
    manifests: list[str]
    lock_files: list[str]
    readme: str | None
    entry_points: list[str]
    directories: dict[str, list[str]]
    dockerfiles: list[str]
    deploy_manifests: list[str]
    ci_configs: list[str]
    model_refs: list[str]
    dataset_refs: list[str]
    technologies: list[str]
    categories: dict[Category, float]
    recommended_adapters: list[str]
    confidence: float
    missing: list[str]
    limitations: list[str]


def _norm(name: str) -> str:
    name = name.strip().lower()
    if name.startswith("@"):  # npm scope: "@langchain/core" -> "langchain", "@google/genai" -> "genai"
        scope, _, pkg = name[1:].partition("/")
        name = scope if scope not in {"google", "tensorflow", "types"} else pkg
    return re.sub(r"[-.]", "_", name.split("[")[0])


def _read(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_PARSE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _parse(source: str) -> ast.Module | None:
    try:
        return ast.parse(source)  # parse only; nothing is compiled or executed
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None


def _imports(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        mods: list[str] = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods = [node.module]
        for mod in mods:
            parts = mod.split(".")
            names.add(_norm(parts[0]))
            names.add(_norm("_".join(parts[:2])))  # google.adk -> google_adk
    return names


def _string_calls(tree: ast.Module, funcs: set[str]) -> list[str]:
    """First string-constant argument of calls like from_pretrained("bert-base")."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name in funcs and isinstance(node.args[0].value, str):
                found.append(node.args[0].value[:200])
    return found


def _has_main_guard(tree: ast.Module) -> bool:
    return any(
        isinstance(n, ast.If)
        and isinstance(n.test, ast.Compare)
        and isinstance(n.test.left, ast.Name)
        and n.test.left.id == "__name__"
        for n in tree.body
    )


def _manifest_deps(path: Path, text: str) -> set[str]:
    name = path.name.lower()
    deps: set[str] = set()
    try:
        if name.startswith("requirements") and name.endswith(".txt"):
            deps = {
                m.group(1)
                for line in text.splitlines()
                if not line.lstrip().startswith(("#", "-")) and (m := _REQ_NAME.match(line))
            }
        elif name == "pyproject.toml":
            data = tomllib.loads(text)
            project = data.get("project", {})
            reqs = list(project.get("dependencies", []))
            for extra in project.get("optional-dependencies", {}).values():
                reqs += extra
            deps = {m.group(1) for r in reqs if isinstance(r, str) and (m := _REQ_NAME.match(r))}
            deps |= set(data.get("tool", {}).get("poetry", {}).get("dependencies", {}))
        elif name == "package.json":
            data = json.loads(text)
            for key in ("dependencies", "devDependencies"):
                deps |= set(data.get(key, {}) or {})
        elif name == "setup.py" and (tree := _parse(text)):
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.keyword)
                    and node.arg == "install_requires"
                    and isinstance(node.value, ast.List | ast.Tuple)
                ):
                    deps |= {
                        m.group(1)
                        for e in node.value.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str) and (m := _REQ_NAME.match(e.value))
                    }
    except (ValueError, TypeError, AttributeError, tomllib.TOMLDecodeError):
        return set()
    return {_norm(d) for d in deps}


def _notebook_source(text: str) -> str:
    try:
        cells = json.loads(text).get("cells", [])
        lines = []
        for cell in cells:
            if cell.get("cell_type") == "code":
                src = cell.get("source", "")
                src = "".join(src) if isinstance(src, list) else str(src)
                lines += [ln for ln in src.splitlines() if not ln.lstrip().startswith(("%", "!"))]
        return "\n".join(lines)
    except (ValueError, AttributeError, TypeError):
        return ""


def profile_repository(root: Path, *, commit_sha: str, limitations: list[str]) -> RepositoryProfile:
    files: list[Path] = []
    file_count = total_bytes = 0
    for dirpath, dirnames, filenames in os.walk(root):  # os.walk never follows symlinks by default
        dirnames[:] = sorted(d for d in dirnames)
        for fname in filenames:
            p = Path(dirpath) / fname
            file_count += 1
            total_bytes += p.stat().st_size
            if not SKIP_DIRS.intersection(p.relative_to(root).parts[:-1]):
                files.append(p)
    files.sort()
    limits = list(limitations)

    rel = {p: p.relative_to(root).as_posix() for p in files}
    languages = Counter(LANGUAGES[p.suffix.lower()] for p in files if p.suffix.lower() in LANGUAGES)
    manifests, locks, dockerfiles, deploy, ci, entry, models, datasets = ([] for _ in range(8))
    directories: dict[str, set[str]] = {role: set() for role in DIR_ROLES}
    readme: str | None = None
    names: set[str] = set()
    parsed = 0

    for p in files:
        r, low = rel[p], p.name.lower()
        parts = [x.lower() for x in p.relative_to(root).parts]
        if low in MANIFESTS or (low.startswith("requirements") and low.endswith(".txt")):
            manifests.append(r)
            if (text := _read(p)) is not None:
                names |= _manifest_deps(p, text)
        if low in LOCK_FILES:
            locks.append(r)
        if low.startswith("dockerfile") or low in {
            "docker-compose.yml",
            "docker-compose.yaml",
            "compose.yml",
            "compose.yaml",
        }:
            dockerfiles.append(r)
        if low in DEPLOY:
            deploy.append(r)
        if (
            (parts[:2] == [".github", "workflows"] and p.suffix in {".yml", ".yaml"})
            or low == ".gitlab-ci.yml"
            or parts[:1] == [".circleci"]
        ):
            ci.append(r)
        if readme is None and low.startswith("readme") and len(parts) == 1:
            readme = r
        if p.suffix.lower() in MODEL_EXT:
            models.append(r)
        if p.suffix.lower() in DATA_EXT:
            datasets.append(r)
        for role, dirs in DIR_ROLES.items():
            hit = next((i for i, part in enumerate(parts[:-1]) if part in dirs), None)
            if hit is not None:
                directories[role].add("/".join(p.relative_to(root).parts[: hit + 1]))
        stem = p.stem.lower()
        if p.suffix == ".py" and (stem.startswith("test_") or stem.endswith("_test")):
            directories["tests"].add(r)
        if p.suffix == ".py" and stem.startswith(("eval", "evaluate", "benchmark")):
            directories["evaluation"].add(r)
        if p.suffix == ".py" and stem.startswith("train"):
            directories["training"].add(r)
        if low in ENTRY_NAMES:
            entry.append(r)

        if p.suffix.lower() in {".py", ".ipynb"}:
            if parsed >= MAX_PARSED_FILES:
                continue
            text = _read(p)
            if text is None:
                continue
            tree = _parse(_notebook_source(text) if p.suffix.lower() == ".ipynb" else text)
            parsed += 1
            if tree is None:
                continue
            names |= _imports(tree)
            models += _string_calls(tree, {"from_pretrained", "SentenceTransformer", "pipeline"})
            datasets += _string_calls(tree, {"load_dataset"})
            if p.suffix == ".py" and r not in entry and _has_main_guard(tree):
                entry.append(r)
    if parsed >= MAX_PARSED_FILES:
        limits.append(f"only the first {MAX_PARSED_FILES} Python/notebook files were parsed")

    scores: defaultdict[Category, float] = defaultdict(float)
    technologies: set[str] = set()
    for name in names:
        if name in RULES:
            tech, weights = RULES[name]
            technologies.add(tech)
            for cat, w in weights.items():
                scores[cat] += w
    # Layout signals: weak on purpose.
    if directories["training"]:
        scores[ML] += 0.2
    if dockerfiles and technologies & {"vllm", "triton", "bentoml", "tensorrt", "onnxruntime"}:
        scores[INF] += 0.2

    categories = {c: round(min(1.0, s), 2) for c, s in sorted(scores.items()) if s >= CATEGORY_THRESHOLD}
    adapters = sorted(ADAPTER_FOR[c] for c in categories) or ["generic"]
    if file_count >= 20_000 or any("file count" in lim for lim in limits):
        limits.append("file count cap reached; profile covers extracted files only")

    missing = [
        label
        for label, present in [
            ("readme", readme),
            ("dependency_manifest", manifests),
            ("lock_file", locks),
            ("tests", directories["tests"]),
            ("ci_config", ci),
            ("evaluation", directories["evaluation"]),
        ]
        if not present
    ]

    return RepositoryProfile(
        commit_sha=commit_sha,
        file_count=file_count,
        total_bytes=total_bytes,
        languages=dict(sorted(languages.items())),
        manifests=manifests,
        lock_files=locks,
        readme=readme,
        entry_points=entry[:50],
        directories={k: sorted(v)[:50] for k, v in directories.items()},
        dockerfiles=dockerfiles,
        deploy_manifests=deploy,
        ci_configs=ci,
        model_refs=sorted(set(models))[:50],
        dataset_refs=sorted(set(datasets))[:50],
        technologies=sorted(technologies),
        categories=categories,
        recommended_adapters=adapters,
        confidence=round(max(categories.values(), default=0.0), 2),
        missing=missing,
        limitations=limits,
    )
