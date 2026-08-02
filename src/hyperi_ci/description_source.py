# Project:   HyperI CI
# File:      src/hyperi_ci/description_source.py
# Purpose:   One description, resolved once, for every registry that wants one
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Resolve the one-line project description that every destination duplicates.

The same sentence is asked for by PyPI, crates.io, npm, the OCI image label,
GHCR's package page and the GitHub repo blurb. Kept by hand it drifts, and
ours did worse than drift: ``org.opencontainers.image.description`` shipped
empty in every image because the only caller never passed one.

The manifest is the source, not a new config key. ``cargo publish`` refuses
without ``[package] description``, and PyPI and npm render theirs from the
manifest, so those files must carry it and be correct regardless. A separate
config key would be a fourth place able to disagree with three authoritative
ones.

Resolution order:

1. ``.hyperi-ci.yaml`` ``description`` -- the cascade opt-out, for a project
   with no manifest field (Go) or a deliberate divergence.
2. The manifest's top-level description, chosen by detected language.
3. The GitHub repo description, which is what ``docker/metadata-action``
   uses by default.
4. Nothing -- reported, never silently blank.

A Cargo workspace has no repo-level description of its own: ``[package]``
lives in each member and they legitimately differ (a core library and a CLI
describe different things). ``[workspace.package] description`` is the
repo-level answer -- cargo accepts it whether or not any member inherits it
with ``description.workspace = true``, so members keep their specific text.
"""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Callable
from pathlib import Path

from hyperi_ci.common import run_cmd
from hyperi_ci.config import CIConfig
from hyperi_ci.detect import detect_language


def _load_toml(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _clean(value: object) -> str | None:
    """Collapse a manifest value to a single-line description, or reject it."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text or None


def _python_description(path: Path) -> str | None:
    """PEP 621 ``[project] description``, else Poetry's."""
    data = _load_toml(path)
    project = data.get("project")
    if isinstance(project, dict):
        found = _clean(project.get("description"))
        if found:
            return found
    poetry = data.get("tool", {}).get("poetry")
    if isinstance(poetry, dict):
        return _clean(poetry.get("description"))
    return None


def _rust_description(path: Path) -> str | None:
    """``[package] description``, falling back to the workspace-level one.

    A virtual manifest has no ``[package]`` at all, and a member that sets
    ``description.workspace = true`` parses as a table rather than a string.
    Both resolve to ``[workspace.package]``.
    """
    data = _load_toml(path)
    workspace = _clean(data.get("workspace", {}).get("package", {}).get("description"))
    package = data.get("package")
    if isinstance(package, dict):
        found = _clean(package.get("description"))
        if found:
            return found
    return workspace


def _node_description(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _clean(data.get("description")) if isinstance(data, dict) else None


# Routed by detected language rather than a fixed file order: the manifest
# that owns the published artefact is the one that describes it. A Rust
# binary with a Python packaging wrapper takes the Cargo description.
# Go is absent on purpose: go.mod carries no description, so there is nothing
# to read and pkg.go.dev renders the package doc comment instead.
_MANIFESTS: dict[str, tuple[str, Callable[[Path], str | None]]] = {
    "python": ("pyproject.toml", _python_description),
    "rust": ("Cargo.toml", _rust_description),
    "typescript": ("package.json", _node_description),
    "javascript": ("package.json", _node_description),
}


def manifest_description(root: Path | None = None) -> tuple[str, str] | None:
    """Read the repo-level description from the project's own manifest.

    Args:
        root: Project root. Defaults to cwd.

    Returns:
        ``(description, manifest_filename)``, or None when the language has no
        such field (Go) or the field is absent.

    """
    base = root or Path.cwd()
    language = detect_language(base)
    entry = _MANIFESTS.get(language or "")
    if entry is None:
        return None
    filename, reader = entry
    path = base / filename
    if not path.is_file():
        return None
    found = reader(path)
    return (found, filename) if found else None


def repo_slug(cwd: Path | None = None) -> str | None:
    """Resolve ``owner/name``, from CI's env or the git remote.

    ``GITHUB_REPOSITORY`` only exists inside Actions, so a local
    ``describe --check`` has to read the remote or it silently checks nothing.
    """
    from_env = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if from_env:
        return from_env

    result = run_cmd(
        ["git", "remote", "get-url", "origin"],
        capture=True,
        check=False,
        cwd=str(cwd) if cwd else None,
    )
    if result.returncode != 0:
        return None
    url = result.stdout.strip().removesuffix(".git")
    for marker in ("github.com:", "github.com/"):
        _, sep, tail = url.partition(marker)
        if sep and tail.count("/") == 1:
            return tail
    return None


def github_description(
    repo: str | None = None, *, cwd: Path | None = None
) -> str | None:
    """Read the description GitHub shows for the repo.

    What ``docker/metadata-action`` falls back to, so a repo already carrying
    a good blurb needs no manifest change to get a populated image label.
    """
    target = repo or repo_slug(cwd)
    if not target:
        return None
    result = run_cmd(
        ["gh", "repo", "view", target, "--json", "description", "-q", ".description"],
        capture=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return _clean(result.stdout.strip())


def resolve_description(
    config: CIConfig | None = None,
    *,
    root: Path | None = None,
    repo: str | None = None,
    allow_github: bool = True,
) -> tuple[str, str] | None:
    """Resolve the project description and say where it came from.

    Args:
        config: Merged CI configuration, for the ``description`` opt-out.
        root: Project root. Defaults to cwd.
        repo: ``owner/name`` for the GitHub fallback.
        allow_github: Set False to skip the network lookup.

    Returns:
        ``(description, source)`` where source is ``.hyperi-ci.yaml``, a
        manifest filename, or ``GitHub``. None when nothing declares one.

    """
    if config is not None:
        declared = _clean(config.get("description", ""))
        if declared:
            return declared, ".hyperi-ci.yaml"

    from_manifest = manifest_description(root)
    if from_manifest:
        return from_manifest

    if allow_github:
        from_github = github_description(repo, cwd=root)
        if from_github:
            return from_github, "GitHub"

    return None
