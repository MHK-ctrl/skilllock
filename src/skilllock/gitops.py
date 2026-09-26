"""Git retrieval of immutable snapshots without checking out repository code.

We fetch into a throwaway bare repository, read objects with ``git archive``,
and parse the resulting tar stream ourselves. No working tree is ever created,
so hooks, smudge filters, and repository scripts never run. Symlinks,
hardlinks, device nodes, FIFOs, and gitlinks are rejected before any bytes are
trusted.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .checks import SL003, SL005, OperationalError, ValidationProblem
from .hashing import sha256_hex, sort_by_utf8_path
from .paths import check_case_collisions, validate_relative_path

_BLOCK = 512
MAX_FILES = 2048
MAX_TOTAL_BYTES = 64 * 1024 * 1024

_FETCH_VALIDATION_MARKERS = (
    "couldn't find remote ref",
    "not our ref",
    "bad object",
    "unknown revision",
    "did not match any",
    "no such ref",
)


@dataclass(frozen=True)
class SnapshotFile:
    """One regular file from a skill snapshot."""

    path: str
    sha256: str
    mode: str
    data: bytes


@dataclass(frozen=True)
class Snapshot:
    """The immutable content of one skill at a pinned commit."""

    commit: str
    files: tuple[SnapshotFile, ...]
    skill_md: bytes | None


def _git_env(gitconfig: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = str(gitconfig)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    return env


def _run_git(
    args: list[str], *, env: dict[str, str], cwd: Path | None = None
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise OperationalError("git executable not found on PATH") from exc
    except OSError as exc:
        raise OperationalError(f"cannot run git: {exc}") from exc


def _first_line(data: bytes) -> str:
    for line in data.decode("utf-8", "replace").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return "unknown git error"


def _fetch_error(stderr: bytes, check_id: str) -> Exception:
    lowered = stderr.decode("utf-8", "replace").lower()
    if any(marker in lowered for marker in _FETCH_VALIDATION_MARKERS):
        return ValidationProblem(
            check_id, f"ref or commit did not resolve: {_first_line(stderr)}"
        )
    return OperationalError(f"git fetch failed: {_first_line(stderr)}")


def _is_full_sha(text: str) -> bool:
    return len(text) == 40 and all(char in "0123456789abcdef" for char in text)


def fetch_snapshot(
    source: str, rev: str, subdir: str, *, allow_file: bool = False
) -> Snapshot:
    """Fetch ``rev`` from ``source`` and return a validated snapshot.

    ``allow_file`` exists only as a test seam: production callers leave it
    ``False`` so the ``ext`` and ``file`` Git transports stay disabled.
    """
    with tempfile.TemporaryDirectory(prefix="skilllock-") as work:
        workdir = Path(work)
        bare = workdir / "repo.git"
        template = workdir / "template"
        template.mkdir()
        hooks = workdir / "hooks"
        hooks.mkdir()
        gitconfig = workdir / "gitconfig"
        gitconfig.write_text("", encoding="utf-8")
        env = _git_env(gitconfig)

        init = _run_git(
            ["init", "--bare", f"--template={template}", str(bare)], env=env
        )
        if init.returncode != 0:
            raise OperationalError(f"git init failed: {_first_line(init.stderr)}")

        config = ["-c", f"core.hooksPath={hooks}", "-c", "protocol.ext.allow=never"]
        if not allow_file:
            config += ["-c", "protocol.file.allow=never"]
        fetched = _run_git(
            [
                *config,
                "fetch",
                "--no-tags",
                "--no-recurse-submodules",
                "--depth=1",
                source,
                rev,
            ],
            env=env,
            cwd=bare,
        )
        if fetched.returncode != 0:
            raise _fetch_error(fetched.stderr, SL003)

        commit = _rev_parse_commit(bare, env)
        _assert_tree_safe(bare, commit, subdir, env)
        archive = _archive(bare, commit, subdir, env)
        files = _snapshot_files(archive, subdir)
        skill_md = next((item.data for item in files if item.path == "SKILL.md"), None)
        return Snapshot(commit=commit, files=files, skill_md=skill_md)


def _rev_parse_commit(bare: Path, env: dict[str, str]) -> str:
    proc = _run_git(["rev-parse", "--verify", "FETCH_HEAD^{commit}"], env=env, cwd=bare)
    if proc.returncode != 0:
        raise ValidationProblem(SL003, "fetched ref did not resolve to a commit")
    text = proc.stdout.decode("ascii", "replace").strip()
    if not _is_full_sha(text):
        raise ValidationProblem(SL003, f"fetched object is not a commit: {text!r}")
    return text


def _assert_tree_safe(
    bare: Path, commit: str, subdir: str, env: dict[str, str]
) -> None:
    args = ["ls-tree", "-r", "-z", commit]
    if subdir != ".":
        args += ["--", subdir]
    proc = _run_git(args, env=env, cwd=bare)
    if proc.returncode != 0:
        raise ValidationProblem(
            SL003, f"cannot read tree for {commit}: {_first_line(proc.stderr)}"
        )
    for record in proc.stdout.split(b"\0"):
        if not record:
            continue
        meta, _, raw_path = record.partition(b"\t")
        mode = meta.split(b" ", 1)[0].decode("ascii", "replace")
        path = raw_path.decode("utf-8", "replace")
        if mode == "120000":
            raise ValidationProblem(SL005, f"repository contains a symlink: {path!r}")
        if mode == "160000":
            raise ValidationProblem(SL005, f"repository contains a submodule: {path!r}")
        if mode not in {"100644", "100755"}:
            raise ValidationProblem(
                SL005, f"repository contains an unsupported mode {mode}: {path!r}"
            )


def _archive(bare: Path, commit: str, subdir: str, env: dict[str, str]) -> bytes:
    args = ["archive", "--format=tar", commit]
    if subdir != ".":
        args += ["--", subdir]
    proc = _run_git(args, env=env, cwd=bare)
    if proc.returncode != 0:
        raise ValidationProblem(
            SL003, f"cannot archive {commit}: {_first_line(proc.stderr)}"
        )
    return proc.stdout


def _strip_subdir(name: str, subdir: str) -> str:
    if subdir == ".":
        return name
    prefix = f"{subdir}/"
    if not name.startswith(prefix):
        raise ValidationProblem(
            SL005, f"archive entry is outside the skill root: {name!r}"
        )
    relative = name[len(prefix) :]
    if not relative:
        raise ValidationProblem(
            SL005, f"archive entry is the skill root itself: {name!r}"
        )
    return relative


def _mode_from_perm(perm: int) -> str:
    return "100755" if perm & 0o111 else "100644"


def _snapshot_files(archive: bytes, subdir: str) -> tuple[SnapshotFile, ...]:
    records: list[SnapshotFile] = []
    total = 0
    for name, perm, payload in _iter_tar(archive):
        relative = _strip_subdir(name, subdir)
        validate_relative_path(relative)
        if len(records) >= MAX_FILES:
            raise ValidationProblem(
                SL005, f"snapshot exceeds the {MAX_FILES}-file limit"
            )
        total += len(payload)
        if total > MAX_TOTAL_BYTES:
            raise ValidationProblem(
                SL005, f"snapshot exceeds the {MAX_TOTAL_BYTES}-byte limit"
            )
        records.append(
            SnapshotFile(
                path=relative,
                sha256=sha256_hex(payload),
                mode=_mode_from_perm(perm),
                data=payload,
            )
        )
    check_case_collisions(item.path for item in records)
    return tuple(sort_by_utf8_path(records))


def _parse_octal(field: bytes) -> int:
    if not field:
        return 0
    if field[0] & 0x80:
        return int.from_bytes(bytes([field[0] & 0x7F]) + field[1:], "big")
    text = field.split(b"\0", 1)[0].strip()
    if not text:
        return 0
    try:
        return int(text, 8)
    except ValueError:
        return -1


def _checksum_ok(header: bytes, stored: int) -> bool:
    unsigned = sum(header[:148]) + (0x20 * 8) + sum(header[156:])
    if stored == unsigned:
        return True
    signed = (
        sum(byte - 256 if byte > 127 else byte for byte in header[:148])
        + (0x20 * 8)
        + sum(byte - 256 if byte > 127 else byte for byte in header[156:])
    )
    return stored == signed


def _parse_pax(payload: bytes) -> dict[str, str]:
    records: dict[str, str] = {}
    index = 0
    while index < len(payload):
        space = payload.find(b" ", index)
        if space == -1:
            break
        try:
            length = int(payload[index:space])
        except ValueError:
            break
        if length <= 0 or index + length > len(payload):
            break
        record = payload[space + 1 : index + length].rstrip(b"\n")
        key, _, value = record.partition(b"=")
        try:
            records[key.decode("utf-8")] = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationProblem(
                SL005, "archive metadata is not valid UTF-8"
            ) from exc
        index += length
    return records


def _iter_tar(data: bytes) -> Iterator[tuple[str, int, bytes]]:
    """Yield ``(path, permission_bits, payload)`` for regular files only."""
    offset = 0
    total = len(data)
    pending_path: str | None = None
    pending_size: int | None = None
    gnu_name: str | None = None
    while offset + _BLOCK <= total:
        header = data[offset : offset + _BLOCK]
        offset += _BLOCK
        if header == b"\0" * _BLOCK:
            return
        stored_checksum = _parse_octal(header[148:156])
        if stored_checksum < 0 or not _checksum_ok(header, stored_checksum):
            raise OperationalError("corrupt tar archive header")
        raw_size = _parse_octal(header[124:136])
        if raw_size < 0:
            raise OperationalError("corrupt tar archive size field")
        typeflag = header[156:157]

        if typeflag in {b"x", b"g"}:
            payload = _read_payload(data, offset, raw_size)
            offset += _padded(raw_size)
            if typeflag == b"x":
                pax = _parse_pax(payload)
                pending_path = pax.get("path", pending_path)
                if "size" in pax:
                    try:
                        pending_size = int(pax["size"])
                    except ValueError:
                        pending_size = None
            continue
        if typeflag in {b"L", b"K"}:
            payload = _read_payload(data, offset, raw_size)
            offset += _padded(raw_size)
            if typeflag == b"L":
                gnu_name = payload.split(b"\0", 1)[0].decode("utf-8", "surrogateescape")
            continue

        size = pending_size if pending_size is not None else raw_size
        payload = _read_payload(data, offset, size)
        offset += _padded(size)
        pending_size = None

        if gnu_name is not None:
            name_bytes = gnu_name.encode("utf-8", "surrogateescape")
            gnu_name = None
        else:
            name_field = header[0:100].split(b"\0", 1)[0]
            prefix_field = header[345:500].split(b"\0", 1)[0]
            name_bytes = (
                f"{prefix_field}/{name_field}".encode() if prefix_field else name_field
            )
        if pending_path is not None:
            name_bytes = pending_path.encode("utf-8")
            pending_path = None
        try:
            name = name_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationProblem(
                SL005, "archive entry path is not valid UTF-8"
            ) from exc

        if typeflag in {b"0", b"\0", b""}:
            yield name, _parse_octal(header[100:108]), payload
        elif typeflag == b"5":
            continue
        elif typeflag == b"2":
            raise ValidationProblem(SL005, f"archive contains a symlink: {name!r}")
        elif typeflag == b"1":
            raise ValidationProblem(SL005, f"archive contains a hardlink: {name!r}")
        elif typeflag in {b"3", b"4"}:
            raise ValidationProblem(SL005, f"archive contains a device node: {name!r}")
        elif typeflag == b"6":
            raise ValidationProblem(SL005, f"archive contains a FIFO: {name!r}")
        else:
            raise ValidationProblem(
                SL005, f"archive contains an unsupported object: {name!r}"
            )


def _padded(size: int) -> int:
    return size + ((_BLOCK - (size % _BLOCK)) % _BLOCK)


def _read_payload(data: bytes, offset: int, size: int) -> bytes:
    if size < 0 or offset + size > len(data):
        raise OperationalError("truncated tar archive")
    return data[offset : offset + size]
