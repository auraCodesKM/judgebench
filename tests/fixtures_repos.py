"""Small benign fixture repositories (demo cast, §20.1), as {path: content}."""

ALPHA_RAG = {  # honest RAG app with tests, eval and real CI
    "README.md": "# Alpha DocQA\nAnswers questions over PDFs with citations. Evaluated with recall@5.\n",
    "requirements.txt": "langchain\nfaiss-cpu\nfastapi\nsentence-transformers\npytest\n",
    "app/main.py": (
        "from fastapi import FastAPI\nfrom app.rag import answer\n\napp = FastAPI()\n\n"
        "@app.post('/ask')\ndef ask(q: str):\n    return answer(q)\n"
    ),
    "app/rag.py": (
        "from langchain.text_splitter import RecursiveCharacterTextSplitter\n"
        "from langchain.vectorstores import FAISS\nfrom langchain.embeddings import HuggingFaceEmbeddings\n\n"
        "def build(docs):\n    chunks = RecursiveCharacterTextSplitter(chunk_size=500).split_documents(docs)\n"
        "    return FAISS.from_documents(chunks, HuggingFaceEmbeddings())\n\n"
        "STORE = None\n\ndef answer(q):\n    hits = STORE.similarity_search(q, k=5)\n"
        "    return {'answer': hits[0].page_content, 'sources': [h.metadata['source'] for h in hits]}\n"
    ),
    "eval/eval_retrieval.py": "def recall_at_k(gold, hits, k=5):\n    return len(set(gold) & set(hits[:k])) / len(gold)\n",
    "tests/test_rag.py": "def test_recall():\n    assert True\n\ndef test_answer_has_sources():\n    assert True\n",
    ".github/workflows/test.yml": (
        "name: test\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - uses: actions/checkout@v4\n      - run: pip install -r requirements.txt\n      - run: pytest -q\n"
    ),
}

BETA_RAG_UNWIRED = {  # inflated claims; retriever defined but never called from the app
    "README.md": "# Beta\nOur RAG system achieves 98% accuracy and answers in real time. Fully tested.\n",
    "requirements.txt": "langchain\nchromadb\nstreamlit\n",
    "app.py": "import streamlit as st\nimport openai\n\nq = st.text_input('Ask')\nst.write(openai.chat(q))\n",
    "retriever.py": "from langchain.vectorstores import Chroma\n\ndef search(db, q):\n    return db.similarity_search(q)\n",
}

GAMMA_MULTI_AGENT = {
    "README.md": "# Gamma\nA multi-agent planner with a researcher and a writer.\n",
    "requirements.txt": "langgraph\nlangchain\n",
    "graph.py": (
        "from langgraph.graph import StateGraph\nfrom langgraph.prebuilt import create_react_agent\n"
        "from langgraph.checkpoint.memory import MemorySaver\nfrom langchain.tools import tool\n\n"
        "@tool\ndef web_search(q: str) -> str:\n    return q\n\n"
        "researcher = create_react_agent(model, tools=[web_search])\nwriter = create_react_agent(model, tools=[])\n"
        "g = StateGraph(dict)\ng.add_node('research', researcher)\ng.add_node('write', writer)\n"
        "g.add_edge('research', 'write')\napp = g.compile(checkpointer=MemorySaver())\n\n"
        "if __name__ == '__main__':\n    app.invoke({'q': 'x'}, config={'recursion_limit': 10})\n"
    ),
}

ML_CLASSIFIER = {
    "README.md": "# Churn\nXGBoost churn model, 91% F1 on held-out test.\n",
    "requirements.txt": "scikit-learn\nxgboost\npandas\n",
    "train.py": (
        "from sklearn.model_selection import train_test_split\nfrom sklearn.metrics import f1_score\n"
        "from sklearn.dummy import DummyClassifier\nfrom xgboost import XGBClassifier\n\n"
        "def main(X, y):\n    X_train, X_test, y_train, y_test = train_test_split(X, y)\n"
        "    base = DummyClassifier().fit(X_train, y_train)\n    m = XGBClassifier().fit(X_train, y_train)\n"
        "    print(f1_score(y_test, m.predict(X_test)))\n\nif __name__ == '__main__':\n    main(None, None)\n"
    ),
}

ZETA_EMPTY_CI = {  # green CI that tests nothing
    "README.md": "# Zeta\nFully tested agent.\n",
    "requirements.txt": "openai\n",
    "main.py": "import openai\n\nif __name__ == '__main__':\n    print(openai.chat('hi'))\n",
    ".github/workflows/ci.yml": (
        "name: ci\non: [push]\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n"
        '      - uses: actions/checkout@v4\n      - run: echo "tests passed"\n'
    ),
}
