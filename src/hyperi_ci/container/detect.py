# Project:   HyperI CI
# File:      src/hyperi_ci/container/detect.py
# Purpose:   Detect whether a project has a containerisable artefact
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Container artefact detection.

A project ships a container if:

  * it is **not** a library, AND
  * it has a build signal — either a Dockerfile at the configured path,
    or (Rust only) the project's binary supports the rustlib contract
    `generate-artefacts` subcommand.

Libraries (Rust crates with no ``[[bin]]``, Python packages with no
``[project.scripts]``, TypeScript packages with no ``bin``/``main`` /
server entry) skip silently — there is nothing to ship.

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
    import tomli as tomllib  # type: ignore[no-redef]


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
    if _is_library(language=language, project_dir=project_dir):
        return Decision(build=False, reason=f"{language} project is library-only")

    dockerfile_path = project_dir / dockerfile
    if dockerfile_path.exists():
        return Decision(
            build=True, reason=f"Dockerfile found at {dockerfile}", mode="custom"
        )

    if language == "rust" and _rust_supports_contract(project_dir):
        return Decision(
            build=True,
            reason="Rust binary supports rustlib generate-artefacts contract",
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
    """Return True if the Rust project depends on hyperi-rustlib (contract source).

    The contract source is the rustlib ``DfeApp::generate_artefacts``
    code path, which is exposed by every binary that uses rustlib's CLI
    harness. Presence of ``hyperi-rustlib`` in the project's manifest
    is a sufficient signal — projects opting out of contract mode can
    still set ``publish.container.mode: custom`` explicitly.
    """
    cargo_toml = project_dir / "Cargo.toml"
    if not cargo_toml.exists():
        return False
    try:
        manifest = tomllib.loads(cargo_toml.read_text())
    except Exception:
        return False
    deps = manifest.get("dependencies", {})
    return "hyperi-rustlib" in deps


def _python_is_library(project_dir: Path) -> bool:
    pyproject = project_dir / "pyproject.toml"
    if not pyproject.exists():
        return False
    try:
        manifest = tomllib.loads(pyproject.read_text())
    except Exception:
        return False
    project = manifest.get("project", {})
    if project.get("scripts"):
        return False
    if project.get("gui-scripts"):
        return False
    entry_points = project.get("entry-points", {})
    if entry_points.get("console_scripts"):
        return False
    return True


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
