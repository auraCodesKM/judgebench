"""Builders for hostile repository tarballs (§14.2).

Hostile file contents live only in these strings and are packed into in-memory
tarballs at test time. They never exist as files in the test tree, so pytest can
never collect or import them.
"""

import gzip
import io
import tarfile
from collections.abc import Mapping

PREFIX = "team-repo-3f2c1ab"  # GitHub tarballs wrap everything in one top-level dir
MIB = 1024 * 1024


def _add_file(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o755  # hostile: tries to make files executable
    tar.addfile(info, io.BytesIO(data))


def tar_gz(files: Mapping[str, bytes | str], *, prefix: str = PREFIX) -> bytes:
    """Regular repository tarball: {relative_path: content}."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            raw = data.encode() if isinstance(data, str) else data
            _add_file(tar, f"{prefix}/{name}" if prefix else name, raw)
    return buf.getvalue()


def code_execution_repo(canary: str, port: int) -> bytes:
    """Repo whose setup.py, conftest.py and __init__.py write a canary and call the network."""
    payload = f"""
import socket, pathlib
pathlib.Path({canary!r}).write_text("executed")
socket.create_connection(("127.0.0.1", {port}), timeout=1)
"""
    return tar_gz(
        {
            "setup.py": payload + "\nfrom setuptools import setup\nsetup(name='x')\n",
            "conftest.py": payload,
            "pkg/__init__.py": payload,
            "pkg/model.py": "import torch\nfrom langchain import hub\n",
            "requirements.txt": "torch\nlangchain\nfaiss-cpu\n",
            "README.md": "# Team project\nRun `pip install -e .`\n",
            "notebook.ipynb": '{"cells": [{"cell_type": "code", "source": ["' + "import os; os.system('touch x')" + '"]}]}',
        }
    )


def path_traversal_repo(absolute_target: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _add_file(tar, f"{PREFIX}/ok.txt", b"fine")
        _add_file(tar, "../../etc/x", b"escaped")
        _add_file(tar, f"{PREFIX}/../../escaped.txt", b"escaped")
        _add_file(tar, f"{PREFIX}/a/../../../escaped2.txt", b"escaped")
        _add_file(tar, absolute_target, b"escaped")
    return buf.getvalue()


def symlink_repo() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _add_file(tar, f"{PREFIX}/README.md", b"# hi")
        for name, target in [
            ("passwd", "/etc/passwd"),
            ("up", ".."),
            ("deep_up", "../../../../../../"),
        ]:
            link = tarfile.TarInfo(f"{PREFIX}/{name}")
            link.type = tarfile.SYMTYPE
            link.linkname = target
            tar.addfile(link)
        hard = tarfile.TarInfo(f"{PREFIX}/hardlink")
        hard.type = tarfile.LNKTYPE
        hard.linkname = "/etc/passwd"
        tar.addfile(hard)
        # Classic two-step escape: write *through* the symlink created above.
        _add_file(tar, f"{PREFIX}/up/escaped_via_symlink.txt", b"escaped")
    return buf.getvalue()


def _gzip_of_zeros(total: int) -> bytes:
    """Gzip stream that inflates to `total` zero bytes, built cheaply.

    Concatenated gzip members are one valid gzip stream, so N copies of a
    compressed 1 MiB block of zeros inflate to N MiB while costing ~1 KiB each.
    """
    assert total % MIB == 0
    return gzip.compress(b"\0" * MIB, 9) * (total // MIB)


def huge_file_repo(size: int, *, small_files_first: int = 3) -> bytes:
    """Repo with a few normal files then one member whose real data is `size` bytes."""
    head = io.BytesIO()
    with tarfile.open(fileobj=head, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for i in range(small_files_first):
            _add_file(tar, f"{PREFIX}/src/file{i}.py", b"x = 1\n")
        info = tarfile.TarInfo(f"{PREFIX}/weights.bin")
        info.size = size
        tar.addfile(info)  # header only; data follows as gzip-of-zeros
    header_bytes = head.getvalue()
    # tarfile wrote header + end-of-archive blocks; keep only up to the big header.
    cut = header_bytes.rfind(b"weights.bin")
    header_end = (cut // 512 + 1) * 512
    trailer = b"\0" * 1024  # end-of-archive
    return gzip.compress(header_bytes[:header_end]) + _gzip_of_zeros(size) + gzip.compress(trailer)


def archive_bomb(inflated: int) -> bytes:
    """~1 KiB per MiB inflated: an 8 GiB bomb is ~8 MiB on disk."""
    return huge_file_repo(inflated, small_files_first=1)


def many_files_repo(count: int) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=1) as tar:
        for i in range(count):
            info = tarfile.TarInfo(f"{PREFIX}/d{i % 100}/f{i}.txt")
            info.size = 0
            tar.addfile(info, io.BytesIO(b""))
    return buf.getvalue()
