"""Static wiring analysis (§7.5 source 3): is a claimed component actually on a code path that
runs? Built from `ast.parse` only. Answers questions like "is the retriever called from a request
handler?" - an import is not an implementation.

Resolution is by bare name (`retrieve()` matches every `def retrieve`). ponytail: name-based call
graph over-approximates reachability; upgrade to per-module resolution if false positives show up
in the benchmark.
"""

import ast
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from judgebench.adapters.profiler import ENTRY_NAMES, MAX_PARSED_FILES, SKIP_DIRS, _parse, _read

MODULE = "<module>"
_HANDLER_DECORATOR = re.compile(
    r"\.(get|post|put|patch|delete|route|websocket|api_route|command|message_handler)\b|^(tool|function_tool)\b"
)
_APP_MARKERS = ("streamlit", "gradio", "flask", "fastapi", "chainlit")


@dataclass
class Func:
    qualname: str  # "app/rag.py:answer"
    name: str
    path: str
    line: int
    end_line: int
    calls: set[str] = field(default_factory=set)
    decorators: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CallSite:
    path: str
    line: int
    name: str
    func: str  # qualname of the enclosing function, or "<path>:<module>"
    text: str  # unparsed call, truncated


def _call_name(node: ast.Call) -> str:
    fn = node.func
    if isinstance(fn, ast.Attribute):
        return fn.attr
    if isinstance(fn, ast.Name):
        return fn.id
    return ""


class CodeIndex:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.funcs: dict[str, Func] = {}
        self.by_name: dict[str, list[str]] = {}
        self.sites: list[CallSite] = []
        self.keywords: list[tuple[str, int, str, str]] = []  # (path, line, call name, keyword)
        self.decorated: list[tuple[str, int, str, str]] = []  # (path, line, func name, decorator)
        self.roots: set[str] = set()
        self.parsed = 0
        self.test_functions = 0
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(root)
            if SKIP_DIRS.intersection(rel.parts[:-1]) or not path.is_file() or path.is_symlink():
                continue
            if self.parsed >= MAX_PARSED_FILES:
                break
            text = _read(path)
            tree = _parse(text) if text is not None else None
            if tree is None:
                continue
            self.parsed += 1
            self._index(rel.as_posix(), tree, text or "")

    def _index(self, rel: str, tree: ast.Module, text: str) -> None:
        module_q = f"{rel}:{MODULE}"
        module = Func(module_q, MODULE, rel, 1, len(text.splitlines()))
        self.funcs[module_q] = module
        is_entry_file = Path(rel).name in ENTRY_NAMES or any(m in text for m in _APP_MARKERS)
        if is_entry_file:
            self.roots.add(module_q)

        def visit(node: ast.AST, owner: Func) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    q = f"{rel}:{child.name}"
                    decos = [ast.unparse(d)[:120] for d in child.decorator_list]
                    f = Func(q, child.name, rel, child.lineno, child.end_lineno or child.lineno, decorators=decos)
                    self.funcs[q] = f
                    self.by_name.setdefault(child.name, []).append(q)
                    if child.name.startswith("test_"):
                        self.test_functions += 1
                    for d in decos:
                        self.decorated.append((rel, child.lineno, child.name, d))
                        if _HANDLER_DECORATOR.search(d):
                            self.roots.add(q)
                    visit(child, f)
                    continue
                if isinstance(child, ast.If) and owner is module and _is_main_guard(child):
                    self.roots.add(module_q)
                if isinstance(child, ast.Call):
                    name = _call_name(child)
                    if name:
                        owner.calls.add(name)
                        self.sites.append(CallSite(rel, child.lineno, name, owner.qualname, ast.unparse(child)[:160]))
                    for kw in child.keywords:
                        if kw.arg:
                            self.keywords.append((rel, child.lineno, name, kw.arg))
                    # callbacks passed as values (fn=answer, Tool(func=search)) count as calls
                    for arg in [*child.args, *(k.value for k in child.keywords)]:
                        if isinstance(arg, ast.Name):
                            owner.calls.add(arg.id)
                        elif isinstance(arg, ast.List | ast.Tuple):
                            owner.calls.update(e.id for e in arg.elts if isinstance(e, ast.Name))
                if isinstance(child, ast.Dict):  # config dicts: {"recursion_limit": 10}
                    for k in child.keys:
                        if isinstance(k, ast.Constant) and isinstance(k.value, str):
                            self.keywords.append((rel, child.lineno, "<dict>", k.value))
                visit(child, owner)

        visit(tree, module)

    def reachable(self) -> set[str]:
        """Qualnames reachable from entry points (handlers, __main__, app files) by name."""
        seen, queue = set(self.roots), deque(self.roots)
        while queue:
            for name in self.funcs[queue.popleft()].calls:
                for q in self.by_name.get(name, []):
                    if q not in seen:
                        seen.add(q)
                        queue.append(q)
        return seen

    def find_defs(self, names: set[str]) -> list[CallSite]:
        return [
            CallSite(f.path, f.line, f.name, f.qualname, f"def {f.name}")
            for f in self.funcs.values()
            if f.name in names
        ]

    def find(self, names: set[str]) -> list[CallSite]:
        return [s for s in self.sites if s.name in names]

    def find_keywords(self, keywords: set[str]) -> list[tuple[str, int, str, str]]:
        return [k for k in self.keywords if k[3] in keywords]


def _is_main_guard(node: ast.If) -> bool:
    t = node.test
    return isinstance(t, ast.Compare) and isinstance(t.left, ast.Name) and t.left.id == "__name__"
