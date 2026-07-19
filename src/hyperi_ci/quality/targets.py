# Project:   HyperI CI
# File:      src/hyperi_ci/quality/targets.py
# Purpose:   Discover lint targets (Dockerfiles, k8s manifests) on disk
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Discover the files a linting tool should scan.

gitleaks and semgrep scan the whole tree; the container / k8s linters need an
explicit target list (hadolint takes files, kubeconform takes rendered
manifests). Keeping discovery here means one place decides what counts as a
Dockerfile / manifest and one place prunes the dirs nobody should lint (`.git`,
`.worktrees` duplicate checkouts, vendored deps).

Auto-detect + clean skip: a repo with no Dockerfile just yields ``[]`` and the
tool info-skips - no opt-out config needed for a repo that has no target.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

import yaml

# Always pruned, regardless of config: VCS internals, worktree duplicate trees
# (dfe-infra keeps two full checkouts under .worktrees/ - scanning them doubles
# every finding), scratch, and the usual vendored-dependency sinks.
_ALWAYS_PRUNE = {
    ".git",
    ".worktrees",
    ".tmp",
    ".hyperi-ai",
    "node_modules",
    "target",
    "vendor",
    ".venv",
    "venv",
    ".mypy_cache",
    ".ruff_cache",
    "__pycache__",
}


def _is_dockerfile(name: str) -> bool:
    """Match Dockerfile / Containerfile and their .suffix / prefix. forms.

    Matches ``Dockerfile``, ``Dockerfile.<x>``, ``<x>.Dockerfile`` and the
    ``Containerfile`` equivalents. Deliberately does NOT match
    ``.dockerignore`` (it starts with a dot, not ``Dockerfile.``).
    """
    for base in ("Dockerfile", "Containerfile"):
        if name == base or name.startswith(f"{base}.") or name.endswith(f".{base}"):
            return True
    return False


def discover_dockerfiles(
    root: Path | str, *, exclude_dirs: Iterable[str] = ()
) -> list[Path]:
    """Return every Dockerfile/Containerfile under ``root``, pruned + sorted.

    ``exclude_dirs`` (typically ``get_exclude_dirs(config)``) is added to the
    always-pruned set. Paths are returned sorted for deterministic output.
    """
    root = Path(root)
    prune = _ALWAYS_PRUNE | {str(d).strip("/") for d in exclude_dirs if d}
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune in place so os.walk does not descend into excluded dirs.
        dirnames[:] = [d for d in dirnames if d not in prune]
        for fn in filenames:
            if _is_dockerfile(fn):
                found.append(Path(dirpath) / fn)
    return sorted(found)


def _prune(exclude_dirs: Iterable[str]) -> set[str]:
    return _ALWAYS_PRUNE | {str(d).strip("/") for d in exclude_dirs if d}


def discover_helm_charts(
    root: Path | str, *, exclude_dirs: Iterable[str] = ()
) -> list[Path]:
    """Return top-level Helm chart directories (dirs holding a ``Chart.yaml``).

    Skips:

    * ``type: library`` charts - they render nothing on their own, so linting
      or schema-validating them is pointless (dfe-infra's ``dfe-common``).
    * subcharts - a ``Chart.yaml`` nested under another chart's ``charts/`` dir
      is a vendored dependency, rendered by its parent, not a target itself.

    Pruned dirs (``.worktrees`` etc) never descend, so a duplicate worktree
    checkout does not double every chart.
    """
    root = Path(root)
    prune = _prune(exclude_dirs)
    charts: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in prune]
        if "Chart.yaml" not in filenames:
            continue
        chart_dir = Path(dirpath)
        # A chart inside another chart's charts/ dir is a subchart - skip it.
        if (
            chart_dir.parent.name == "charts"
            and (chart_dir.parent.parent / "Chart.yaml").exists()
        ):
            continue
        if _is_library_chart(chart_dir / "Chart.yaml"):
            continue
        charts.append(chart_dir)
    return sorted(charts)


def _is_library_chart(chart_yaml: Path) -> bool:
    """True when Chart.yaml declares ``type: library`` (renders nothing)."""
    try:
        data = yaml.safe_load(chart_yaml.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return False
    return isinstance(data, dict) and str(data.get("type", "")).lower() == "library"


def _looks_like_manifest(path: Path) -> bool:
    """True when any YAML doc in ``path`` has both ``apiVersion`` and ``kind``.

    This is what separates a real k8s manifest (Deployment, an Argo CR, ...)
    from a Helm ``values.yaml``, a ``Chart.yaml`` (has apiVersion but no kind),
    or arbitrary config YAML - so kubeconform is fed manifests, not values
    files it would reject as "missing kind". Helm TEMPLATE files (``{{ }}``)
    are not valid YAML and fail the parse, so they are excluded here and get
    rendered instead.
    """
    try:
        docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError):
        return False
    return any(isinstance(d, dict) and "apiVersion" in d and "kind" in d for d in docs)


def _inside_chart(dirpath: Path, root: Path) -> bool:
    """True when ``dirpath`` is at or under a Helm chart (an ancestor Chart.yaml).

    Chart content (``templates/`` Go-templates, ``values.yaml``, ``Chart.yaml``)
    is handled by :func:`discover_helm_charts` + ``helm template``, so it must
    not also be picked up as a plain manifest - a chart template can happen to
    parse as YAML, so the ``apiVersion``+``kind`` heuristic alone is not enough.
    """
    d = dirpath
    while True:
        if (d / "Chart.yaml").exists():
            return True
        if d == root or d.parent == d:
            return False
        d = d.parent


def discover_manifests(
    root: Path | str, *, exclude_dirs: Iterable[str] = ()
) -> list[Path]:
    """Return plain (already-rendered) k8s manifest YAML files under ``root``.

    A file counts only if it holds at least one ``apiVersion``+``kind`` doc
    (:func:`_looks_like_manifest`) AND is not inside a Helm chart
    (:func:`_inside_chart`) - chart content is rendered separately. This yields
    the loose manifests (Argo CRs, plain Deployments) that need direct schema
    validation. Pruned dirs never descend.
    """
    root = Path(root)
    prune = _prune(exclude_dirs)
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in prune]
        here = Path(dirpath)
        if _inside_chart(here, root):
            continue
        for fn in filenames:
            if not fn.endswith((".yaml", ".yml")):
                continue
            p = here / fn
            if _looks_like_manifest(p):
                out.append(p)
    return sorted(out)
