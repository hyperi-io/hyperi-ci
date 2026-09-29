# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/_manifest.py
# Purpose:   Facts about the root Cargo.toml, and restoring the manifests
#            cargo-hack rewrites
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Read the root Cargo.toml, and put back the manifests cargo-hack rewrites."""

import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from hyperi_ci.common import info, warn
from hyperi_ci.languages.rust.targets import cargo_metadata


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
    manifest = (project_dir or Path.cwd()) / "Cargo.toml"
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8", errors="replace"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    workspace = data.get("workspace")
    return (
        isinstance(data.get("package"), dict)
        and isinstance(workspace, dict)
        and "default-members" not in workspace
    )


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

    """
    try:
        if content is None:
            if not path.exists():
                return False
            path.unlink()
            return True
        if path.is_file() and path.read_bytes() == content:
            return False
        path.write_bytes(content)
        return True
    except OSError as exc:
        warn(f"  feature_matrix: could not restore {path}: {exc}")
        return False


@contextmanager
def restore_cargo_manifests(project_dir: Path | None = None) -> Iterator[None]:
    """Restore every Cargo.toml and the Cargo.lock to their bytes on entry.

    ``cargo hack --no-dev-deps`` strips dev-dependencies from each member's
    Cargo.toml and prunes Cargo.lock while it runs, and puts them back only
    if it exits on its own. ``subprocess.run`` answers Ctrl-C by killing the
    child 0.25s later, which leaves the rewrite in place, and the next clippy
    pass over tests fails on a dev-dependency it cannot resolve. The restore
    runs however the block exits. A Cargo.lock absent on entry is deleted.

    Args:
        project_dir: Directory cargo runs in; the cwd if None.

    """
    root = project_dir or Path.cwd()
    manifests, lockfile = _rewritable_files(root)
    snapshot: dict[Path, bytes | None] = {path: path.read_bytes() for path in manifests}
    snapshot[lockfile] = lockfile.read_bytes() if lockfile.is_file() else None
    try:
        yield
    finally:
        restored = [
            path for path, content in snapshot.items() if _restore(path, content)
        ]
        if restored:
            names = ", ".join(_display(path, root) for path in restored)
            info(f"  feature_matrix: restored what cargo-hack left rewritten: {names}")


def _display(path: Path, root: Path) -> str:
    """Return ``path`` relative to ``root`` where it lies under it."""
    try:
        return str(path.relative_to(root.resolve()))
    except ValueError:
        return str(path)
