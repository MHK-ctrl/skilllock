"""Shared local Git fixture used by the snapshot equivalence test.

The bytes here are the contract: the golden fixture
(``tests/fixtures/golden-snapshot.json``) was captured from skilllock v0.1.0
using exactly this repository content, so any reader change must reproduce it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SUBDIR = "skills/example-skill"
REF = "refs/tags/v1.0.0"

NON_ASCII_TEXT = "héllo wörld — 日本語\n"
LARGE_PAYLOAD = bytes(range(256)) * 1024  # 256 KiB

# Repository-relative (inside SUBDIR) path -> exact file bytes.
FIXTURE_FILES: dict[str, bytes] = {
    "SKILL.md": b"---\nname: example-skill\n---\n# Example skill\n",
    "nested/deep/file.txt": b"nested content\n",
    "name with spaces.txt": b"spaces are allowed in the middle\n",
    "unicode.txt": NON_ASCII_TEXT.encode("utf-8"),
    "empty.txt": b"",
    "large.bin": LARGE_PAYLOAD,
}

# A file outside SUBDIR that must never appear in the snapshot.
OUTSIDE_FILE = ("README.md", b"this file is outside the declared subdir\n")


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True
    )
    return result.stdout.decode("utf-8").strip()


def build_repo(tmp_path: Path) -> str:
    """Create a local repository and return a ``file://`` source URL."""
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    _git("config", "user.email", "test@example.com", cwd=work)
    _git("config", "user.name", "Test", cwd=work)

    root = work / SUBDIR
    for relative, data in FIXTURE_FILES.items():
        target = root.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    outside_name, outside_data = OUTSIDE_FILE
    (work / outside_name).write_bytes(outside_data)

    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "fixture", cwd=work)
    _git("tag", REF.removeprefix("refs/tags/"), cwd=work)

    bare = tmp_path / "remote.git"
    _git("init", "-q", "--bare", str(bare), cwd=tmp_path)
    _git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=bare)
    _git(
        "-c",
        "protocol.file.allow=always",
        "push",
        "-q",
        str(bare),
        "refs/heads/main:refs/heads/main",
        f"{REF}:{REF}",
        cwd=work,
    )
    return f"file://{bare}"
