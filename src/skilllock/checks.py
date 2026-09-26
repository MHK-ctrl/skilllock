"""Stable check identifiers, findings, and error types shared by skilllock.

Every deterministic finding carries one of the ``SLxxx`` identifiers below so
that output is stable and machine-greppable. Operational failures (filesystem,
Git, or network) intentionally have no check ID: they return exit code ``2``.
"""

from __future__ import annotations

from dataclasses import dataclass

MANIFEST_NAME = "skills.toml"
LOCK_NAME = "skills.lock"
FORMAT_VERSION = 1

SL001 = "SL001"
SL002 = "SL002"
SL003 = "SL003"
SL004 = "SL004"
SL005 = "SL005"
SL006 = "SL006"

CHECK_DESCRIPTIONS = {
    SL001: "TOML is unparsable, the format version is unsupported, a required "
    "key is missing, an unknown key is present, or a field has the wrong type.",
    SL002: "A skill name, source URL, ref, or subdir is invalid, or a name is "
    "duplicated.",
    SL003: "A locked commit is missing/malformed/disagrees with the manifest, "
    "or a pinned commit does not resolve or match the lock.",
    SL004: "An installed file is missing, extra, has different bytes, or has a "
    "different executable mode.",
    SL005: "An unsafe path, traversal, symlink, unsupported archive object, or "
    "platform-colliding path was detected.",
    SL006: "The snapshot lacks a regular root-level SKILL.md, or its frontmatter "
    "name is missing, invalid, or does not match the manifest name.",
}


@dataclass(frozen=True)
class Finding:
    """A single deterministic validation finding."""

    id: str
    message: str

    def format(self) -> str:
        return f"{self.id}: {self.message}"


class SkilllockError(Exception):
    """Base class for expected skilllock failures."""


class ValidationProblem(SkilllockError):
    """A deterministic validation finding identified by a check ID."""

    def __init__(self, check_id: str, message: str) -> None:
        super().__init__(f"{check_id}: {message}")
        self.id = check_id
        self.message = message

    def as_finding(self) -> Finding:
        return Finding(self.id, self.message)


class OperationalError(SkilllockError):
    """Invalid usage, filesystem, Git, or network failure (exit code 2)."""
