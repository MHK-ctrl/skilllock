"""Git retrieval of immutable snapshots without checking out repository code.

We fetch into a throwaway bare repository, read objects with ``git archive``,
and stream the resulting tar through Python's standard-library ``tarfile``
reader (``mode="r|"``), consuming one member at a time. No working tree is ever
created, so hooks, smudge filters, and repository scripts never run. Symlinks,
hardlinks, device nodes, and FIFOs are rejected by the reader; gitlinks are
rejected by the separate ``git ls-tree`` pass, because ``git archive`` omits
them entirely.
"""

from __future__ import annotations

import os
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .checks import SL003, SL005, OperationalError, ValidationProblem
from .hashing import sha256_hex, sort_by_utf8_path
from .paths import check_case_collisions, validate_relative_path

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


def _popen_git(
    args: list[str], *, env: dict[str, str], cwd: Path | None = None
) -> subprocess.Popen:
    try:
        return subprocess.Popen(
            ["git", *args],
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise OperationalError("git executable not found on PATH") from exc
    except OSError as exc:
        raise OperationalError(f"cannot run git: {exc}") from exc


def _close_quietly(stream: BinaryIO | None) -> None:
    if stream is None:
        return
    try:
        stream.close()
    except OSError:  # pragma: no cover - best-effort cleanup
        pass


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
        files = _archive_snapshot(bare, commit, subdir, env, source=source)
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


def _archive_snapshot(
    bare: Path,
    commit: str,
    subdir: str,
    env: dict[str, str],
    *,
    source: str,
) -> tuple[SnapshotFile, ...]:
    args = ["archive", "--format=tar", commit]
    if subdir != ".":
        args += ["--", subdir]
    process = _popen_git(args, env=env, cwd=bare)
    evidence: Exception | None = None
    records: tuple[SnapshotFile, ...] = ()
    try:
        records = parse_archive_stream(
            process.stdout, subdir, source=source, commit=commit
        )
    except Exception as exc:
        evidence = exc
    finally:
        _close_quietly(process.stdout)
        stderr = process.stderr.read() if process.stderr is not None else b""
        _close_quietly(process.stderr)
        returncode = process.wait()
    if returncode != 0:
        raise ValidationProblem(
            SL003, f"cannot archive {commit}: {_first_line(stderr)}"
        )
    if evidence is not None:
        raise evidence
    return records


def _strip_prefix(name: str, subdir: str) -> str:
    """Strip leading ``./`` and the declared subdir prefix from a member name."""
    while name.startswith("./"):
        name = name[2:]
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


def _mode_from_member(member: tarfile.TarInfo) -> str:
    return "100755" if member.mode & 0o111 else "100644"


def _unsupported_kind(member: tarfile.TarInfo) -> str:
    if member.issym():
        return "symlink"
    if member.islnk():
        return "hardlink"
    if member.ischr() or member.isblk():
        return "device node"
    if member.isfifo():
        return "FIFO"
    return "unsupported object"


def parse_archive_stream(
    stream: BinaryIO,
    subdir: str,
    *,
    source: str,
    commit: str,
) -> tuple[SnapshotFile, ...]:
    """Read a ``git archive`` tar stream, one member at a time.

    Directory members are skipped (as in v0.1.0). Every regular-file member is
    normalized, validated with the shared path validator, and bounded by the
    file-count and total-size limits before its bytes are read. Members that
    are not regular files or directories are rejected as ``SL005``.

    Malformed, truncated, or unreadable streams raise ``OperationalError``
    (exit code 2, no rule ID).
    """
    records: list[SnapshotFile] = []
    total = 0
    try:
        with tarfile.open(fileobj=stream, mode="r|") as archive:
            for member in archive:
                if member.isdir():
                    continue
                if not member.isfile():
                    kind = _unsupported_kind(member)
                    raise ValidationProblem(
                        SL005, f"archive contains a {kind}: {member.name!r}"
                    )
                relative = _strip_prefix(member.name, subdir)
                validate_relative_path(relative)
                if len(records) >= MAX_FILES:
                    raise ValidationProblem(
                        SL005, f"snapshot exceeds the {MAX_FILES}-file limit"
                    )
                size = int(member.size)
                if size < 0:
                    raise OperationalError(
                        f"cannot read archive for {source} at {commit}: "
                        f"negative size for {member.name!r}"
                    )
                total += size
                if total > MAX_TOTAL_BYTES:
                    raise ValidationProblem(
                        SL005, f"snapshot exceeds the {MAX_TOTAL_BYTES}-byte limit"
                    )
                handle = archive.extractfile(member)
                if handle is None:
                    raise OperationalError(
                        f"cannot read archive entry {member.name!r} for {source} "
                        f"at {commit}"
                    )
                data = handle.read()
                if len(data) != size:
                    raise OperationalError(
                        f"truncated archive for {source} at {commit}: {member.name!r}"
                    )
                records.append(
                    SnapshotFile(
                        path=relative,
                        sha256=sha256_hex(data),
                        mode=_mode_from_member(member),
                        data=data,
                    )
                )
    except ValidationProblem:
        raise
    except OperationalError:
        raise
    except (tarfile.TarError, EOFError, OSError, ValueError) as exc:
        raise OperationalError(
            f"cannot read archive for {source} at {commit}: {exc}"
        ) from exc
    check_case_collisions(item.path for item in records)
    return tuple(sort_by_utf8_path(records))
