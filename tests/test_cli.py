"""CLI behaviour and exit-code tests. None of these touch the network."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from skilllock import cli

SOURCE = "https://github.com/example/agent-skills.git"
NAME = "code-review"
FULL_SHA = "0" * 40


@pytest.fixture(autouse=True)
def _in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


def test_help_exits_zero() -> None:
    assert cli.main(["--help"]) == 0


def test_version_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--version"]) == 0
    assert "0.1.1" in capsys.readouterr().out


def test_unknown_command_exits_two() -> None:
    assert cli.main(["nope"]) == 2


def test_init_creates_manifest_then_refuses(tmp_path: Path) -> None:
    assert cli.main(["init"]) == 0
    assert (tmp_path / "skills.toml").is_file()
    assert cli.main(["init"]) == 2


def test_add_requires_init(tmp_path: Path) -> None:
    assert cli.main(["add", SOURCE, "--name", NAME, "--ref", "refs/tags/v1.2.0"]) == 2


def test_add_rejects_invalid_name(tmp_path: Path) -> None:
    assert cli.main(["init"]) == 0
    code = cli.main(["add", SOURCE, "--name", "Bad_Name", "--ref", "refs/tags/v1.2.0"])
    assert code == 1


def test_add_rejects_invalid_source() -> None:
    assert cli.main(["init"]) == 0
    code = cli.main(
        [
            "add",
            "http://github.com/example/agent-skills.git",
            "--name",
            NAME,
            "--ref",
            "refs/tags/v1.2.0",
        ]
    )
    assert code == 1


def test_add_appends_then_rejects_duplicates(tmp_path: Path) -> None:
    assert cli.main(["init"]) == 0
    assert cli.main(["add", SOURCE, "--name", NAME, "--ref", "refs/tags/v1.2.0"]) == 0
    text = (tmp_path / "skills.toml").read_text(encoding="utf-8")
    assert "[[skill]]" in text
    assert cli.main(["add", SOURCE, "--name", NAME, "--ref", "refs/tags/v1.2.0"]) == 1


def test_lock_without_manifest_exits_two() -> None:
    assert cli.main(["lock"]) == 2


def test_verify_missing_target_exits_two() -> None:
    assert cli.main(["init"]) == 0
    assert cli.main(["verify", "--target", "./does-not-exist"]) == 2


def test_verify_without_lock_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert cli.main(["init"]) == 0
    assert cli.main(["add", SOURCE, "--name", NAME, "--ref", "refs/tags/v1.2.0"]) == 0
    capsys.readouterr()
    assert cli.main(["verify", "--target", str(empty)]) == 2
    captured = capsys.readouterr()
    assert "no skills.lock found at" in captured.err
    assert "run `skilllock lock` first" in captured.err


def _craft(tmp_path: Path) -> Path:
    (tmp_path / "skills.toml").write_text(
        f'format = 1\n\n[[skill]]\nname = "{NAME}"\nsource = "{SOURCE}"\n'
        'ref = "refs/tags/v1.2.0"\nsubdir = "skills/code-review"\n',
        encoding="utf-8",
    )
    skill_dir = tmp_path / "skills" / NAME
    skill_dir.mkdir(parents=True)
    data = b"---\nname: code-review\n---\n# Code review\n"
    (skill_dir / "SKILL.md").write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    (tmp_path / "skills.lock").write_text(
        f'format = 1\n\n[[skill]]\nname = "{NAME}"\nsource = "{SOURCE}"\n'
        'ref = "refs/tags/v1.2.0"\nsubdir = "skills/code-review"\n'
        f'commit = "{FULL_SHA}"\n\n[[skill.files]]\npath = "SKILL.md"\n'
        f'sha256 = "{digest}"\nmode = "100644"\n',
        encoding="utf-8",
    )
    return tmp_path / "skills"


def test_verify_clean_crafted_install_exits_zero(tmp_path: Path) -> None:
    target = _craft(tmp_path)
    assert cli.main(["verify", "--target", str(target)]) == 0


def test_verify_drifted_target_exits_one(tmp_path: Path) -> None:
    target = _craft(tmp_path)
    (target / NAME / "SKILL.md").write_bytes(b"tampered\n")
    assert cli.main(["verify", "--target", str(target)]) == 1


def test_verify_missing_skill_md_exits_one(tmp_path: Path) -> None:
    target = _craft(tmp_path)
    (target / NAME / "SKILL.md").unlink()
    assert cli.main(["verify", "--target", str(target)]) == 1


def test_verify_malformed_lock_exits_one(tmp_path: Path) -> None:
    target = _craft(tmp_path)
    (tmp_path / "skills.lock").write_text(
        'format = 1\n\n[[skill]]\nname = "code-review"\n'
        f'source = "{SOURCE}"\nref = "refs/tags/v1.2.0"\n'
        'subdir = "skills/code-review"\n'
        'commit = "not-a-sha"\n\n[[skill.files]]\npath = "SKILL.md"\n'
        f'sha256 = "{"a" * 64}"\nmode = "100644"\n',
        encoding="utf-8",
    )
    assert cli.main(["verify", "--target", str(target)]) == 1


def test_verify_lock_manifest_disagreement_exits_one(tmp_path: Path) -> None:
    target = _craft(tmp_path)
    (tmp_path / "skills.lock").write_text(
        'format = 1\n\n[[skill]]\nname = "code-review"\n'
        f'source = "{SOURCE}"\nref = "refs/tags/v9.9.9"\n'
        'subdir = "skills/code-review"\n'
        f'commit = "{FULL_SHA}"\n\n[[skill.files]]\npath = "SKILL.md"\n'
        f'sha256 = "{"a" * 64}"\nmode = "100644"\n',
        encoding="utf-8",
    )
    assert cli.main(["verify", "--target", str(target)]) == 1
