# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/_manifest.py
# Purpose:   Facts about the root Cargo.toml, the package scope of each cargo
#            invocation, and restoring the manifests cargo-hack rewrites
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Read the root Cargo.toml, scope each cargo invocation, and restore manifests."""

import json
import shlex
import shutil
import signal
import threading
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

from hyperi_ci.common import announce, info, run_cmd, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.rust.targets import cargo_metadata

# Resolved by `git rev-parse --git-path`, so it lands in the right git dir in a worktree.
_BACKUP_GIT_PATH = "hyperi-ci/manifest-backup"
_BACKUP_INDEX = "index.json"
_KILLED_SUFFIX = ".killed"
_KILLED_RUN_TITLE = "hyperi-ci feature matrix was killed mid-run"


ISOLATE_MEMBERS_KEY = "build.rust.isolate_members"


@dataclass(frozen=True, slots=True)
class PackageScope:
    """The package switches for one cargo invocation of a workspace-wide pass.

    Attributes:
        args: ``--workspace``, ``--exclude`` and ``-p`` switches, or none.
        label: What the invocation covers, for tool names and log lines. Empty
            when the pass runs as one invocation.
        has_lib: Whether a package in scope has a lib target, or None when
            only the workspace-wide answer is known.
        packages: The packages the invocation covers, known only for a split
            pass. A report over several invocations names them with ``-p``.

    """

    args: tuple[str, ...] = ()
    label: str = ""
    has_lib: bool | None = None
    packages: tuple[str, ...] = ()


def _read_manifest(project_dir: Path | None) -> dict:
    """Return the parsed root Cargo.toml, empty when missing or unparseable."""
    manifest = (project_dir or Path.cwd()) / "Cargo.toml"
    try:
        return tomllib.loads(manifest.read_text(encoding="utf-8", errors="replace"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def is_root_package_workspace(project_dir: Path | None = None) -> bool:
    """Return True when a bare cargo command covers only the root package.

    That is a root manifest with both ``[package]`` and ``[workspace]``. A
    virtual workspace covers every member by default, so it answers False. So
    does a workspace that sets ``default-members``: that is the repo's own
    choice of what a bare command runs, and ``--workspace`` would override it.
    A missing or unparseable manifest answers False and leaves cargo to report
    it.

    Args:
        project_dir: Directory holding the root Cargo.toml; the cwd if None.

    Returns:
        Whether cargo needs ``--workspace`` to reach every member.

    """
    data = _read_manifest(project_dir)
    workspace = data.get("workspace")
    return (
        isinstance(data.get("package"), dict)
        and isinstance(workspace, dict)
        and "default-members" not in workspace
    )


def package_has_lib(package: dict) -> bool:
    """Return True when a ``cargo metadata`` package entry has a lib target."""
    return any(
        "lib" in target.get("kind", []) or "rlib" in target.get("kind", [])
        for target in package.get("targets", [])
    )


def _isolate_names(config: CIConfig) -> list[str]:
    """Return the member names ``build.rust.isolate_members`` lists, deduplicated.

    An unusable value is warned about and reads as empty.
    """
    value = config.get(ISOLATE_MEMBERS_KEY, [])
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        warn(
            f"  {ISOLATE_MEMBERS_KEY}: {value!r} is not a list of package names, "
            "ignored"
        )
        return []
    return list(dict.fromkeys(value))


def package_scopes(
    config: CIConfig, *, workspace: bool, project_dir: Path | None = None
) -> list[PackageScope]:
    """Split a workspace-wide pass so each isolated member runs on its own.

    ``build.rust.isolate_members`` names members too large to compile beside
    the rest. Each one gets its own ``-p`` invocation, after one invocation of
    ``--workspace`` with an ``--exclude`` per isolated member, so every member
    is still covered. It applies where a pass covers every member: a
    root-package workspace, which takes ``--workspace``, or a virtual one. A
    workspace that sets ``default-members`` keeps its own scope.

    Args:
        config: Merged CI configuration.
        workspace: Whether the pass takes ``--workspace`` today.
        project_dir: Directory holding the root Cargo.toml; the cwd if None.

    Returns:
        One scope per invocation, the workspace-wide one first. With nothing
        to isolate, a single scope carrying today's switches.

    """
    unsplit = [PackageScope(args=("--workspace",) if workspace else ())]
    names = _isolate_names(config)
    if not names:
        return unsplit

    table = _read_manifest(project_dir).get("workspace")
    if not isinstance(table, dict):
        warn(f"  {ISOLATE_MEMBERS_KEY}: not a cargo workspace, ignored")
        return unsplit
    if "default-members" in table:
        warn(
            f"  {ISOLATE_MEMBERS_KEY}: ignored, because default-members already "
            "decides what each pass covers"
        )
        return unsplit

    metadata = cargo_metadata(project_dir)
    if metadata is None:
        warn(
            f"  {ISOLATE_MEMBERS_KEY}: cargo metadata failed, so the members "
            "cannot be confirmed and every member runs together"
        )
        return unsplit
    lib_by_member = {
        package["name"]: package_has_lib(package)
        for package in metadata.get("packages", [])
        if package.get("name")
    }

    unknown = [name for name in names if name not in lib_by_member]
    if unknown:
        announce(
            f"{ISOLATE_MEMBERS_KEY} names {', '.join(unknown)}, which "
            f"{'is' if len(unknown) == 1 else 'are'} not a member of this "
            f"workspace, ignored. Members: {', '.join(sorted(lib_by_member))}",
            "hyperi-ci isolate_members names no such member",
        )
    isolated = [name for name in names if name in lib_by_member]
    if not isolated:
        return unsplit

    scopes: list[PackageScope] = []
    rest = [name for name in lib_by_member if name not in isolated]
    if rest:
        excludes = [arg for name in isolated for arg in ("--exclude", name)]
        scopes.append(
            PackageScope(
                args=("--workspace", *excludes),
                label=" ".join(excludes),
                has_lib=any(lib_by_member[name] for name in rest),
                packages=tuple(rest),
            )
        )
    scopes.extend(
        PackageScope(
            args=("-p", name),
            label=f"-p {name}",
            has_lib=lib_by_member[name],
            packages=(name,),
        )
        for name in isolated
    )
    info(
        f"  {ISOLATE_MEMBERS_KEY}: {', '.join(isolated)} "
        f"{'runs' if len(isolated) == 1 else 'run'} in a cargo invocation of "
        "its own"
    )
    return scopes


def _rewritable_files(project_dir: Path) -> tuple[list[Path], Path]:
    """Return the manifests cargo-hack may rewrite, and the lockfile path.

    Without cargo metadata only the project's own Cargo.toml and Cargo.lock
    are known.
    """
    manifests = [project_dir / "Cargo.toml"]
    workspace_root = project_dir
    metadata = cargo_metadata(project_dir)
    if metadata is not None:
        workspace_root = Path(metadata.get("workspace_root") or project_dir)
        manifests.append(workspace_root / "Cargo.toml")
        for package in metadata.get("packages", []):
            if manifest_path := package.get("manifest_path"):
                manifests.append(Path(manifest_path))
    unique = list(dict.fromkeys(path.resolve() for path in manifests))
    lockfile = (workspace_root / "Cargo.lock").resolve()
    return [path for path in unique if path.is_file()], lockfile


def _restore(path: Path, content: bytes | None) -> bool:
    """Put ``path`` back to ``content``, deleting it when it was absent.

    Returns:
        True when the file had changed and was put back.

    Raises:
        OSError: The file could not be read, written or deleted.

    """
    if content is None:
        if not path.exists():
            return False
        path.unlink()
        return True
    if path.is_file() and path.read_bytes() == content:
        return False
    path.write_bytes(content)
    return True


def _backup_dir(project_dir: Path) -> Path | None:
    """Return the per-repo backup directory inside the git dir, or None.

    None outside a git repository, or where git is not installed.
    """
    try:
        result = run_cmd(
            ["git", "rev-parse", "--git-path", _BACKUP_GIT_PATH],
            check=False,
            capture=True,
            cwd=project_dir,
        )
    except OSError:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return project_dir / result.stdout.strip()


def _write_backup(backup: Path, snapshot: dict[Path, bytes | None]) -> None:
    """Persist ``snapshot`` under ``backup``, the index last.

    Raises:
        OSError: The backup could not be written.

    """
    shutil.rmtree(backup, ignore_errors=True)
    backup.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, str | None]] = []
    for number, (path, content) in enumerate(snapshot.items()):
        name = None
        if content is not None:
            name = f"{number}-{path.name}"
            (backup / name).write_bytes(content)
        entries.append({"path": str(path), "backup": name})
    (backup / _BACKUP_INDEX).write_text(
        json.dumps({"files": entries}, indent=2), encoding="utf-8", newline="\n"
    )


def _read_backup(backup: Path) -> list[dict[str, str | None]]:
    """Return the backup's entries, or none when its index is missing or bad."""
    try:
        data = json.loads((backup / _BACKUP_INDEX).read_text(encoding="utf-8"))
        files = data["files"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    if not isinstance(files, list):
        return []
    return [entry for entry in files if isinstance(entry, dict) and entry.get("path")]


def _matches(backup: Path, entry: dict[str, str | None]) -> bool:
    """Return True when the file is as the backup recorded it."""
    path = Path(str(entry["path"]))
    name = entry.get("backup")
    if name is None:
        return not path.exists()
    try:
        return path.read_bytes() == (backup / name).read_bytes()
    except OSError:
        return False


def _report_killed_run(backup: Path) -> bool:
    """Warn about each file a killed matrix run may have left rewritten.

    The file itself is left alone, because it may have been edited since. The
    backup moves aside so the next snapshot cannot overwrite the copy the
    warning tells the developer to restore from.

    Returns:
        False when the old backup is still at ``backup`` and must not be
        overwritten.

    """
    if not backup.exists():
        return True
    # An index missing means the run died writing the backup, before cargo-hack ran.
    differing = [entry for entry in _read_backup(backup) if not _matches(backup, entry)]
    if not differing:
        shutil.rmtree(backup, ignore_errors=True)
        return True
    kept = backup.with_name(backup.name + _KILLED_SUFFIX)
    shutil.rmtree(kept, ignore_errors=True)
    try:
        backup.rename(kept)
    except OSError as exc:
        warn(f"  feature_matrix: could not move {backup} aside: {exc}")
        kept = backup
    for entry in differing:
        path = shlex.quote(str(entry["path"]))
        name = entry.get("backup")
        if name is None:
            message = (
                f"feature_matrix: {entry['path']} did not exist before a "
                "feature-matrix run that was killed, which likely left it pruned. "
                f"If nothing has created it since, remove it: rm {path}"
            )
        else:
            source = shlex.quote(str(kept / name))
            message = (
                f"feature_matrix: {entry['path']} differs from the copy taken "
                "before a feature-matrix run that was killed, which likely "
                "stripped its dev-dependencies. If you have not edited it "
                f"since, restore it: cp {source} {path}"
            )
        announce(message, _KILLED_RUN_TITLE)
    return kept != backup


@contextmanager
def _sigterm_raises() -> Iterator[None]:
    """Turn a SIGTERM into ``SystemExit(143)`` so ``finally`` blocks run.

    Python's default SIGTERM action ends the process without unwinding. Only
    the main thread can set a handler, so elsewhere this does nothing.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def _exit(signum: int, _frame: FrameType | None) -> None:
        # A second SIGTERM must not interrupt the restore the first one started.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise SystemExit(128 + signum)

    previous = signal.signal(signal.SIGTERM, _exit)
    try:
        yield
    finally:
        # None means a handler set outside Python, which cannot be put back.
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


@contextmanager
def restore_cargo_manifests(project_dir: Path | None = None) -> Iterator[None]:
    """Restore every Cargo.toml and the Cargo.lock to their bytes on entry.

    ``cargo hack --no-dev-deps`` strips dev-dependencies from each member's
    Cargo.toml and prunes Cargo.lock while it runs, and puts them back only
    if it exits on its own. ``subprocess.run`` answers Ctrl-C by killing the
    child 0.25s later, which leaves the rewrite in place, and the next clippy
    pass over tests fails on a dev-dependency it cannot resolve. The restore
    runs however the block exits, a SIGTERM included. A Cargo.lock absent on
    entry is deleted.

    A SIGKILL skips the restore, so in a git repository the snapshot is also
    kept under the git dir while the block runs. Finding it on entry means a
    run was killed, and each file that differs from it is named with the
    command that restores it. Nothing is restored automatically.

    Args:
        project_dir: Directory cargo runs in; the cwd if None.

    """
    root = project_dir or Path.cwd()
    manifests, lockfile = _rewritable_files(root)
    backup = _backup_dir(root) if manifests else None
    if backup is not None and not _report_killed_run(backup):
        backup = None
    snapshot: dict[Path, bytes | None] = {path: path.read_bytes() for path in manifests}
    snapshot[lockfile] = lockfile.read_bytes() if lockfile.is_file() else None
    if backup is not None:
        try:
            _write_backup(backup, snapshot)
        except OSError as exc:
            warn(f"  feature_matrix: could not back up the manifests: {exc}")
            backup = None

    with _sigterm_raises():
        try:
            yield
        finally:
            _restore_snapshot(snapshot, root, backup)


def _restore_snapshot(
    snapshot: dict[Path, bytes | None], root: Path, backup: Path | None
) -> None:
    """Restore ``snapshot`` and drop ``backup`` once every file is back."""
    restored: list[Path] = []
    failed = False
    for path, content in snapshot.items():
        try:
            if _restore(path, content):
                restored.append(path)
        except OSError as exc:
            warn(f"  feature_matrix: could not restore {path}: {exc}")
            failed = True
    if restored:
        names = ", ".join(_display(path, root) for path in restored)
        info(f"  feature_matrix: restored what cargo-hack left rewritten: {names}")
    if backup is not None and not failed:
        shutil.rmtree(backup, ignore_errors=True)


def _display(path: Path, root: Path) -> str:
    """Return ``path`` relative to ``root`` where it lies under it."""
    try:
        return str(path.relative_to(root.resolve()))
    except ValueError:
        return str(path)
