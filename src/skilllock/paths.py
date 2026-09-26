"""Path-safety validation and atomic filesystem helpers.

Everything that arrives from a fetched repository is treated as untrusted
data. Paths are validated before use, symlinked destination components are
rejected, installs are staged in a temporary sibling directory and moved into
place with an atomic rename, and manifest/lock writes go through a temporary
file followed by ``os.replace``.
"""

from __future__ import annotations

import os
import tempfile
import unicodedata
from collections.abc import Iterable
from pathlib import Path

from .checks import SL005, OperationalError, ValidationProblem

_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)
_FORBIDDEN_CHARS = frozenset("\\:")


def _fail(message: str) -> None:
    raise ValidationProblem(SL005, message)


def validate_relative_path(path: str) -> tuple[str, ...]:
    """Validate a repository-relative POSIX path and return its components.

    Rejects empty paths, absolute paths, ``.``/``..``/empty components,
    backslashes, colons, NUL bytes, non-NFC names, trailing dots or spaces,
    and Windows reserved device names.
    """
    if not isinstance(path, str) or path == "":
        _fail("empty path is not allowed")
    if "\x00" in path:
        _fail("path contains a NUL byte")
    if "\\" in path:
        _fail(f"path contains a backslash: {path!r}")
    if path.startswith("/"):
        _fail(f"absolute path is not allowed: {path!r}")
    if unicodedata.normalize("NFC", path) != path:
        _fail(f"path is not Unicode NFC: {path!r}")
    components = path.split("/")
    for component in components:
        if component == "":
            _fail(f"path has an empty component: {path!r}")
        if component in {".", ".."}:
            _fail(f"path has a traversal component: {path!r}")
        if any(char in _FORBIDDEN_CHARS for char in component):
            _fail(f"path contains a forbidden character: {path!r}")
        if component[-1] in {" ", "."}:
            _fail(f"path component has a trailing dot or space: {path!r}")
        if component.split(".", 1)[0].upper() in _RESERVED_NAMES:
            _fail(f"path uses a reserved device name: {path!r}")
    return tuple(components)


def check_case_collisions(paths: Iterable[str]) -> None:
    """Reject paths that collide when compared case-insensitively."""
    seen: dict[str, str] = {}
    for path in paths:
        key = path.casefold()
        previous = seen.get(key)
        if previous is not None and previous != path:
            _fail(f"case-insensitive path collision: {previous!r} and {path!r}")
        seen.setdefault(key, path)


def _reject_symlink(raw: str, given: Path) -> None:
    if given.is_symlink():
        _fail(f"target is a symlink: {raw}")


def resolve_existing_directory(raw: str) -> Path:
    """Return the real path of an existing target directory, or fail."""
    given = Path(raw)
    _reject_symlink(raw, given)
    if not given.exists():
        raise OperationalError(f"target directory does not exist: {raw}")
    if not given.is_dir():
        raise OperationalError(f"target is not a directory: {raw}")
    return Path(os.path.realpath(given))


def create_directory(raw: str) -> Path:
    """Return the real path of a target directory, creating it if needed."""
    given = Path(raw)
    _reject_symlink(raw, given)
    resolved = Path(os.path.realpath(given))
    if resolved.exists():
        if resolved.is_symlink():
            _fail(f"target is a symlink: {raw}")
        if not resolved.is_dir():
            raise OperationalError(f"target is not a directory: {raw}")
        return resolved
    try:
        resolved.mkdir(parents=True)
    except OSError as exc:
        raise OperationalError(f"cannot create target directory {raw}: {exc}") from exc
    return resolved


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically within the same directory."""
    directory = path.parent if str(path.parent) else Path(".")
    try:
        handle_fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=directory)
    except OSError as exc:
        raise OperationalError(f"cannot write {path}: {exc}") from exc
    temp = Path(temp_name)
    try:
        with os.fdopen(handle_fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise OperationalError(f"cannot write {path}: {exc}") from exc
