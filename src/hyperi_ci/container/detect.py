# Project:   HyperI CI
# File:      src/hyperi_ci/container/detect.py
# Purpose:   Detect whether a project has a containerisable artefact
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Container artefact detection.

A project ships a container if:

  * it is **not** a library, AND
  * it has a build signal — either a Dockerfile at the configured path,
    or (Rust only) the project's binary supports the scalo contract
    `generate-artefacts` subcommand.

Libraries skip silently — there is nothing to ship. A Rust crate with
no ``[[bin]]`` is a library; a Python package is treated as library-only
regardless of any ``[project.scripts]`` CLI (a console-script is not a
service — see issue #51); a TypeScript package with no ``bin``/``main`` /
server entry is a library. A genuine Python/TS service opts in with a
Dockerfile or ``publish.container.enabled: true``.

The detector returns a ``Decision`` so callers can both gate the build
and present a clear reason to the developer.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover — Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]  # ty: ignore[unresolved-import]


@dataclass(frozen=True)
class Decision:
    """Outcome of containerisable-artefact detection.

    Attributes:
        build: Whether the container stage should run.
        reason: Human-readable explanation. Always present, used for log
            output regardless of outcome.
        mode: Suggested build mode (``contract`` | ``template`` | ``custom``)
            when ``build`` is True. Empty string when ``build`` is False.

    """

    build: bool
    reason: str
    mode: str = ""


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
    # An explicit Dockerfile is an unambiguous "ship a container" signal
    # and must win over the library heuristic — otherwise a monorepo root
    # with a Dockerfile (whose runnable start script lives in a workspace,
    # not the root package.json) is wrongly declared library-only and the
    # Dockerfile is never used.
    dockerfile_path = project_dir / dockerfile
    if dockerfile_path.exists():
        return Decision(
            build=True, reason=f"Dockerfile found at {dockerfile}", mode="custom"
        )

    if _is_library(language=language, project_dir=project_dir):
        return Decision(build=False, reason=f"{language} project is library-only")

    if language == "rust" and _rust_supports_contract(project_dir):
        return Decision(
            build=True,
            reason="Rust binary supports scalo generate-artefacts contract",
            mode="contract",
        )

    if language in {"python", "typescript"}:
        return Decision(
            build=True,
            reason=f"{language} template applies (no Dockerfile required)",
            mode="template",
        )

    return Decision(
        build=False,
        reason=(
            f"no container artefact detected for {language} "
            f"(no {dockerfile}, no contract source)"
        ),
    )


def _is_library(*, language: str, project_dir: Path) -> bool:
    """Return True if the project is library-only (no executable target)."""
    if language == "rust":
        return _rust_is_library(project_dir)
    if language == "python":
        return _python_is_library(project_dir)
    if language == "typescript":
        return _typescript_is_library(project_dir)
    if language == "golang":
        return _golang_is_library(project_dir)
    return False


def _rust_is_library(project_dir: Path) -> bool:
    """Rust: library if no ``[[bin]]`` target nor ``src/main.rs`` / ``src/bin/*.rs``.

    Uses ``cargo metadata`` for the authoritative answer when cargo is
    available; falls back to filesystem heuristics otherwise (CI images
    that don't have cargo on PATH at the container-stage shouldn't hit
    this path, but the fallback keeps tests deterministic).
    """
    cargo_toml = project_dir / "Cargo.toml"
    if not cargo_toml.exists():
        return False

    metadata = _cargo_metadata(project_dir)
    if metadata is not None:
        for package in metadata.get("packages", []):
            for target in package.get("targets", []):
                if "bin" in target.get("kind", []):
                    return False
        return True

    if (project_dir / "src" / "main.rs").exists():
        return False
    bin_dir = project_dir / "src" / "bin"
    if bin_dir.is_dir() and any(p.suffix == ".rs" for p in bin_dir.iterdir()):
        return False
    try:
        manifest = tomllib.loads(cargo_toml.read_text())
    except Exception:
        return False
    if manifest.get("bin"):
        return False
    return True


def _cargo_metadata(project_dir: Path) -> dict | None:
    result = subprocess.run(
        ["cargo", "metadata", "--no-deps", "--format-version=1"],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _rust_supports_contract(project_dir: Path) -> bool:
    """Return True if the Rust project depends on scalo (contract source).

    The contract source is scalo's ``DfeApp::generate_artefacts`` code
    path, which is exposed by every binary that uses scalo's CLI
    harness. Presence of ``scalo`` (or the deprecated predecessor
    ``hyperi-rustlib``) in the project's manifest is a sufficient
    signal — projects opting out of contract mode can still set
    ``publish.container.mode: custom`` explicitly.
    """
    cargo_toml = project_dir / "Cargo.toml"
    if not cargo_toml.exists():
        return False
    try:
        manifest = tomllib.loads(cargo_toml.read_text())
    except Exception:
        return False
    deps = manifest.get("dependencies", {})
    return "scalo" in deps or "hyperi-rustlib" in deps


def _python_is_library(project_dir: Path) -> bool:
    """Python packages are library-only by default (issue #51).

    A console-script (``[project.scripts]`` / ``[gui-scripts]`` /
    ``console_scripts`` entry point) is NOT a "ship a container" signal.
    The most common Python shape is a library that ALSO exposes a CLI -
    ruff, black, pytest, uv, httpie, pip-audit all declare
    ``[project.scripts]`` and none of them are container workloads. The
    old heuristic treated any console-script as "build a container", so
    every hyperi-io Python library that shipped a CLI got a spurious,
    failing ``Release tail / Container`` job on every release.

    There is no reliable pyproject signal for "this is a deployable
    service", so Python defaults to library-only. A genuine Python
    service opts in explicitly with a Dockerfile (wins over this
    heuristic in :func:`detect`) or ``publish.container.enabled: true``
    (forces the template build in the stage handler). This mirrors the
    rust/golang behaviour - a container needs a bin/main target or a
    Dockerfile, not merely "has a CLI".
    """
    return (project_dir / "pyproject.toml").exists()


def _typescript_is_library(project_dir: Path) -> bool:
    package_json = project_dir / "package.json"
    if not package_json.exists():
        return False
    try:
        manifest = json.loads(package_json.read_text())
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
        # which also matches — favour build-on-doubt.
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
            head = path.read_text(errors="replace").splitlines()[:5]
        except OSError:
            continue
        for line in head:
            stripped = line.strip()
            if stripped.startswith("package main"):
                return False
    return True
