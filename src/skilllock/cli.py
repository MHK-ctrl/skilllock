"""Command line interface for skilllock.

Exit codes:
    0  Success; ``verify`` reports no drift.
    1  A deterministic validation finding (a check ID) or drift was reported.
    2  Invalid usage, filesystem, Git/network, or other operational failure.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

import typer

from . import __version__
from .checks import (
    FORMAT_VERSION,
    LOCK_NAME,
    MANIFEST_NAME,
    OperationalError,
    ValidationProblem,
)
from .lockfile import build_lock, check_agreement, read_lock, write_lock
from .manifest import (
    Manifest,
    SkillEntry,
    add_skill,
    load_manifest,
    validate_entry,
    write_manifest,
)
from .paths import resolve_existing_directory
from .report import EXIT_ERROR, EXIT_OK, Report
from .sync import sync_project, verify_project

DEFAULT_TARGET = "./skills"

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Pin Git-hosted Agent Skills to immutable commits and reproduce them.",
)


def _fail(message: str) -> None:
    print(f"skilllock: error: {message}", file=sys.stderr)


def _emit(report: Report) -> int:
    for line in report.format_lines():
        print(line, file=sys.stderr)
    return report.exit_code


@app.callback(invoke_without_command=True)
def _root(
    version: bool = typer.Option(
        False, "--version", help="Show the version and exit.", is_eager=True
    ),
) -> None:
    if version:
        typer.echo(f"skilllock {__version__}")
        raise typer.Exit(code=EXIT_OK)


@app.command()
def init() -> int:
    """Create skills.toml. Refuses to overwrite an existing file."""
    manifest_path = Path(MANIFEST_NAME)
    if manifest_path.exists():
        _fail(f"{MANIFEST_NAME} already exists; refusing to overwrite")
        return EXIT_ERROR
    try:
        write_manifest(manifest_path, Manifest(format=FORMAT_VERSION, skills=()))
    except OperationalError as exc:
        _fail(str(exc))
        return EXIT_ERROR
    print(f"skilllock: created {MANIFEST_NAME}")
    return EXIT_OK


@app.command()
def add(
    url: str = typer.Argument(..., help="HTTPS Git source URL."),
    name: str = typer.Option(..., "--name", help="Skill name."),
    ref: str = typer.Option(
        ..., "--ref", help="Full refs/heads/... or refs/tags/... ref."
    ),
    subdir: str = typer.Option(
        ".", "--subdir", help="Repository-relative skill directory."
    ),
) -> int:
    """Validate and append one manifest entry. Performs no network access."""
    try:
        manifest = load_manifest(Path(MANIFEST_NAME))
        entry = SkillEntry(name=name, source=url, ref=ref, subdir=subdir)
        validate_entry(entry)
        updated = add_skill(manifest, entry)
        write_manifest(Path(MANIFEST_NAME), updated)
    except ValidationProblem as exc:
        return _emit(Report.from_problem(exc))
    except OperationalError as exc:
        _fail(str(exc))
        return EXIT_ERROR
    print(f"skilllock: added {name!r} to {MANIFEST_NAME}")
    return EXIT_OK


@app.command()
def lock() -> int:
    """Fetch declared refs, validate snapshots, and write skills.lock."""
    try:
        manifest = load_manifest(Path(MANIFEST_NAME))
        locked = build_lock(manifest)
        write_lock(Path(LOCK_NAME), locked)
    except ValidationProblem as exc:
        return _emit(Report.from_problem(exc))
    except OperationalError as exc:
        _fail(str(exc))
        return EXIT_ERROR
    print(f"skilllock: wrote {LOCK_NAME} with {len(locked)} skill(s)")
    return EXIT_OK


@app.command()
def sync(
    target: str = typer.Option(DEFAULT_TARGET, "--target", help="Install directory."),
) -> int:
    """Fetch pinned commits, verify hashes, and install skills."""
    try:
        manifest = load_manifest(Path(MANIFEST_NAME))
        locked = read_lock(Path(LOCK_NAME))
        check_agreement(manifest, locked)
        report = sync_project(manifest, locked, target)
    except ValidationProblem as exc:
        return _emit(Report.from_problem(exc))
    except OperationalError as exc:
        _fail(str(exc))
        return EXIT_ERROR
    code = _emit(report)
    if code == EXIT_OK:
        print(f"skilllock: synced {len(locked)} skill(s) into {target}")
    return code


@app.command()
def verify(
    target: str = typer.Option(DEFAULT_TARGET, "--target", help="Install directory."),
) -> int:
    """Offline check of installed files against the lock. Performs no network."""
    try:
        target_root = resolve_existing_directory(target)
        lock_path = Path(LOCK_NAME)
        if not lock_path.is_file():
            raise OperationalError(
                f"no {LOCK_NAME} found at {lock_path.resolve()} \u2014 "
                "run `skilllock lock` first"
            )
        manifest = load_manifest(Path(MANIFEST_NAME))
        locked = read_lock(lock_path)
        check_agreement(manifest, locked)
        report = verify_project(manifest, locked, target_root)
    except ValidationProblem as exc:
        return _emit(Report.from_problem(exc))
    except OperationalError as exc:
        _fail(str(exc))
        return EXIT_ERROR
    code = _emit(report)
    if code == EXIT_OK:
        print(f"skilllock: no drift in {len(locked)} skill(s)")
    return code


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns the documented exit code."""
    args = list(argv) if argv is not None else None
    try:
        result = app(args=args, standalone_mode=False, prog_name="skilllock")
    except typer.Abort:
        _fail("aborted")
        return EXIT_ERROR
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - translate vendor usage errors
        exit_code = getattr(exc, "exit_code", None)
        if exit_code is None:
            raise
        message = str(exc).strip()
        if message:
            _fail(message)
        return int(exit_code)
    return result if isinstance(result, int) else EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
