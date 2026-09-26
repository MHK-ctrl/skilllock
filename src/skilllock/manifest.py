"""Parsing, validation, and writing of the ``skills.toml`` manifest.

The manifest is user-authored. Validation is strict: unknown keys, wrong types,
and unsupported format versions are ``SL001`` findings; invalid or duplicate
names, sources, refs, and subdirs are ``SL002`` findings.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import tomli_w
import yaml

from .checks import (
    FORMAT_VERSION,
    SL001,
    SL002,
    OperationalError,
    ValidationProblem,
)
from .paths import atomic_write_bytes, validate_relative_path

_NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_NAME_MAX = 64
_MANIFEST_KEYS = frozenset({"format", "skill"})
_ENTRY_KEYS = frozenset({"name", "source", "ref", "subdir"})
_GITHUB_SEGMENT_RE = re.compile(r"[A-Za-z0-9._-]+\Z")


@dataclass(frozen=True)
class SkillEntry:
    """One declared skill in ``skills.toml``."""

    name: str
    source: str
    ref: str
    subdir: str = "."


@dataclass(frozen=True)
class Manifest:
    """A parsed manifest."""

    format: int = FORMAT_VERSION
    skills: tuple[SkillEntry, ...] = field(default_factory=tuple)


def name_is_valid(name: str) -> bool:
    """Return whether ``name`` satisfies the skill-name grammar."""
    return (
        bool(name) and len(name) <= _NAME_MAX and _NAME_RE.fullmatch(name) is not None
    )


def validate_name(name: str) -> None:
    if not name_is_valid(name):
        raise ValidationProblem(SL002, f"invalid skill name: {name!r}")


def validate_source(source: str) -> None:
    """Validate an ``https://github.com/OWNER/REPO`` source URL."""
    try:
        parts = urlsplit(source)
    except ValueError as exc:
        raise ValidationProblem(SL002, f"invalid source URL: {source!r}") from exc
    if parts.scheme != "https":
        raise ValidationProblem(SL002, f"source must use https: {source!r}")
    if parts.username is not None or parts.password is not None:
        raise ValidationProblem(
            SL002, f"source must not contain credentials: {source!r}"
        )
    try:
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ValidationProblem(SL002, f"invalid source URL: {source!r}") from exc
    if host != "github.com":
        raise ValidationProblem(SL002, f"source host must be github.com: {source!r}")
    if port is not None:
        raise ValidationProblem(SL002, f"source must not specify a port: {source!r}")
    if parts.query or parts.fragment:
        raise ValidationProblem(
            SL002, f"source must not contain a query or fragment: {source!r}"
        )
    path = parts.path
    if not path.startswith("/") or len(path[1:].split("/")) != 2:
        raise ValidationProblem(
            SL002, f"source must be https://github.com/OWNER/REPO: {source!r}"
        )
    owner, repo = path[1:].split("/")
    if repo.endswith(".git"):
        repo = repo[:-4]
    for label, value in (("owner", owner), ("repository", repo)):
        if value in {".", ".."} or _GITHUB_SEGMENT_RE.fullmatch(value) is None:
            raise ValidationProblem(SL002, f"invalid {label} in source: {source!r}")


def validate_ref(ref: str) -> None:
    """Validate a full ``refs/heads/...`` or ``refs/tags/...`` ref."""
    if not isinstance(ref, str) or not (
        ref.startswith("refs/heads/") or ref.startswith("refs/tags/")
    ):
        raise ValidationProblem(
            SL002, f"ref must be a full refs/heads/... or refs/tags/... ref: {ref!r}"
        )
    try:
        proc = subprocess.run(
            ["git", "check-ref-format", ref],
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise OperationalError("git executable not found on PATH") from exc
    except OSError as exc:
        raise OperationalError(f"cannot run git: {exc}") from exc
    if proc.returncode != 0:
        raise ValidationProblem(SL002, f"invalid ref: {ref!r}")


def validate_subdir(subdir: str) -> None:
    """Validate an optional repository-relative POSIX directory."""
    if not isinstance(subdir, str):
        raise ValidationProblem(SL001, "subdir must be a string")
    if subdir == ".":
        return
    try:
        validate_relative_path(subdir)
    except ValidationProblem as exc:
        raise ValidationProblem(SL002, f"invalid subdir: {subdir!r}") from exc


def validate_entry(entry: SkillEntry) -> None:
    """Run all semantic validations for a single manifest entry."""
    validate_name(entry.name)
    validate_source(entry.source)
    validate_ref(entry.ref)
    validate_subdir(entry.subdir)


def _parse_entry(raw: dict[str, object], index: int) -> SkillEntry:
    unknown = set(raw) - _ENTRY_KEYS
    if unknown:
        raise ValidationProblem(
            SL001, f"skill[{index}] has unknown key(s): {sorted(unknown)}"
        )
    for key in ("name", "source", "ref"):
        if key not in raw:
            raise ValidationProblem(
                SL001, f"skill[{index}] is missing required key: {key!r}"
            )
        if not isinstance(raw[key], str):
            raise ValidationProblem(SL001, f"skill[{index}].{key} must be a string")
    if "subdir" in raw and not isinstance(raw["subdir"], str):
        raise ValidationProblem(SL001, f"skill[{index}].subdir must be a string")
    entry = SkillEntry(
        name=raw["name"],
        source=raw["source"],
        ref=raw["ref"],
        subdir=raw.get("subdir", "."),
    )
    validate_name(entry.name)
    validate_source(entry.source)
    validate_ref(entry.ref)
    validate_subdir(entry.subdir)
    return entry


def parse_manifest(data: dict[str, object]) -> Manifest:
    unknown = set(data) - _MANIFEST_KEYS
    if unknown:
        raise ValidationProblem(
            SL001, f"manifest has unknown key(s): {sorted(unknown)}"
        )
    fmt = data.get("format")
    if not isinstance(fmt, int) or isinstance(fmt, bool):
        raise ValidationProblem(SL001, "manifest 'format' must be an integer")
    if fmt != FORMAT_VERSION:
        raise ValidationProblem(SL001, f"unsupported manifest format version: {fmt}")
    raw_skills = data.get("skill")
    if not isinstance(raw_skills, list):
        raise ValidationProblem(SL001, "manifest 'skill' must be an array of tables")
    skills: list[SkillEntry] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_skills):
        if not isinstance(raw, dict):
            raise ValidationProblem(SL001, f"skill[{index}] must be a table")
        entry = _parse_entry(raw, index)
        if entry.name in seen:
            raise ValidationProblem(SL002, f"duplicate skill name: {entry.name!r}")
        seen.add(entry.name)
        skills.append(entry)
    return Manifest(format=fmt, skills=tuple(skills))


def load_manifest(path: Path) -> Manifest:
    """Read and fully validate ``skills.toml``."""
    if not path.is_file():
        raise OperationalError(f"{path} not found; run 'skilllock init' first")
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
    return parse_manifest(data)


def _manifest_data(manifest: Manifest) -> dict[str, object]:
    return {
        "format": manifest.format,
        "skill": [
            {
                "name": entry.name,
                "source": entry.source,
                "ref": entry.ref,
                "subdir": entry.subdir,
            }
            for entry in manifest.skills
        ],
    }


def write_manifest(path: Path, manifest: Manifest) -> None:
    payload = tomli_w.dumps(_manifest_data(manifest))
    atomic_write_bytes(path, payload.encode("utf-8"))


def add_skill(manifest: Manifest, entry: SkillEntry) -> Manifest:
    """Return a new manifest with ``entry`` appended, rejecting duplicates."""
    if any(existing.name == entry.name for existing in manifest.skills):
        raise ValidationProblem(SL002, f"duplicate skill name: {entry.name!r}")
    return Manifest(format=manifest.format, skills=(*manifest.skills, entry))


def read_frontmatter_name(data: bytes) -> str | None:
    """Minimal YAML frontmatter parse: return the top-level ``name`` or None."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        return None
    try:
        metadata = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError:
        return None
    if not isinstance(metadata, dict):
        return None
    name = metadata.get("name")
    if not isinstance(name, str):
        return None
    return name
