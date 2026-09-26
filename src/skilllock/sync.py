"""Installing pinned snapshots and verifying installed files offline.

Installs never execute repository content. Files are staged in a temporary
sibling directory and moved into place with an atomic rename; an existing
install is a no-op only when it is byte-identical, otherwise sync refuses to
overwrite it.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path

from .checks import SL003, SL004, SL005, SL006, OperationalError, ValidationProblem
from .gitops import Snapshot, fetch_snapshot
from .hashing import sha256_hex
from .lockfile import LockedFile, LockedSkill
from .manifest import Manifest, name_is_valid, read_frontmatter_name
from .paths import create_directory, validate_relative_path
from .report import Report

_MODE_BY_FILE = {"100755": 0o755, "100644": 0o644}


def _installed_records(skill_root: Path, report: Report) -> dict[str, tuple[str, str]]:
    records: dict[str, tuple[str, str]] = {}
    for dirpath, dirnames, filenames in os.walk(skill_root, followlinks=False):
        base = Path(dirpath)
        for dirname in list(dirnames):
            candidate = base / dirname
            if candidate.is_symlink():
                report.error(
                    SL005,
                    f"installed path is a symlinked directory: "
                    f"{candidate.relative_to(skill_root).as_posix()}",
                )
                dirnames.remove(dirname)
        for filename in filenames:
            candidate = base / filename
            relative = candidate.relative_to(skill_root).as_posix()
            if candidate.is_symlink():
                report.error(SL005, f"installed path is a symlink: {relative}")
                continue
            try:
                validate_relative_path(relative)
            except ValidationProblem as exc:
                report.error(exc.id, exc.message)
                continue
            try:
                info = candidate.lstat()
            except OSError as exc:
                raise OperationalError(f"cannot stat {candidate}: {exc}") from exc
            if not stat.S_ISREG(info.st_mode):
                report.error(SL005, f"installed path is not a regular file: {relative}")
                continue
            try:
                data = candidate.read_bytes()
            except OSError as exc:
                raise OperationalError(f"cannot read {candidate}: {exc}") from exc
            mode = "100755" if info.st_mode & 0o111 else "100644"
            records[relative] = (sha256_hex(data), mode)
    return records


def _compare_installed(
    installed: dict[str, tuple[str, str]],
    expected: dict[str, LockedFile],
    report: Report,
    label: str,
) -> None:
    for relative in sorted(set(expected) - set(installed)):
        report.error(SL004, f"{label}: missing file: {relative}")
    for relative in sorted(set(installed) - set(expected)):
        report.error(SL004, f"{label}: unexpected file: {relative}")
    for relative in sorted(set(expected) & set(installed)):
        digest, mode = installed[relative]
        reference = expected[relative]
        if digest != reference.sha256:
            report.error(SL004, f"{label}: content differs: {relative}")
        if mode != reference.mode:
            report.error(SL004, f"{label}: executable mode differs: {relative}")


def _check_skill_md(
    manifest_name: str, skill_root: Path, report: Report, label: str
) -> None:
    marker = skill_root / "SKILL.md"
    if marker.is_symlink() or not marker.is_file():
        report.error(SL006, f"{label}: no regular root-level SKILL.md")
        return
    try:
        data = marker.read_bytes()
    except OSError as exc:
        raise OperationalError(f"cannot read {marker}: {exc}") from exc
    name = read_frontmatter_name(data)
    if name is None or not name_is_valid(name):
        report.error(SL006, f"{label}: SKILL.md frontmatter name is missing or invalid")
    elif name != manifest_name:
        report.error(
            SL006,
            f"{label}: SKILL.md name {name!r} does not match manifest name "
            f"{manifest_name!r}",
        )


def verify_project(
    manifest: Manifest, skills: tuple[LockedSkill, ...], target_root: Path
) -> Report:
    """Offline verification of installed files against the lock."""
    report = Report()
    manifest_by_name = {entry.name: entry for entry in manifest.skills}
    for skill in skills:
        label = skill.name
        skill_root = target_root / skill.name
        if skill_root.is_symlink():
            report.error(SL005, f"{label}: install path is a symlink")
            continue
        if not skill_root.exists():
            report.error(SL004, f"{label}: skill is not installed")
            continue
        if not skill_root.is_dir():
            report.error(SL004, f"{label}: install path is not a directory")
            continue
        installed = _installed_records(skill_root, report)
        expected = {item.path: item for item in skill.files}
        _compare_installed(installed, expected, report, label)
        _check_skill_md(manifest_by_name[label].name, skill_root, report, label)
    return report


def _check_snapshot_against_lock(
    skill: LockedSkill, snapshot: Snapshot, report: Report
) -> None:
    locked_files = {item.path: item for item in skill.files}
    actual_files = {item.path: item for item in snapshot.files}
    for relative in sorted(set(locked_files) - set(actual_files)):
        report.error(
            SL003, f"{skill.name}: pinned commit is missing locked file: {relative}"
        )
    for relative in sorted(set(actual_files) - set(locked_files)):
        report.error(
            SL003, f"{skill.name}: pinned commit has an unlocked file: {relative}"
        )
    for relative in sorted(set(actual_files) & set(locked_files)):
        if actual_files[relative].sha256 != locked_files[relative].sha256:
            report.error(
                SL003, f"{skill.name}: hash mismatch at pinned commit: {relative}"
            )
        if actual_files[relative].mode != locked_files[relative].mode:
            report.error(
                SL003, f"{skill.name}: mode mismatch at pinned commit: {relative}"
            )


def _write_snapshot(staging: Path, snapshot: Snapshot) -> None:
    for record in snapshot.files:
        destination = staging.joinpath(*record.path.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(record.data)
        os.chmod(destination, _MODE_BY_FILE[record.mode])


def _install_skill(
    skill: LockedSkill, snapshot: Snapshot, target_root: Path, report: Report
) -> None:
    destination = target_root / skill.name
    if destination.is_symlink():
        report.error(SL005, f"{skill.name}: install path is a symlink")
        return
    if destination.exists():
        if not destination.is_dir():
            report.error(SL004, f"{skill.name}: install path is not a directory")
            return
        before = len(report.findings)
        installed = _installed_records(destination, report)
        expected = {
            item.path: LockedFile(path=item.path, sha256=item.sha256, mode=item.mode)
            for item in snapshot.files
        }
        _compare_installed(installed, expected, report, skill.name)
        if len(report.findings) != before:
            report.error(
                SL004, f"{skill.name}: existing install differs; refusing to overwrite"
            )
        return

    try:
        staging = Path(tempfile.mkdtemp(prefix=".skilllock-", dir=target_root))
    except OSError as exc:
        raise OperationalError(f"cannot create staging directory: {exc}") from exc
    try:
        _write_snapshot(staging, snapshot)
        os.rename(staging, destination)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise OperationalError(f"cannot install {skill.name}: {exc}") from exc


def sync_project(
    manifest: Manifest,
    skills: tuple[LockedSkill, ...],
    target: str,
    *,
    allow_file: bool = False,
) -> Report:
    """Fetch pinned commits, validate hashes, and install skills atomically."""
    report = Report()
    target_root = create_directory(target)
    snapshots: dict[str, Snapshot] = {}
    for skill in skills:
        try:
            snapshot = fetch_snapshot(
                skill.source, skill.commit, skill.subdir, allow_file=allow_file
            )
        except ValidationProblem as exc:
            report.error(exc.id, f"{skill.name}: {exc.message}")
            continue
        _check_snapshot_against_lock(skill, snapshot, report)
        snapshots[skill.name] = snapshot
    if report.findings:
        return report
    for skill in skills:
        _install_skill(skill, snapshots[skill.name], target_root, report)
    return report
