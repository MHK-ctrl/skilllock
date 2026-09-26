"""Reading, writing, and building ``skills.lock``.

The lock is generated, never hand-edited. It records the pinned commit and a
sorted per-file ``sha256``/mode listing for every manifest entry. Reading is
strict: unknown keys, wrong types, and unsupported versions are ``SL001``;
malformed or disagreeing commits are ``SL003``.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

import tomli_w

from .checks import (
    FORMAT_VERSION,
    SL001,
    SL003,
    SL006,
    OperationalError,
    ValidationProblem,
)
from .gitops import Snapshot, fetch_snapshot
from .hashing import sort_by_utf8_path
from .manifest import Manifest, SkillEntry, name_is_valid, read_frontmatter_name
from .paths import atomic_write_bytes, validate_relative_path

_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_LOCK_KEYS = frozenset({"format", "skill"})
_SKILL_KEYS = frozenset({"name", "source", "ref", "subdir", "commit", "files"})
_FILE_KEYS = frozenset({"path", "sha256", "mode"})
_VALID_MODES = frozenset({"100644", "100755"})


@dataclass(frozen=True)
class LockedFile:
    """One pinned file record."""

    path: str
    sha256: str
    mode: str


@dataclass(frozen=True)
class LockedSkill:
    """One fully pinned skill."""

    name: str
    source: str
    ref: str
    subdir: str
    commit: str
    files: tuple[LockedFile, ...]


def _locked_from_snapshot(entry: SkillEntry, snapshot: Snapshot) -> LockedSkill:
    if snapshot.skill_md is None:
        raise ValidationProblem(
            SL006, f"{entry.name}: snapshot has no regular root-level SKILL.md"
        )
    frontmatter_name = read_frontmatter_name(snapshot.skill_md)
    if frontmatter_name is None or not name_is_valid(frontmatter_name):
        raise ValidationProblem(
            SL006, f"{entry.name}: SKILL.md frontmatter name is missing or invalid"
        )
    if frontmatter_name != entry.name:
        raise ValidationProblem(
            SL006,
            f"{entry.name}: SKILL.md name {frontmatter_name!r} does not match "
            f"the manifest name",
        )
    files = tuple(
        LockedFile(path=item.path, sha256=item.sha256, mode=item.mode)
        for item in snapshot.files
    )
    return LockedSkill(
        name=entry.name,
        source=entry.source,
        ref=entry.ref,
        subdir=entry.subdir,
        commit=snapshot.commit,
        files=files,
    )


def build_lock(
    manifest: Manifest, *, allow_file: bool = False
) -> tuple[LockedSkill, ...]:
    """Fetch every declared ref and build the pinned lock entries."""
    locked: list[LockedSkill] = []
    for entry in manifest.skills:
        snapshot = fetch_snapshot(
            entry.source, entry.ref, entry.subdir, allow_file=allow_file
        )
        locked.append(_locked_from_snapshot(entry, snapshot))
    return tuple(locked)


def _lock_data(skills: tuple[LockedSkill, ...]) -> dict[str, object]:
    return {
        "format": FORMAT_VERSION,
        "skill": [
            {
                "name": skill.name,
                "source": skill.source,
                "ref": skill.ref,
                "subdir": skill.subdir,
                "commit": skill.commit,
                "files": [
                    {"path": item.path, "sha256": item.sha256, "mode": item.mode}
                    for item in skill.files
                ],
            }
            for skill in skills
        ],
    }


def write_lock(path: Path, skills: tuple[LockedSkill, ...]) -> None:
    payload = tomli_w.dumps(_lock_data(skills))
    atomic_write_bytes(path, payload.encode("utf-8"))


def _parse_file(raw: object, skill_index: int, file_index: int) -> LockedFile:
    if not isinstance(raw, dict):
        raise ValidationProblem(
            SL001, f"skill[{skill_index}].files[{file_index}] must be a table"
        )
    unknown = set(raw) - _FILE_KEYS
    if unknown:
        raise ValidationProblem(
            SL001,
            f"skill[{skill_index}].files[{file_index}] has unknown key(s): "
            f"{sorted(unknown)}",
        )
    for key in _FILE_KEYS:
        if key not in raw:
            raise ValidationProblem(
                SL001,
                f"skill[{skill_index}].files[{file_index}] is missing key: {key!r}",
            )
        if not isinstance(raw[key], str):
            raise ValidationProblem(
                SL001,
                f"skill[{skill_index}].files[{file_index}].{key} must be a string",
            )
    path = raw["path"]
    digest = raw["sha256"]
    mode = raw["mode"]
    validate_relative_path(path)
    if _SHA256_RE.fullmatch(digest) is None:
        raise ValidationProblem(SL001, f"malformed sha256 for {path!r}")
    if mode not in _VALID_MODES:
        raise ValidationProblem(SL001, f"malformed mode for {path!r}: {mode!r}")
    return LockedFile(path=path, sha256=digest, mode=mode)


def _parse_skill(raw: object, index: int) -> LockedSkill:
    if not isinstance(raw, dict):
        raise ValidationProblem(SL001, f"skill[{index}] must be a table")
    unknown = set(raw) - _SKILL_KEYS
    if unknown:
        raise ValidationProblem(
            SL001, f"skill[{index}] has unknown key(s): {sorted(unknown)}"
        )
    for key in _SKILL_KEYS:
        if key not in raw:
            raise ValidationProblem(
                SL001, f"skill[{index}] is missing required key: {key!r}"
            )
    for key in ("name", "source", "ref", "subdir"):
        if not isinstance(raw[key], str):
            raise ValidationProblem(SL001, f"skill[{index}].{key} must be a string")
    commit = raw["commit"]
    if not isinstance(commit, str) or _COMMIT_RE.fullmatch(commit) is None:
        raise ValidationProblem(
            SL003, f"skill[{index}] commit is missing or malformed: {commit!r}"
        )
    raw_files = raw["files"]
    if not isinstance(raw_files, list) or not raw_files:
        raise ValidationProblem(
            SL001, f"skill[{index}].files must be a non-empty array"
        )
    files: list[LockedFile] = []
    seen: set[str] = set()
    for file_index, item in enumerate(raw_files):
        record = _parse_file(item, index, file_index)
        if record.path in seen:
            raise ValidationProblem(
                SL001, f"skill[{index}] has duplicate file: {record.path!r}"
            )
        seen.add(record.path)
        files.append(record)
    return LockedSkill(
        name=raw["name"],
        source=raw["source"],
        ref=raw["ref"],
        subdir=raw["subdir"],
        commit=commit,
        files=tuple(sort_by_utf8_path(files)),
    )


def parse_lock(data: dict[str, object]) -> tuple[LockedSkill, ...]:
    unknown = set(data) - _LOCK_KEYS
    if unknown:
        raise ValidationProblem(SL001, f"lock has unknown key(s): {sorted(unknown)}")
    fmt = data.get("format")
    if not isinstance(fmt, int) or isinstance(fmt, bool):
        raise ValidationProblem(SL001, "lock 'format' must be an integer")
    if fmt != FORMAT_VERSION:
        raise ValidationProblem(SL001, f"unsupported lock format version: {fmt}")
    raw_skills = data.get("skill", [])
    if not isinstance(raw_skills, list):
        raise ValidationProblem(SL001, "lock 'skill' must be an array of tables")
    skills: list[LockedSkill] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_skills):
        skill = _parse_skill(raw, index)
        if skill.name in seen:
            raise ValidationProblem(
                SL001, f"lock has duplicate skill name: {skill.name!r}"
            )
        seen.add(skill.name)
        skills.append(skill)
    return tuple(skills)


def read_lock(path: Path) -> tuple[LockedSkill, ...]:
    """Read and validate ``skills.lock``."""
    if not path.is_file():
        raise OperationalError(f"{path} not found; run 'skilllock lock' first")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise OperationalError(f"cannot read {path}: {exc}") from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ValidationProblem(SL001, f"cannot parse {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationProblem(SL001, f"{path.name} root must be a table")
    return parse_lock(data)


def check_agreement(manifest: Manifest, skills: tuple[LockedSkill, ...]) -> None:
    """Require a one-to-one, field-consistent match between manifest and lock."""
    manifest_by_name = {entry.name: entry for entry in manifest.skills}
    lock_by_name = {skill.name: skill for skill in skills}
    missing = sorted(set(manifest_by_name) - set(lock_by_name))
    extra = sorted(set(lock_by_name) - set(manifest_by_name))
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing from lock: {missing}")
        if extra:
            details.append(f"not in manifest: {extra}")
        raise ValidationProblem(
            SL003, "lock does not match manifest (" + "; ".join(details) + ")"
        )
    for name, entry in manifest_by_name.items():
        locked = lock_by_name[name]
        for field_name in ("source", "ref", "subdir"):
            if getattr(locked, field_name) != getattr(entry, field_name):
                raise ValidationProblem(
                    SL003,
                    f"{name}: lock {field_name} {getattr(locked, field_name)!r} "
                    f"disagrees with manifest {getattr(entry, field_name)!r}",
                )
