"""Content hashing and deterministic ordering helpers.

Hashes are plain lowercase ``sha256`` digests over the exact archived bytes of
each regular file. There is deliberately no aggregate tree hash: a lock is a
list of per-file digests, sorted by UTF-8 path bytes so output is reproducible.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Protocol, TypeVar


class _HasPath(Protocol):
    path: str


T = TypeVar("T", bound=_HasPath)


def sha256_hex(data: bytes) -> str:
    """Return the lowercase hexadecimal sha256 digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def sort_by_utf8_path(records: Iterable[T]) -> list[T]:
    """Sort records by their path encoded as UTF-8 bytes."""
    return sorted(records, key=lambda record: record.path.encode("utf-8"))
