"""Path-safety (SL005) and deterministic ordering tests."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from skilllock.checks import ValidationProblem
from skilllock.hashing import sha256_hex, sort_by_utf8_path
from skilllock.paths import check_case_collisions, validate_relative_path

VALID_PATHS = [
    "SKILL.md",
    "references/checklist.md",
    "a/b/c.txt",
    "UPPER.md",
    "with space.md",
    "dash-name.md",
    "conifer.md",
    "nullable.md",
]


@pytest.mark.parametrize("path", VALID_PATHS)
def test_valid_paths_are_accepted(path: str) -> None:
    assert validate_relative_path(path) == tuple(path.split("/"))


INVALID_PATHS = [
    "",
    "/absolute",
    "trailing/",
    "double//slash",
    "./leading-dot",
    "a/./b",
    "../escape",
    "a/../../b",
    "back\\slash",
    "colon:name",
    "nul",
    "con.txt",
    "aux",
    "com1.md",
    "lpt9",
    "trailing.",
    "trailing ",
    "sub/trailing.",
    "e\u0301.md",
]


@pytest.mark.parametrize("path", INVALID_PATHS)
def test_invalid_paths_are_rejected_as_sl005(path: str) -> None:
    with pytest.raises(ValidationProblem) as info:
        validate_relative_path(path)
    assert info.value.id == "SL005"


def test_case_insensitive_collision_is_rejected() -> None:
    with pytest.raises(ValidationProblem) as info:
        check_case_collisions(["README.md", "readme.md"])
    assert info.value.id == "SL005"


def test_exact_duplicates_are_not_a_collision() -> None:
    check_case_collisions(["a.md", "a.md", "b.md"])


def test_sha256_hex_is_lowercase_hex() -> None:
    assert sha256_hex(b"") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


@dataclass(frozen=True)
class _Record:
    path: str


def test_sorting_uses_utf8_path_bytes() -> None:
    records = [_Record("b.md"), _Record("A.md"), _Record("a.md")]
    ordered = sort_by_utf8_path(records)
    assert [record.path for record in ordered] == ["A.md", "a.md", "b.md"]
