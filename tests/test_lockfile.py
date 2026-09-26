"""Manifest (SL001/SL002) and lockfile round-trip tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from skilllock.checks import ValidationProblem
from skilllock.lockfile import (
    LockedFile,
    LockedSkill,
    check_agreement,
    parse_lock,
    read_lock,
    write_lock,
)
from skilllock.manifest import (
    Manifest,
    SkillEntry,
    parse_manifest,
    read_frontmatter_name,
    write_manifest,
)

VALID_SOURCE = "https://github.com/example/agent-skills.git"
FULL_SHA = "0123456789abcdef0123456789abcdef01234567"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def _entry(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "name": "code-review",
        "source": VALID_SOURCE,
        "ref": "refs/tags/v1.2.0",
        "subdir": "skills/code-review",
    }
    entry.update(overrides)
    return entry


def _manifest(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {"format": 1, "skill": [_entry()]}
    data.update(overrides)
    return data


def test_parse_valid_manifest() -> None:
    manifest = parse_manifest(_manifest())
    assert manifest.format == 1
    assert manifest.skills == (
        SkillEntry(
            "code-review", VALID_SOURCE, "refs/tags/v1.2.0", "skills/code-review"
        ),
    )


def test_subdir_defaults_to_dot() -> None:
    data = _manifest(
        skill=[{"name": "a", "source": VALID_SOURCE, "ref": "refs/heads/main"}]
    )
    assert parse_manifest(data).skills[0].subdir == "."


@pytest.mark.parametrize(
    "data",
    [
        _manifest(format="1"),
        _manifest(format=2),
        _manifest(extra=1),
        _manifest(skill={"name": "a"}),
        _manifest(skill=[{"source": VALID_SOURCE, "ref": "refs/heads/main"}]),
        _manifest(skill=[_entry(extra="x")]),
        _manifest(skill=[_entry(name=1)]),
        _manifest(skill=[_entry(subdir=1)]),
    ],
)
def test_manifest_sl001_cases(data: dict[str, object]) -> None:
    with pytest.raises(ValidationProblem) as info:
        parse_manifest(data)
    assert info.value.id == "SL001"


def test_unparsable_toml_is_sl001(tmp_path: Path) -> None:
    from skilllock.manifest import load_manifest

    path = tmp_path / "skills.toml"
    path.write_text("format = = 1\n", encoding="utf-8")
    with pytest.raises(ValidationProblem) as info:
        load_manifest(path)
    assert info.value.id == "SL001"


@pytest.mark.parametrize(
    "data",
    [
        _manifest(skill=[_entry(name="Bad-Name")]),
        _manifest(skill=[_entry(name="double--hyphen")]),
        _manifest(skill=[_entry(name="")]),
        _manifest(skill=[_entry(), _entry()]),
        _manifest(skill=[_entry(source="http://github.com/a/b")]),
        _manifest(skill=[_entry(source="https://user:pw@github.com/a/b")]),
        _manifest(skill=[_entry(source="https://gitlab.com/a/b")]),
        _manifest(skill=[_entry(source="https://github.com/a/b?x=1")]),
        _manifest(skill=[_entry(source="https://github.com/a/b#frag")]),
        _manifest(skill=[_entry(source="https://github.com/a/b/c")]),
        _manifest(skill=[_entry(ref="v1.2.0")]),
        _manifest(skill=[_entry(ref="refs/tags/bad..ref")]),
        _manifest(skill=[_entry(subdir="../escape")]),
        _manifest(skill=[_entry(subdir="/absolute")]),
    ],
)
def test_manifest_sl002_cases(data: dict[str, object]) -> None:
    with pytest.raises(ValidationProblem) as info:
        parse_manifest(data)
    assert info.value.id == "SL002"


def test_manifest_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "skills.toml"
    manifest = Manifest(
        format=1,
        skills=(
            SkillEntry("code-review", VALID_SOURCE, "refs/tags/v1.2.0", "skills/ci"),
        ),
    )
    write_manifest(path, manifest)
    reloaded = parse_manifest(_load(path))
    assert reloaded == manifest


def _load(path: Path) -> dict[str, object]:
    import tomllib

    return tomllib.loads(path.read_text(encoding="utf-8"))


def _locked(**overrides: object) -> LockedSkill:
    fields: dict[str, object] = {
        "name": "code-review",
        "source": VALID_SOURCE,
        "ref": "refs/tags/v1.2.0",
        "subdir": "skills/code-review",
        "commit": FULL_SHA,
        "files": (
            LockedFile("SKILL.md", DIGEST_A, "100644"),
            LockedFile("references/checklist.md", DIGEST_B, "100644"),
        ),
    }
    fields.update(overrides)
    return LockedSkill(**fields)  # type: ignore[arg-type]


def test_lock_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "skills.lock"
    original = _locked()
    write_lock(path, (original,))
    reloaded = read_lock(path)
    assert reloaded == (original,)


def test_lock_sorts_files_by_path_bytes(tmp_path: Path) -> None:
    path = tmp_path / "skills.lock"
    messy = _locked(
        files=(
            LockedFile("references/checklist.md", DIGEST_B, "100644"),
            LockedFile("SKILL.md", DIGEST_A, "100644"),
        )
    )
    write_lock(path, (messy,))
    (reloaded,) = read_lock(path)
    assert [item.path for item in reloaded.files] == [
        "SKILL.md",
        "references/checklist.md",
    ]


@pytest.mark.parametrize(
    "data",
    [
        {"format": 2, "skill": []},
        {"format": 1, "unknown": True},
        {"format": 1, "skill": [{"name": "a"}]},
        {
            "format": 1,
            "skill": [
                {
                    "name": "a",
                    "source": VALID_SOURCE,
                    "ref": "refs/tags/v1",
                    "subdir": ".",
                    "commit": FULL_SHA,
                    "files": [
                        {"path": "SKILL.md", "sha256": DIGEST_A, "mode": "120000"}
                    ],
                }
            ],
        },
        {
            "format": 1,
            "skill": [
                {
                    "name": "a",
                    "source": VALID_SOURCE,
                    "ref": "refs/tags/v1",
                    "subdir": ".",
                    "commit": FULL_SHA,
                    "files": [{"path": "SKILL.md", "sha256": "zz", "mode": "100644"}],
                }
            ],
        },
        {
            "format": 1,
            "skill": [
                {
                    "name": "a",
                    "source": VALID_SOURCE,
                    "ref": "refs/tags/v1",
                    "subdir": ".",
                    "commit": FULL_SHA,
                    "files": [],
                }
            ],
        },
    ],
)
def test_lock_sl001_cases(data: dict[str, object]) -> None:
    with pytest.raises(ValidationProblem) as info:
        parse_lock(data)
    assert info.value.id == "SL001"


@pytest.mark.parametrize(
    "commit", ["", "abc", "Z" * 40, "0123456789ABCDEF0123456789ABCDEF01234567"]
)
def test_lock_sl003_bad_commit(commit: str) -> None:
    with pytest.raises(ValidationProblem) as info:
        parse_lock(_lock_data(commit=commit))
    assert info.value.id == "SL003"


def _lock_data(**overrides: object) -> dict[str, object]:
    skill: dict[str, object] = {
        "name": "code-review",
        "source": VALID_SOURCE,
        "ref": "refs/tags/v1.2.0",
        "subdir": "skills/code-review",
        "commit": FULL_SHA,
        "files": [{"path": "SKILL.md", "sha256": DIGEST_A, "mode": "100644"}],
    }
    skill.update(overrides)
    return {"format": 1, "skill": [skill]}


def test_check_agreement_rejects_missing_and_extra() -> None:
    manifest = Manifest(
        format=1, skills=(SkillEntry("a", VALID_SOURCE, "refs/tags/v1"),)
    )
    with pytest.raises(ValidationProblem) as info:
        check_agreement(manifest, ())
    assert info.value.id == "SL003"


def test_check_agreement_rejects_field_mismatch() -> None:
    manifest = Manifest(
        format=1, skills=(SkillEntry("a", VALID_SOURCE, "refs/tags/v1"),)
    )
    locked = _locked(name="a", ref="refs/tags/v2")
    with pytest.raises(ValidationProblem) as info:
        check_agreement(manifest, (locked,))
    assert info.value.id == "SL003"


def test_check_agreement_accepts_exact_match() -> None:
    entry = SkillEntry(
        "code-review", VALID_SOURCE, "refs/tags/v1.2.0", "skills/code-review"
    )
    manifest = Manifest(format=1, skills=(entry,))
    check_agreement(manifest, (_locked(),))


@pytest.mark.parametrize(
    "text,expected",
    [
        ("---\nname: code-review\n---\n", "code-review"),
        ("---\nname: x\ndescription: y\n---\nbody", "x"),
        ("# no frontmatter", None),
        ("---\nname: [oops\n---\n", None),
        ("---\ndescription: y\n---\n", None),
        ("---\nname: 12\n---\n", None),
    ],
)
def test_read_frontmatter_name(text: str, expected: str | None) -> None:
    assert read_frontmatter_name(text.encode("utf-8")) == expected
