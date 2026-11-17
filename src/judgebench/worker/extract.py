"""Hardened tarball extraction (§14.1 rule 1). Reads participant files; never runs them.

Policy:
- Only regular files and directories. Every symlink, hard link, device and FIFO is skipped.
- Reject absolute paths, `..` components and paths deeper than max_depth.
- Caps: decompressed stream bytes, total bytes written, single file size, file count, wall-clock.
  Oversize files are skipped; hitting a global cap stops extraction. Both are recorded as
  limitations so the submission is marked, not silently truncated.
- Files are written 0o644 with O_EXCL|O_NOFOLLOW: never executable, never overwritten, never
  written through a link.
"""

import gzip
import os
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import BinaryIO

MIB = 1024 * 1024


@dataclass(frozen=True)
class Limits:
    max_archive_bytes: int = 1024 * MIB  # decompressed stream, incl. skipped members
    max_total_bytes: int = 300 * MIB  # bytes actually written
    max_file_bytes: int = 5 * MIB  # larger files (weights, datasets) are skipped
    max_files: int = 20_000
    max_depth: int = 25
    max_seconds: float = 120.0


@dataclass(frozen=True)
class Rejected:
    path: str
    reason: str


@dataclass
class ExtractResult:
    files_written: int = 0
    bytes_written: int = 0
    bytes_read: int = 0
    truncated: bool = False
    rejected: list[Rejected] = field(default_factory=list)
    skipped: list[Rejected] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


class _CapExceeded(Exception):
    pass


class _CappedReader:
    """Counts decompressed bytes and aborts past the cap: archive-bomb guard."""

    def __init__(self, raw: BinaryIO, cap: int) -> None:
        self._raw, self._cap, self.count = raw, cap, 0

    def read(self, size: int = -1) -> bytes:
        data = self._raw.read(size)
        self.count += len(data)
        if self.count > self._cap:
            raise _CapExceeded
        return data


def _safe_relpath(name: str, max_depth: int) -> tuple[PurePosixPath | None, str]:
    """Strip GitHub's top-level dir. Returns (path, "") or (None, reason)."""
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        return None, "absolute path"
    parts = PurePosixPath(name).parts
    if ".." in parts:
        return None, "parent traversal"
    rel = parts[1:]  # drop "<owner>-<repo>-<sha>/"
    if not rel:
        return None, "top-level entry"
    if len(rel) > max_depth:
        return None, "path too deep"
    return PurePosixPath(*rel), ""


def extract_tarball(stream: BinaryIO, dest: Path, limits: Limits = Limits()) -> ExtractResult:  # noqa: B008
    dest.mkdir(parents=True, exist_ok=False)
    root = dest.resolve()
    result = ExtractResult()
    reader = _CappedReader(gzip.GzipFile(fileobj=stream), limits.max_archive_bytes)
    deadline = time.monotonic() + limits.max_seconds

    def stop(reason: str) -> None:
        result.truncated = True
        result.limitations.append(reason)

    try:
        # "r|" = sequential stream: no seeking, so a lying index can't make us jump around.
        with tarfile.open(fileobj=reader, mode="r|") as tar:  # type: ignore[call-overload]
            for member in tar:
                if time.monotonic() > deadline:
                    stop(f"extraction time limit ({limits.max_seconds:.0f}s) reached")
                    break
                rel, reason = _safe_relpath(member.name, limits.max_depth)
                if rel is None:
                    if reason != "top-level entry":
                        result.rejected.append(Rejected(member.name, reason))
                    continue
                if not (member.isfile() or member.isdir()):
                    result.rejected.append(Rejected(str(rel), f"not a regular file ({member.type!r})"))
                    continue
                target = root / rel
                if not target.resolve().is_relative_to(root):  # defence in depth
                    result.rejected.append(Rejected(str(rel), "escapes workspace"))
                    continue
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if member.size > limits.max_file_bytes:
                    result.skipped.append(Rejected(str(rel), f"file larger than {limits.max_file_bytes} bytes"))
                    result.limitations.append(f"skipped oversize file {rel} ({member.size} bytes)")
                    continue
                if result.files_written >= limits.max_files:
                    stop(f"file count cap ({limits.max_files}) reached")
                    break
                if result.bytes_written + member.size > limits.max_total_bytes:
                    stop(f"total size cap ({limits.max_total_bytes} bytes) reached")
                    break
                src = tar.extractfile(member)
                if src is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
                except FileExistsError:
                    result.rejected.append(Rejected(str(rel), "duplicate entry"))
                    continue
                with os.fdopen(fd, "wb") as out:
                    while chunk := src.read(MIB):
                        out.write(chunk)
                result.files_written += 1
                result.bytes_written += member.size
    except _CapExceeded:
        stop(f"archive cap ({limits.max_archive_bytes} decompressed bytes) reached: possible archive bomb")
    except (tarfile.TarError, OSError, EOFError) as exc:
        stop(f"archive unreadable after {result.files_written} files: {type(exc).__name__}")
    result.bytes_read = reader.count
    return result
