"""Adapter protocol (§7.3). Adapters are deterministic inspectors, not agents. They read the
extracted snapshot (text + ast) and return normalised Findings. Behaviour checks (CI, live app,
demo) are shared across adapters rather than duplicated in each (see DECISIONS.md)."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from judgebench.adapters.profiler import RepositoryProfile
from judgebench.adapters.wiring import CallSite, CodeIndex
from judgebench.evidence import EvidenceStatus, Finding, SourceType, sha256_file

ADAPTER_VERSION = "0.1.0"


@dataclass
class Snapshot:
    root: Path
    profile: RepositoryProfile
    index: CodeIndex

    def code_finding(
        self,
        key: str,
        observation: str,
        site: CallSite | tuple[str, int, int],
        *,
        status: EvidenceStatus = EvidenceStatus.OBSERVED,
        criterion: str | None = None,
        confidence: float = 0.8,
    ) -> Finding:
        path, start, end = (site.path, site.line, site.line) if isinstance(site, CallSite) else site
        return Finding(
            key=key,
            observation=observation,
            source_type=SourceType.CODE,
            source_locator=f"{path}#L{start}" + (f"-L{end}" if end != start else ""),
            status=status,
            confidence=confidence,
            verification_method="static_analysis",
            criterion=criterion,
            path=path,
            line_start=start,
            line_end=end,
            file_sha256=sha256_file(self.root / path),
        )

    def missing(self, key: str, observation: str, *, criterion: str | None = None) -> Finding:
        return Finding(
            key=key,
            observation=observation,
            source_type=SourceType.CODE,
            source_locator="repository",
            status=EvidenceStatus.MISSING,
            confidence=0.7,
            verification_method="static_analysis",
            criterion=criterion,
        )


class Adapter(Protocol):
    name: str

    def inspect(self, snap: Snapshot) -> list[Finding]: ...


def on_path(snap: Snapshot, sites: list[CallSite]) -> tuple[list[CallSite], list[CallSite]]:
    """Split call sites into (reachable from an entry point, not reachable)."""
    live = snap.index.reachable()
    return [s for s in sites if s.func in live], [s for s in sites if s.func not in live]


def locs(sites: list[CallSite], n: int = 3) -> str:
    return ", ".join(f"{s.path}:{s.line}" for s in sites[:n]) + (f" (+{len(sites) - n} more)" if len(sites) > n else "")
