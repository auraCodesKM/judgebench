"""RAG, agentic, ML and generic inspectors (§7.3 initial adapters). Name lists are versioned with
ADAPTER_VERSION. Each check reports what the code shows, not what the README says, and separates
"present" from "wired into a path that runs" (an import is not an implementation, §7.4)."""

from judgebench.adapters.base import Adapter, Snapshot, locs, on_path
from judgebench.evidence import EvidenceStatus, Finding, SourceType

MISSING, INFERRED = EvidenceStatus.MISSING, EvidenceStatus.INFERRED

EMBED = {
    "embed_documents",
    "embed_query",
    "embed_content",
    "encode",
    "OpenAIEmbeddings",
    "HuggingFaceEmbeddings",
    "GoogleGenerativeAIEmbeddings",
    "VertexAIEmbeddings",
    "SentenceTransformer",
    "get_embeddings",
    "TextEmbeddingModel",
    "CohereEmbeddings",
    "OllamaEmbeddings",
    "embed",
}
CHUNK = {
    "RecursiveCharacterTextSplitter",
    "CharacterTextSplitter",
    "TokenTextSplitter",
    "MarkdownTextSplitter",
    "SentenceSplitter",
    "split_documents",
    "split_text",
    "create_documents",
    "chunk_text",
}
STORE = {
    "FAISS",
    "Chroma",
    "PineconeVectorStore",
    "QdrantClient",
    "Qdrant",
    "PGVector",
    "Weaviate",
    "Milvus",
    "LanceDB",
    "VectorStoreIndex",
    "from_documents",
    "from_texts",
    "add_documents",
    "add_texts",
    "upsert",
    "create_collection",
    "get_or_create_collection",
    "IndexFlatL2",
    "IndexFlatIP",
}
RETRIEVE = {
    "similarity_search",
    "similarity_search_with_score",
    "max_marginal_relevance_search",
    "as_retriever",
    "get_relevant_documents",
    "retrieve",
    "as_query_engine",
    "search_vectors",
    "query_points",
    "knn_search",
}
RERANK = {"CrossEncoder", "rerank", "CohereRerank", "ContextualCompressionRetriever", "FlashrankRerank"}
RAG_EVAL_NAMES = {
    "ragas",
    "trulens",
    "deepeval",
    "recall_at_k",
    "mrr",
    "ndcg",
    "faithfulness",
    "context_precision",
    "context_recall",
    "answer_relevancy",
    "hit_rate",
}

AGENT = {
    "Agent",
    "LlmAgent",
    "AssistantAgent",
    "ConversableAgent",
    "UserProxyAgent",
    "create_react_agent",
    "initialize_agent",
    "AgentExecutor",
    "create_tool_calling_agent",
    "CodeAgent",
    "ToolCallingAgent",
    "ReActAgent",
    "FunctionCallingAgent",
    "SequentialAgent",
    "ParallelAgent",
    "LoopAgent",
}
ORCHESTRATION = {
    "StateGraph",
    "Graph",
    "Crew",
    "GroupChat",
    "GroupChatManager",
    "add_conditional_edges",
    "add_edge",
    "Swarm",
    "transfer_to_agent",
    "handoff",
}
DELEGATION_KW = {"sub_agents", "handoffs", "agents", "tasks", "transfer"}
TOOL_CALLS = {"Tool", "StructuredTool", "FunctionTool", "tool", "bind_tools", "AgentTool"}
STATE = {
    "MemorySaver",
    "SqliteSaver",
    "PostgresSaver",
    "InMemorySessionService",
    "DatabaseSessionService",
    "checkpointer",
    "ConversationBufferMemory",
}
LIMIT_KW = {
    "max_iterations",
    "recursion_limit",
    "max_retries",
    "max_turns",
    "max_llm_calls",
    "max_consecutive_auto_reply",
}

TRAIN = {"fit", "train", "fit_transform", "Trainer", "backward", "train_on_batch", "fit_generator"}
SPLIT = {
    "train_test_split",
    "KFold",
    "StratifiedKFold",
    "GroupKFold",
    "TimeSeriesSplit",
    "random_split",
    "cross_val_score",
    "cross_validate",
    "ShuffleSplit",
}
METRIC = {
    "accuracy_score",
    "f1_score",
    "precision_score",
    "recall_score",
    "roc_auc_score",
    "classification_report",
    "confusion_matrix",
    "mean_squared_error",
    "mean_absolute_error",
    "r2_score",
    "log_loss",
    "evaluate",
    "precision_recall_fscore_support",
    "average_precision_score",
}
BASELINE = {"DummyClassifier", "DummyRegressor", "LogisticRegression", "baseline"}


class RagAdapter:
    name = "rag"

    def inspect(self, snap: Snapshot) -> list[Finding]:
        out: list[Finding] = []
        crit = "ai_technical_depth"
        for key, names, label in [
            ("rag.chunking", CHUNK, "Chunking"),
            ("rag.embedding", EMBED, "Embedding calls"),
            ("rag.vector_store", STORE, "Vector store writes/index"),
            ("rag.reranking", RERANK, "Reranking"),
        ]:
            sites = snap.index.find(names)
            if sites:
                out.append(snap.code_finding(key, f"{label} found at {locs(sites)}", sites[0], criterion=crit))
            elif key != "rag.reranking":
                out.append(snap.missing(key, f"No {label.lower()} found in code", criterion=crit))
        retrieval = snap.index.find(RETRIEVE)
        live, dead = on_path(snap, retrieval)
        if live:
            out.append(
                snap.code_finding(
                    "rag.retrieval_on_request_path",
                    f"Retrieval is called on a path reachable from an entry point: {locs(live)}",
                    live[0],
                    criterion=crit,
                    confidence=0.75,
                )
            )
        elif dead:
            out.append(
                snap.code_finding(
                    "rag.retrieval_on_request_path",
                    f"Retrieval code exists ({locs(dead)}) but no entry point or request handler reaches it",
                    dead[0],
                    status=INFERRED,
                    criterion=crit,
                    confidence=0.6,
                )
            )
        else:
            out.append(snap.missing("rag.retrieval_on_request_path", "No retrieval call found", criterion=crit))
        evals = snap.index.find(RAG_EVAL_NAMES) + snap.index.find_defs(RAG_EVAL_NAMES)
        eval_dirs = snap.profile.directories.get("evaluation", [])
        if evals:
            out.append(
                snap.code_finding(
                    "rag.retrieval_evaluation",
                    f"Retrieval/answer evaluation code: {locs(evals)}",
                    evals[0],
                    criterion="model_quality",
                )
            )
        else:
            out.append(
                snap.missing(
                    "rag.retrieval_evaluation",
                    "No retrieval evaluation (recall@k, MRR, faithfulness, ragas...) found"
                    + (f"; evaluation paths exist: {', '.join(eval_dirs[:3])}" if eval_dirs else ""),
                    criterion="model_quality",
                )
            )
        return out


class AgenticAdapter:
    name = "agentic"

    def inspect(self, snap: Snapshot) -> list[Finding]:
        out: list[Finding] = []
        crit = "ai_technical_depth"
        agents = snap.index.find(AGENT)
        if agents:
            out.append(
                snap.code_finding(
                    "agentic.agent_definitions",
                    f"{len(agents)} agent definition(s): {locs(agents)}",
                    agents[0],
                    criterion=crit,
                )
            )
        else:
            out.append(
                snap.missing("agentic.agent_definitions", "No agent framework constructors found", criterion=crit)
            )
        tools = [(t.path, t.line) for t in snap.index.find(TOOL_CALLS)]
        tools += [(k[0], k[1]) for k in snap.index.find_keywords({"tools"})]
        tools += [(d[0], d[1]) for d in snap.index.decorated if d[3].split("(")[0].endswith("tool")]
        if tools:
            out.append(
                snap.code_finding(
                    "agentic.tools",
                    f"{len(tools)} tool definitions/bindings",
                    (tools[0][0], tools[0][1], tools[0][1]),
                    criterion=crit,
                )
            )
        else:
            out.append(snap.missing("agentic.tools", "No tool definitions found", criterion=crit))
        orch = snap.index.find(ORCHESTRATION)
        deleg_kw = snap.index.find_keywords(DELEGATION_KW)
        multi = len(agents) >= 2 and (orch or deleg_kw)
        if multi:
            site = orch[0] if orch else (deleg_kw[0][0], deleg_kw[0][1], deleg_kw[0][1])
            out.append(
                snap.code_finding(
                    "agentic.delegation",
                    f"Multiple agents with delegation/orchestration ({len(agents)} agents; "
                    f"{len(orch)} graph/crew calls, {len(deleg_kw)} delegation keywords)",
                    site,
                    criterion=crit,
                )
            )
        else:
            out.append(
                snap.missing(
                    "agentic.delegation",
                    f"No multi-agent delegation found ({len(agents)} agent definition(s), no orchestration)",
                    criterion=crit,
                )
            )
        state = snap.index.find(STATE)
        if state:
            out.append(
                snap.code_finding(
                    "agentic.state", f"Agent state/memory: {locs(state)}", state[0], criterion="engineering"
                )
            )
        limits = snap.index.find_keywords(LIMIT_KW)
        if limits:
            out.append(
                snap.code_finding(
                    "agentic.limits",
                    f"Iteration/retry limits set ({', '.join(sorted({k[3] for k in limits}))})",
                    (limits[0][0], limits[0][1], limits[0][1]),
                    criterion="engineering",
                )
            )
        else:
            out.append(
                snap.missing("agentic.limits", "No max-iteration / retry limits configured", criterion="engineering")
            )
        return out


class MlAdapter:
    name = "ml"

    def inspect(self, snap: Snapshot) -> list[Finding]:
        out: list[Finding] = []
        for key, names, label, crit in [
            ("ml.training", TRAIN, "Training code", "ai_technical_depth"),
            ("ml.data_split", SPLIT, "Train/validation/test split", "model_quality"),
            ("ml.metrics", METRIC, "Metric computation", "model_quality"),
            ("ml.baseline", BASELINE, "Baseline model", "model_quality"),
        ]:
            sites = snap.index.find(names)
            if sites:
                out.append(snap.code_finding(key, f"{label}: {locs(sites)}", sites[0], criterion=crit))
            else:
                out.append(snap.missing(key, f"No {label.lower()} found", criterion=crit))
        held_out = [s for s in snap.index.find(METRIC) if "test" in s.text.lower() or "val" in s.text.lower()]
        if held_out:
            out.append(
                snap.code_finding(
                    "ml.metrics_on_holdout",
                    f"Metrics computed on held-out data: {locs(held_out)}",
                    held_out[0],
                    criterion="model_quality",
                    confidence=0.65,
                )
            )
        elif snap.index.find(METRIC):
            out.append(
                snap.missing(
                    "ml.metrics_on_holdout",
                    "Metrics computed, but not visibly on a test/validation split",
                    criterion="model_quality",
                ).model_copy(update={"status": INFERRED})
            )
        return out


class GenericAdapter:
    name = "generic"

    def inspect(self, snap: Snapshot) -> list[Finding]:
        p, out = snap.profile, []
        live = snap.index.reachable()
        out.append(
            snap.code_finding(
                "generic.entry_points",
                f"Entry points: {', '.join(p.entry_points[:5])}",
                (p.entry_points[0], 1, 1),
                criterion="engineering",
            )
            if p.entry_points
            else snap.missing("generic.entry_points", "No entry point found", criterion="engineering")
        )
        if snap.index.test_functions:
            out.append(
                Finding(
                    key="generic.tests",
                    observation=f"{snap.index.test_functions} test functions in "
                    f"{', '.join(p.directories.get('tests', [])[:3]) or 'repository'}",
                    source_type=SourceType.CODE,
                    source_locator="tests",
                    status=EvidenceStatus.OBSERVED,
                    confidence=0.9,
                    verification_method="static_analysis",
                    criterion="engineering",
                )
            )
        else:
            out.append(snap.missing("generic.tests", "No test functions found", criterion="engineering"))
        out.append(
            Finding(
                key="generic.readme",
                observation=f"README present ({p.readme})" if p.readme else "No README",
                source_type=SourceType.DOCUMENTATION,
                source_locator=p.readme or "repository",
                status=EvidenceStatus.OBSERVED if p.readme else MISSING,
                confidence=0.95,
                verification_method="repository_inspection",
                criterion="presentation",
            )
        )
        for key, items, label in [
            ("generic.ci_config", p.ci_configs, "CI configuration"),
            ("generic.container", p.dockerfiles, "Container/deploy config"),
            ("generic.lock_file", p.lock_files, "Dependency lock file"),
        ]:
            out.append(
                Finding(
                    key=key,
                    observation=f"{label}: {', '.join(items[:3])}" if items else f"No {label.lower()}",
                    source_type=SourceType.CODE,
                    source_locator=items[0] if items else "repository",
                    status=EvidenceStatus.OBSERVED if items else MISSING,
                    confidence=0.95,
                    verification_method="repository_inspection",
                    criterion="engineering",
                )
            )
        handlers = sum(1 for q in snap.index.roots if not q.endswith(":<module>"))
        if handlers:
            out.append(
                Finding(
                    key="generic.api_routes",
                    observation=f"{handlers} request/tool handlers; {len(live)} functions reachable from entry points",
                    source_type=SourceType.CODE,
                    source_locator="repository",
                    status=EvidenceStatus.OBSERVED,
                    confidence=0.7,
                    verification_method="static_analysis",
                    criterion="engineering",
                )
            )
        return out


ADAPTERS: dict[str, Adapter] = {a.name: a for a in (RagAdapter(), AgenticAdapter(), MlAdapter(), GenericAdapter())}


def inspect_repository(snap: Snapshot) -> list[Finding]:
    """Run the adapters the profiler selected, plus generic. Unknown/unbuilt adapters (vision, infra)
    fall back to generic with a recorded limitation (§7.3: no penalty for unfamiliar stacks)."""
    findings: list[Finding] = []
    for name in snap.profile.recommended_adapters:
        adapter = ADAPTERS.get(name)
        if adapter is None:
            findings.append(
                snap.missing(
                    f"generic.adapter_{name}_unavailable",
                    f"No specialised {name} inspector yet; generic inspection only (not a penalty)",
                ).model_copy(update={"status": INFERRED, "confidence": 1.0})
            )
        elif name != "generic":
            findings += adapter.inspect(snap)
    return findings + ADAPTERS["generic"].inspect(snap)
