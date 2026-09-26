"""Streamed ``tarfile`` reader tests and the golden snapshot equivalence test.

The crafted archives here exercise the reader directly (no network, no Git):
traversal, absolute, and Windows-style names; symlinks and hardlinks; pax and
GNU long names; truncation; and the file-count and total-size limits. The
golden test proves byte-compatibility with skilllock v0.1.0 for well-formed
archives produced by real ``git archive``.
"""

from __future__ import annotations

import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest
import snapshot_fixture as fx

from skilllock.checks import OperationalError, ValidationProblem
from skilllock.gitops import (
    MAX_FILES,
    MAX_TOTAL_BYTES,
    fetch_snapshot,
    parse_archive_stream,
)

GOLDEN_PATH = Path(__file__).parent / "fixtures" / "golden-snapshot.json"


class _CountingStream(io.BytesIO):
    """A byte stream that records how many bytes the reader consumed."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.total_read = 0

    def read(self, size: int = -1) -> bytes:
        chunk = super().read(size)
        self.total_read += len(chunk)
        return chunk


def _tar(entries, *, fmt: int = tarfile.PAX_FORMAT) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=fmt) as archive:
        for info, data in entries:
            archive.addfile(info, io.BytesIO(data) if data is not None else None)
    return buffer.getvalue()


def _file(name: str, data: bytes = b"", *, mode: int = 0o644) -> tuple:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    return info, data


def _directory(name: str) -> tuple:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.size = 0
    return info, None


def _special(name: str, kind: bytes, *, linkname: str = "target") -> tuple:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = linkname
    info.size = 0
    return info, None


def _read(archive: bytes, subdir: str = ".", source: str = "source", commit: str = "c"):
    return parse_archive_stream(
        io.BytesIO(archive), subdir, source=source, commit=commit
    )


def test_reader_reads_regular_files_and_skips_directories() -> None:
    archive = _tar(
        [
            _directory("d"),
            _directory("d/nested"),
            _file("d/nested/a.txt", b"alpha\n"),
            _file("b.bin", b"\x00\x01\x02"),
        ]
    )
    records = _read(archive)
    assert [item.path for item in records] == ["b.bin", "d/nested/a.txt"]
    assert records[1].data == b"alpha\n"


def test_reader_strips_leading_dot_slash() -> None:
    archive = _tar([_file("./a.txt", b"dotted\n")])
    records = _read(archive)
    assert [item.path for item in records] == ["a.txt"]


def test_reader_strips_declared_subdir_prefix() -> None:
    archive = _tar([_file("skills/example/SKILL.md", b"body\n")])
    records = _read(archive, "skills/example")
    assert [item.path for item in records] == ["SKILL.md"]


def test_reader_rejects_entry_outside_subdir() -> None:
    archive = _tar([_file("other/x.txt", b"nope\n")])
    with pytest.raises(ValidationProblem) as info:
        _read(archive, "sub")
    assert info.value.id == "SL005"


@pytest.mark.parametrize(
    "name",
    ["../evil", "a/../../evil", "a/b/../../../evil", ".."],
)
def test_reader_rejects_traversal(name: str) -> None:
    archive = _tar([_file(name, b"nope\n")])
    with pytest.raises(ValidationProblem) as info:
        _read(archive)
    assert info.value.id == "SL005"


@pytest.mark.parametrize("name", ["/etc/passwd", "/", "//evil"])
def test_reader_rejects_absolute_names(name: str) -> None:
    archive = _tar([_file(name, b"nope\n")])
    with pytest.raises(ValidationProblem) as info:
        _read(archive)
    assert info.value.id == "SL005"


@pytest.mark.parametrize("name", ["..\\evil", "C:\\evil", "a\\b.txt"])
def test_reader_rejects_windows_style_names(name: str) -> None:
    archive = _tar([_file(name, b"nope\n")])
    with pytest.raises(ValidationProblem) as info:
        _read(archive)
    assert info.value.id == "SL005"


def test_reader_rejects_symlink_member() -> None:
    archive = _tar([_special("link", tarfile.SYMTYPE, linkname="SKILL.md")])
    with pytest.raises(ValidationProblem) as info:
        _read(archive)
    assert info.value.id == "SL005"
    assert "symlink" in info.value.message


def test_reader_rejects_hardlink_member() -> None:
    archive = _tar([_special("hard", tarfile.LNKTYPE, linkname="SKILL.md")])
    with pytest.raises(ValidationProblem) as info:
        _read(archive)
    assert info.value.id == "SL005"
    assert "hardlink" in info.value.message


def test_reader_rejects_fifo_member() -> None:
    archive = _tar([_special("pipe", tarfile.FIFOTYPE)])
    with pytest.raises(ValidationProblem) as info:
        _read(archive)
    assert info.value.id == "SL005"


def test_reader_handles_pax_long_path() -> None:
    name = "nested/" + "a" * 140 + ".txt"
    archive = _tar([_file(name, b"pax payload\n")], fmt=tarfile.PAX_FORMAT)
    records = _read(archive)
    assert [item.path for item in records] == [name]
    assert records[0].data == b"pax payload\n"


def test_reader_handles_gnu_long_name() -> None:
    name = "nested/" + "b" * 140 + ".txt"
    archive = _tar([_file(name, b"gnu payload\n")], fmt=tarfile.GNU_FORMAT)
    records = _read(archive)
    assert [item.path for item in records] == [name]
    assert records[0].data == b"gnu payload\n"


def test_reader_rejects_truncated_archive() -> None:
    payload = bytes(range(256)) * 16
    archive = _tar([_file("big.bin", payload)])
    # Cut inside the 4096-byte payload (the header is the first 512 bytes).
    truncated = archive[:600]
    with pytest.raises(OperationalError) as info:
        _read(truncated)
    assert "cannot read archive" in str(info.value)


def test_reader_rejects_garbage_stream() -> None:
    with pytest.raises(OperationalError):
        _read(b"this is not a tar archive at all" * 40)


def test_reader_enforces_file_count_limit() -> None:
    entries = [_file(f"f{index:05d}.txt", b"x") for index in range(MAX_FILES + 1)]
    archive = _tar(entries)
    with pytest.raises(ValidationProblem) as info:
        _read(archive)
    assert info.value.id == "SL005"
    assert "file limit" in info.value.message


def test_reader_enforces_total_size_limit_without_reading_payload() -> None:
    info = tarfile.TarInfo("big.bin")
    info.size = MAX_TOTAL_BYTES + 1
    info.mode = 0o644
    # The declared payload is absent and followed by garbage: if the reader tried
    # to consume the payload it would fail operationally instead of reporting the
    # limit finding. Getting SL005 proves the limit is enforced from the header
    # before any bytes are read or written.
    archive = _tar([(info, None)]) + b"GARBAGE-NOT-A-TAR-BLOCK" * 64
    stream = _CountingStream(archive)
    with pytest.raises(ValidationProblem) as excinfo:
        parse_archive_stream(stream, ".", source="source", commit="c")
    assert excinfo.value.id == "SL005"
    assert "byte limit" in excinfo.value.message
    assert stream.total_read <= len(archive)


def test_golden_snapshot_matches_v0_1_0(tmp_path: Path) -> None:
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert golden["subdir"] == fx.SUBDIR
    assert golden["ref"] == fx.REF

    source = fx.build_repo(tmp_path)
    snapshot = fetch_snapshot(source, fx.REF, fx.SUBDIR, allow_file=True)

    actual = [(item.path, item.sha256, len(item.data)) for item in snapshot.files]
    expected = [
        (item["path"], item["sha256"], item["size"]) for item in golden["files"]
    ]
    assert actual == expected

    # The exact bytes and the root SKILL.md must also match the fixture.
    for item in snapshot.files:
        assert item.data == fx.FIXTURE_FILES[item.path]
    assert snapshot.skill_md == fx.FIXTURE_FILES["SKILL.md"]

    # The file outside the declared subdir must never appear.
    assert fx.OUTSIDE_FILE[0] not in {item.path for item in snapshot.files}


def test_gitlink_is_rejected_by_ls_tree(tmp_path: Path) -> None:
    work = tmp_path / "work"
    (work / "pkg").mkdir(parents=True)
    (work / "pkg" / "SKILL.md").write_bytes(b"---\nname: pkg\n---\n")

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=str(work), check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("add", "-A")
    # Add a gitlink (submodule) entry without needing a real submodule.
    git("update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},vendor/sub")
    git("commit", "-q", "-m", "gitlink")
    git("tag", "v1.0.0")

    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(bare)], cwd=str(tmp_path), check=True
    )
    subprocess.run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "push",
            "-q",
            str(bare),
            "refs/heads/main:refs/heads/main",
            "refs/tags/v1.0.0:refs/tags/v1.0.0",
        ],
        cwd=str(work),
        check=True,
        capture_output=True,
    )
    with pytest.raises(ValidationProblem) as info:
        fetch_snapshot(f"file://{bare}", "refs/tags/v1.0.0", ".", allow_file=True)
    assert info.value.id == "SL005"
    assert "submodule" in info.value.message
