"""Sync/verify drift (SL004), SL005, and SL006 tests over local Git repos.

No test performs a real network call: every repository is created in a pytest
``tmp_path`` and fetched through the ``file://`` transport, which the library
enables only via its explicit ``allow_file`` test seam.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from skilllock.checks import ValidationProblem
from skilllock.gitops import fetch_snapshot
from skilllock.lockfile import build_lock, read_lock, write_lock
from skilllock.manifest import Manifest, SkillEntry
from skilllock.sync import sync_project, verify_project

FIXTURE = Path(__file__).parent / "fixtures" / "valid-skill"
NAME = "valid-skill"
SUBDIR = "skills/valid-skill"
REF = "refs/tags/v1.2.0"


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True
    )
    return result.stdout.decode("utf-8").strip()


def _make_remote(
    tmp_path: Path, *, skill_md: bytes | None = None, symlink: bool = False
) -> str:
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    _git("config", "user.email", "test@example.com", cwd=work)
    _git("config", "user.name", "Test", cwd=work)
    skill_dir = work / SUBDIR
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_bytes(
        skill_md if skill_md is not None else (FIXTURE / "SKILL.md").read_bytes()
    )
    (skill_dir / "references" / "checklist.md").write_bytes(
        (FIXTURE / "references" / "checklist.md").read_bytes()
    )
    if symlink:
        os.symlink("SKILL.md", skill_dir / "link.md")
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "init", cwd=work)
    _git("tag", REF.removeprefix("refs/tags/"), cwd=work)
    bare = tmp_path / "remote.git"
    _git("init", "-q", "--bare", str(bare), cwd=tmp_path)
    _git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=bare)
    _git(
        "-c",
        "protocol.file.allow=always",
        "push",
        "-q",
        str(bare),
        "refs/heads/main:refs/heads/main",
        "refs/tags/v1.2.0:refs/tags/v1.2.0",
        cwd=work,
    )
    return f"file://{bare}"


def _manifest(source: str) -> Manifest:
    entry = SkillEntry(name=NAME, source=source, ref=REF, subdir=SUBDIR)
    return Manifest(format=1, skills=(entry,))


def _lock(source: str, tmp_path: Path):
    manifest = _manifest(source)
    locked = build_lock(manifest, allow_file=True)
    write_lock(tmp_path / "skills.lock", locked)
    return manifest, read_lock(tmp_path / "skills.lock")


def test_build_lock_pins_commit_and_hashes(tmp_path: Path) -> None:
    source = _make_remote(tmp_path)
    expected_commit = _git("rev-parse", "HEAD", cwd=tmp_path / "work")
    locked = build_lock(_manifest(source), allow_file=True)
    (skill,) = locked
    assert skill.commit == expected_commit
    assert [item.path for item in skill.files] == [
        "SKILL.md",
        "references/checklist.md",
    ]
    assert all(len(item.sha256) == 64 for item in skill.files)
    assert {item.mode for item in skill.files} == {"100644"}


def test_sync_installs_then_verify_is_clean(tmp_path: Path) -> None:
    source = _make_remote(tmp_path)
    manifest, locked = _lock(source, tmp_path)
    target = tmp_path / "skills"

    report = sync_project(manifest, locked, str(target), allow_file=True)
    assert report.findings == []
    assert (target / NAME / "SKILL.md").is_file()

    verification = verify_project(manifest, locked, target)
    assert verification.findings == []


def test_sync_is_a_noop_when_identical(tmp_path: Path) -> None:
    source = _make_remote(tmp_path)
    manifest, locked = _lock(source, tmp_path)
    target = tmp_path / "skills"

    assert sync_project(manifest, locked, str(target), allow_file=True).findings == []
    assert sync_project(manifest, locked, str(target), allow_file=True).findings == []


def test_verify_reports_content_drift(tmp_path: Path) -> None:
    source = _make_remote(tmp_path)
    manifest, locked = _lock(source, tmp_path)
    target = tmp_path / "skills"
    sync_project(manifest, locked, str(target), allow_file=True)

    (target / NAME / "SKILL.md").write_bytes(b"tampered\n")
    report = verify_project(manifest, locked, target)
    assert [finding.id for finding in report.findings] == ["SL004", "SL006"]


def test_verify_reports_missing_skill_md(tmp_path: Path) -> None:
    source = _make_remote(tmp_path)
    manifest, locked = _lock(source, tmp_path)
    target = tmp_path / "skills"
    sync_project(manifest, locked, str(target), allow_file=True)

    (target / NAME / "SKILL.md").unlink()
    report = verify_project(manifest, locked, target)
    assert any(finding.id == "SL004" for finding in report.findings)
    assert any(finding.id == "SL006" for finding in report.findings)


def test_verify_reports_mode_drift(tmp_path: Path) -> None:
    source = _make_remote(tmp_path)
    manifest, locked = _lock(source, tmp_path)
    target = tmp_path / "skills"
    sync_project(manifest, locked, str(target), allow_file=True)

    os.chmod(target / NAME / "SKILL.md", 0o755)
    report = verify_project(manifest, locked, target)
    assert any(
        finding.id == "SL004" and "mode" in finding.message
        for finding in report.findings
    )


def test_verify_reports_unexpected_extra_file(tmp_path: Path) -> None:
    source = _make_remote(tmp_path)
    manifest, locked = _lock(source, tmp_path)
    target = tmp_path / "skills"
    sync_project(manifest, locked, str(target), allow_file=True)

    (target / NAME / "extra.md").write_bytes(b"extra\n")
    report = verify_project(manifest, locked, target)
    assert any(finding.id == "SL004" for finding in report.findings)


def test_sync_refuses_to_overwrite_a_different_install(tmp_path: Path) -> None:
    source = _make_remote(tmp_path)
    manifest, locked = _lock(source, tmp_path)
    target = tmp_path / "skills"
    sync_project(manifest, locked, str(target), allow_file=True)

    (target / NAME / "SKILL.md").write_bytes(b"tampered\n")
    report = sync_project(manifest, locked, str(target), allow_file=True)
    assert any(
        "refusing to overwrite" in finding.message for finding in report.findings
    )


def test_unresolvable_ref_is_sl003(tmp_path: Path) -> None:
    source = _make_remote(tmp_path)
    with pytest.raises(ValidationProblem) as info:
        fetch_snapshot(source, "refs/heads/missing", SUBDIR, allow_file=True)
    assert info.value.id == "SL003"


def test_symlink_in_repository_is_sl005(tmp_path: Path) -> None:
    source = _make_remote(tmp_path, symlink=True)
    with pytest.raises(ValidationProblem) as info:
        build_lock(_manifest(source), allow_file=True)
    assert info.value.id == "SL005"


def test_frontmatter_name_mismatch_is_sl006(tmp_path: Path) -> None:
    source = _make_remote(tmp_path, skill_md=b"---\nname: other\n---\n")
    with pytest.raises(ValidationProblem) as info:
        build_lock(_manifest(source), allow_file=True)
    assert info.value.id == "SL006"


def test_missing_skill_md_is_sl006(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    _git("config", "user.email", "test@example.com", cwd=work)
    _git("config", "user.name", "Test", cwd=work)
    skill_dir = work / SUBDIR
    skill_dir.mkdir(parents=True)
    (skill_dir / "notes.md").write_bytes(b"no skill here\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "init", cwd=work)
    _git("tag", "v1.2.0", cwd=work)
    bare = tmp_path / "remote.git"
    _git("init", "-q", "--bare", str(bare), cwd=tmp_path)
    _git(
        "-c",
        "protocol.file.allow=always",
        "push",
        "-q",
        str(bare),
        "refs/tags/v1.2.0:refs/tags/v1.2.0",
        cwd=work,
    )
    with pytest.raises(ValidationProblem) as info:
        build_lock(_manifest(f"file://{bare}"), allow_file=True)
    assert info.value.id == "SL006"
