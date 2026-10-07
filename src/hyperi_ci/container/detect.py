# Project:   HyperI CI
# File:      src/hyperi_ci/container/detect.py
# Purpose:   Detect whether a project has a containerisable artefact
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Container artefact detection.

A project ships a container exactly when it has a Dockerfile at the
configured path. There is no generated image: the repo's own Dockerfile
is the only build source.

Without one, a library skips quietly. A runnable project with no
Dockerfile (a Rust ``[[bin]]``, a TypeScript server, a Go ``main``) is
flagged with ``notice`` so the stage says out loud that no image is
built. A Python package always reads as a library (a console script is
not a service -- issue #51).

The detector returns a ``Decision`` so callers can both gate the build
and present a clear reason to the developer.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from hyperi_ci.languages.rust.targets import rust_is_library


@dataclass(frozen=True)
class Decision:
    """Outcome of containerisable-artefact detection.

    Attributes:
        build: Whether the container stage should run.
        reason: Human-readable explanation. Always present, used for log
            output regardless of outcome.
        notice: True when a runnable project has no Dockerfile, so the
            skip is worth a warning rather than an info line.

    """

    build: bool
    reason: str
    notice: bool = False


def detect(
    *, language: str, project_dir: Path, dockerfile: str = "Dockerfile"
) -> Decision:
    """Decide whether to build a container for this project.

    Args:
        language: Detected project language.
        project_dir: Project root directory.
        dockerfile: Path to the Dockerfile relative to ``project_dir``.

    Returns:
        ``Decision`` describing the outcome.

    """
    # A Dockerfile wins over the library heuristic: a monorepo root whose
    # runnable start script lives in a workspace still ships its image.
    if (project_dir / dockerfile).exists():
        return Decision(build=True, reason=f"Dockerfile found at {dockerfile}")

    if _is_library(language=language, project_dir=project_dir):
        return Decision(build=False, reason=f"{language} project is library-only")

    return Decision(
        build=False,
        reason=(
            f"no {dockerfile} in this {language} project, and hyperi-ci builds "
            "images only from a repo Dockerfile -- add one to ship a container"
        ),
        notice=True,
    )


def _is_library(*, language: str, project_dir: Path) -> bool:
    """Return True if the project is library-only (no executable target)."""
    if language == "rust":
        return rust_is_library(project_dir)
    if language == "python":
        return _python_is_library(project_dir)
    if language == "typescript":
        return _typescript_is_library(project_dir)
    if language == "golang":
        return _golang_is_library(project_dir)
    return False


def _python_is_library(project_dir: Path) -> bool:
    """Python packages are library-only by default (issue #51).

    A console-script (``[project.scripts]`` / ``[gui-scripts]`` /
    ``console_scripts`` entry point) is NOT a "ship a container" signal.
    The most common Python shape is a library that ALSO exposes a CLI -
    ruff, black, pytest, uv, httpie, pip-audit all declare
    ``[project.scripts]`` and none of them are container workloads.
    There is no reliable pyproject signal for "this is a deployable
    service", so a Python service ships its own Dockerfile.
    """
    return (project_dir / "pyproject.toml").exists()


def _typescript_is_library(project_dir: Path) -> bool:
    package_json = project_dir / "package.json"
    if not package_json.exists():
        return False
    try:
        manifest = json.loads(package_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    if manifest.get("bin"):
        return False
    # A ``workspaces`` field means this is a monorepo root, not a plain
    # library: the runnable ``start``/``serve``/``server`` script lives in
    # a workspace package (e.g. ``apps/<app>/package.json``), so the root
    # legitimately has none. Treat the root as a runnable, not a library.
    if manifest.get("workspaces"):
        return False
    scripts = manifest.get("scripts", {})
    for key in ("start", "serve", "server"):
        if scripts.get(key):
            return False
    main = manifest.get("main", "")
    if main and any(part in main for part in ("server", "main", "index")):
        # Heuristic: a ``main`` field that names a server-ish entrypoint
        # is enough to consider this a runnable. Library packages
        # typically point ``main`` at ``dist/index.js`` for consumers,
        # which also matches -- favour build-on-doubt.
        return False
    return True


def _golang_is_library(project_dir: Path) -> bool:
    """Identify Go library projects (``go.mod`` present, no ``package main``).

    Go projects almost always have a ``package main`` entry; treat a
    project with a ``go.mod`` and no ``main`` package as a library.
    """
    if not (project_dir / "go.mod").exists():
        return False
    for path in project_dir.rglob("*.go"):
        if "vendor" in path.parts or "testdata" in path.parts:
            continue
        try:
            head = path.read_text(encoding="utf-8", errors="replace").splitlines()[:5]
        except OSError:
            continue
        for line in head:
            stripped = line.strip()
            if stripped.startswith("package main"):
                return False
    return True
