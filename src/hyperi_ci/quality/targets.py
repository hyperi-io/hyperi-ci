# Project:   HyperI CI
# File:      src/hyperi_ci/quality/targets.py
# Purpose:   Discover lint targets (Dockerfiles, k8s manifests, markdown) on disk
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Discover the files a linting tool should scan.

gitleaks and semgrep scan the whole tree; the container / k8s / docs linters
need an explicit target list (hadolint takes files, kubeconform takes rendered
manifests, lychee takes markdown). Keeping discovery here means one place
decides what counts as a Dockerfile / manifest / doc and one place prunes the
dirs nobody should lint (`.git`, `.worktrees` duplicate checkouts, vendored
deps).

Auto-detect + clean skip: a repo with no Dockerfile just yields ``[]`` and the
tool info-skips - no opt-out config needed for a repo that has no target.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from hyperi_ci.deps import surfaces

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
        dirnames[:] = [
            d for d in dirnames if not _is_pruned(Path(dirpath) / d, root, prune)
        ]
        for fn in filenames:
            if _is_dockerfile(fn):
                found.append(Path(dirpath) / fn)
    return sorted(found)


def _prune(exclude_dirs: Iterable[str]) -> set[str]:
    return _ALWAYS_PRUNE | {str(d).strip("/") for d in exclude_dirs if d}


def _is_pruned(candidate: Path, root: Path, prune: set[str]) -> bool:
    """Whether ``candidate`` is excluded, by bare NAME or by relative PATH.

    `quality.exclude_paths` takes paths, so a nested entry like
    `docs/superpowers` has to match the path rather than only the basename.
    Matching the name alone accepted the setting and pruned nothing.
    """
    if candidate.name in prune:
        return True
    try:
        relative = candidate.relative_to(root).as_posix()
    except ValueError:
        return False
    return relative in prune


class _TolerantLoader(yaml.SafeLoader):
    """SafeLoader that reads a custom YAML tag as its underlying value.

    Compose's merge directives (``!reset``, ``!override``) are unknown tags that
    abort ``yaml.safe_load``, and an overlay fragment carrying one would then be
    dropped from discovery with no explanation.
    """


def _any_tag(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> Any:  # noqa: ARG001
    """Construct an unknown-tag node from its plain YAML value."""
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


_TolerantLoader.add_multi_constructor("!", _any_tag)


def compose_document(path: Path) -> dict | None:
    """Return the parsed compose document at ``path``, or None if it is not one.

    A compose file is identified by a top-level ``services`` mapping, which is
    what separates it from the other YAML a repo keeps under a compose-shaped
    name.
    """
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_TolerantLoader)  # noqa: S506
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("services"), dict):
        return None
    return data


def _compose_surface() -> surfaces.Surface | None:
    """Return the ``docker-compose`` entry from the dependency-surface catalogue."""
    return next((s for s in surfaces.load() if s.id == "docker-compose"), None)


def discover_compose_files(
    root: Path | str, *, exclude_dirs: Iterable[str] = ()
) -> list[Path]:
    """Return every docker-compose file under ``root``, pruned + sorted.

    Naming comes from the ``docker-compose`` surface in
    ``config/dep-surfaces.yaml`` - the one catalogue that already knows a
    ``<service>.compose.yaml`` counts, so the two never disagree about what a
    compose file is called. A claimed file still has to hold a top-level
    ``services`` mapping to be returned.

    Walks the tree rather than asking git, so a compose file added and not yet
    committed is linted like any other.
    """
    root = Path(root)
    surface = _compose_surface()
    if surface is None:
        return []
    prune = _prune(exclude_dirs)
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if not _is_pruned(Path(dirpath) / d, root, prune)
        ]
        here = Path(dirpath)
        for fn in filenames:
            rel = (here / fn).relative_to(root).as_posix()
            if not surfaces.matches(surface, rel):
                continue
            path = here / fn
            if compose_document(path) is not None:
                out.append(path)
    return sorted(out)


# Markdown that documents the repo, not markdown that IS test data. A fixture
# with a deliberately broken link is the expected result of its own test, so
# linting it reports a defect that is the point of the file.
_DOC_PRUNE = {"fixtures", "testdata", "snapshots", "__snapshots__", "site", "_site"}

# CHANGELOG is generated by semantic-release, LICENSE-style files are verbatim
# upstream text. Neither is ours to fix, so neither is ours to lint.
_DOC_SKIP_STEMS = {"CHANGELOG", "LICENSE", "COPYING", "NOTICE"}


def discover_markdown_files(
    root: Path | str, *, exclude_dirs: Iterable[str] = ()
) -> list[Path]:
    """Return every lintable markdown file under ``root``, pruned + sorted.

    Covers ``.md`` and ``.markdown`` anywhere in the tree, so a repo keeping
    docs beside the code is covered as well as one with a ``docs/`` dir. Skips
    generated or verbatim-upstream files (:data:`_DOC_SKIP_STEMS`) and the dirs
    that hold markdown as test DATA (:data:`_DOC_PRUNE`) - a fixture with a
    deliberately broken link must stay broken.
    """
    root = Path(root)
    prune = _prune(exclude_dirs) | _DOC_PRUNE
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if not _is_pruned(Path(dirpath) / d, root, prune)
        ]
        for fn in filenames:
            if not fn.endswith((".md", ".markdown")):
                continue
            if Path(fn).stem.upper() in _DOC_SKIP_STEMS:
                continue
            found.append(Path(dirpath) / fn)
    return sorted(found)


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
        dirnames[:] = [
            d for d in dirnames if not _is_pruned(Path(dirpath) / d, root, prune)
        ]
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
    """Return True when Chart.yaml declares ``type: library`` (renders nothing)."""
    try:
        data = yaml.safe_load(chart_yaml.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return False
    return isinstance(data, dict) and str(data.get("type", "")).lower() == "library"


def _looks_like_manifest(path: Path) -> bool:
    """Return True when any YAML doc in ``path`` has both ``apiVersion`` and ``kind``.

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
    """Return True when ``dirpath`` is at or under a Helm chart (an ancestor Chart.yaml).

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
        dirnames[:] = [
            d for d in dirnames if not _is_pruned(Path(dirpath) / d, root, prune)
        ]
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
