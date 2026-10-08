# Project:   HyperI CI
# File:      src/hyperi_ci/quality/targets.py
# Purpose:   Discover lint targets (Dockerfiles, k8s manifests, markdown) on disk
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Discover the files the container, k8s, IaC and docs linters scan.

This module decides what counts as each kind of target and which directories
are never linted. No target returns ``[]``, and the tool skips.
"""

import os
import subprocess
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import yaml

from hyperi_ci.common import run_cmd
from hyperi_ci.deps import surfaces

KUSTOMIZATION_FILES = ("kustomization.yaml", "kustomization.yml", "Kustomization")

# Pruned whatever the config; .worktrees holds whole duplicate checkouts.
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
    """Return whether ``name`` is a Dockerfile or Containerfile.

    Matches ``Dockerfile``, ``Dockerfile.<x>``, ``<x>.Dockerfile`` and the
    ``Containerfile`` forms.
    """
    for base in ("Dockerfile", "Containerfile"):
        if name == base or name.startswith(f"{base}.") or name.endswith(f".{base}"):
            return True
    return False


def discover_dockerfiles(
    root: Path | str, *, exclude_dirs: Iterable[str] = ()
) -> list[Path]:
    """Return every Dockerfile and Containerfile under ``root``, pruned and sorted.

    ``exclude_dirs`` is added to the always-pruned set.
    """
    found: list[Path] = []
    for here, filenames in walk(root, prune_set(exclude_dirs)):
        found += [here / fn for fn in filenames if _is_dockerfile(fn)]
    return sorted(found)


def exclude_set(exclude_dirs: Iterable[str]) -> set[str]:
    """Return ``exclude_dirs`` in the form :func:`is_pruned` matches against."""
    return {str(d).strip("/") for d in exclude_dirs if d}


def prune_set(exclude_dirs: Iterable[str]) -> set[str]:
    """Return ``exclude_dirs`` plus the directories pruned whatever the config."""
    return _ALWAYS_PRUNE | exclude_set(exclude_dirs)


def is_pruned(candidate: Path, root: Path, prune: set[str]) -> bool:
    """Return whether ``candidate`` is excluded, by bare name or relative path.

    The path match is what lets a nested `quality.exclude_paths` entry such as
    `docs/superpowers` prune anything.
    """
    if candidate.name in prune:
        return True
    try:
        relative = candidate.relative_to(root).as_posix()
    except ValueError:
        return False
    return relative in prune


def walk(
    root: Path | str, prune: set[str], *, skip_hidden: bool = False
) -> Iterator[tuple[Path, list[str]]]:
    """Yield ``(directory, filenames)`` under ``root``, never entering a pruned dir."""
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = [
            d
            for d in dirnames
            if not (skip_hidden and d.startswith("."))
            and not is_pruned(here / d, root, prune)
        ]
        yield here, filenames


def first_file(directory: Path, names: Iterable[str]) -> Path | None:
    """Return the first of ``names`` that is a file in ``directory``, else None."""
    return next((directory / n for n in names if (directory / n).is_file()), None)


def yaml_mapping(path: Path | None) -> dict:
    """Return the YAML mapping in ``path``; {} when absent, unreadable or not a mapping."""
    if path is None:
        return {}
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return doc if isinstance(doc, dict) else {}


class _TolerantLoader(yaml.SafeLoader):
    """SafeLoader that reads a custom YAML tag as its underlying value.

    Compose's ``!reset`` and ``!override`` would otherwise abort the load.
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

    A compose document has a top-level ``services`` mapping.
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
    """Return every docker-compose file under ``root``, pruned and sorted.

    Names come from the ``docker-compose`` surface in ``config/dep-surfaces.yaml``,
    and a match must hold a ``services`` mapping. The tree is walked rather than
    asking git, so an uncommitted file is linted too.
    """
    root = Path(root)
    surface = _compose_surface()
    if surface is None:
        return []
    out: list[Path] = []
    for here, filenames in walk(root, prune_set(exclude_dirs)):
        for fn in filenames:
            rel = (here / fn).relative_to(root).as_posix()
            if not surfaces.matches(surface, rel):
                continue
            path = here / fn
            if compose_document(path) is not None:
                out.append(path)
    return sorted(out)


# Directories holding markdown as test data, whose defects are deliberate.
_DOC_PRUNE = {"fixtures", "testdata", "snapshots", "__snapshots__", "site", "_site"}

# Generated, or licence text that changes only on a licensing decision.
_DOC_SKIP_STEMS = {
    "CHANGELOG",
    "LICENSE",
    "COPYING",
    "NOTICE",
    "COMMERCIAL",
    "AI-TRAINING-POLICY",
}


def discover_markdown_files(
    root: Path | str, *, exclude_dirs: Iterable[str] = ()
) -> list[Path]:
    """Return every ``.md`` and ``.markdown`` file under ``root``, pruned and sorted.

    Skips :data:`_DOC_SKIP_STEMS` files and :data:`_DOC_PRUNE` directories.
    """
    found: list[Path] = []
    for here, filenames in walk(root, prune_set(exclude_dirs) | _DOC_PRUNE):
        for fn in filenames:
            if not fn.endswith((".md", ".markdown")):
                continue
            if Path(fn).stem.upper() in _DOC_SKIP_STEMS:
                continue
            found.append(here / fn)
    return sorted(found)


def git_ignored_dirs(root: Path | str) -> list[str]:
    """Return the directories git ignores under ``root``, repo-relative.

    Used as extra excludes, so a developer's tree lints like a CI checkout.
    Empty outside a git checkout.
    """
    try:
        result = run_cmd(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "--directory",
                "-z",
            ],
            check=False,
            capture=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [e.rstrip("/") for e in result.stdout.split("\0") if e.endswith("/")]


def walk_iac(
    root: Path | str, *, exclude_dirs: Iterable[str] = ()
) -> Iterator[tuple[Path, list[str]]]:
    """Walk ``root`` for IaC, also skipping hidden dirs (agent worktrees, ``.terraform``)."""
    return walk(root, prune_set(exclude_dirs), skip_hidden=True)


def discover_helm_charts(
    root: Path | str, *, exclude_dirs: Iterable[str] = ()
) -> list[Path]:
    """Return the Helm chart directories under ``root``, sorted.

    Skips ``type: library`` charts, which render nothing alone, and subcharts
    under another chart's ``charts/``, which their parent renders.
    """
    charts: list[Path] = []
    for chart_dir, filenames in walk_iac(root, exclude_dirs=exclude_dirs):
        if "Chart.yaml" not in filenames:
            continue
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
    return str(yaml_mapping(chart_yaml).get("type", "")).lower() == "library"


def _looks_like_manifest(path: Path) -> bool:
    """Return True when any YAML doc in ``path`` has both ``apiVersion`` and ``kind``.

    That excludes values files and ``Chart.yaml``; a Helm template usually
    fails the parse.
    """
    try:
        docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError):
        return False
    return any(isinstance(d, dict) and "apiVersion" in d and "kind" in d for d in docs)


def _inside_chart(dirpath: Path, root: Path) -> bool:
    """Return True when ``dirpath`` is at or under a Helm chart (an ancestor Chart.yaml).

    A chart template can parse as a manifest, so chart content is excluded here.
    """
    d = dirpath
    while True:
        if (d / "Chart.yaml").exists():
            return True
        if d == root or d.parent == d:
            return False
        d = d.parent


def kustomization(directory: Path) -> dict:
    """Return the parsed kustomization in ``directory``; {} when it has none."""
    return yaml_mapping(first_file(directory, KUSTOMIZATION_FILES))


# Keys whose values are lists of paths relative to the kustomization.
_KUSTOMIZE_PATH_LISTS = (
    "resources",
    "bases",
    "components",
    "crds",
    "patchesStrategicMerge",
    "configurations",
    "generators",
    "transformers",
    "validators",
)
# Keys holding a list of mappings, and the field in each that is a path.
_KUSTOMIZE_PATH_FIELDS = (
    ("patches", "path"),
    ("patchesJson6902", "path"),
    ("replacements", "path"),
    ("helmCharts", "valuesFile"),
)


def _items(doc: dict, key: str) -> list:
    value = doc.get(key)
    return value if isinstance(value, list) else []


def kustomization_refs(directory: Path) -> list[Path]:
    """Return the local files and directories a kustomization references, resolved.

    Remote URLs and inline patches are not paths and are skipped.
    """
    doc = kustomization(directory)
    raw: list[object] = []
    for key in _KUSTOMIZE_PATH_LISTS:
        raw += _items(doc, key)
    for key, name in _KUSTOMIZE_PATH_FIELDS:
        raw += [e.get(name) for e in _items(doc, key) if isinstance(e, dict)]
    for key in ("configMapGenerator", "secretGenerator"):
        for entry in _items(doc, key):
            if isinstance(entry, dict):
                files = [f for f in _items(entry, "files") if isinstance(f, str)]
                raw += [f.split("=", 1)[-1] for f in files] + _items(entry, "envs")
    refs: list[Path] = []
    for entry in raw:
        text = entry.strip() if isinstance(entry, str) else ""
        if not text or "\n" in text or "://" in text:
            continue
        if text.startswith(("github.com", "git@")):
            continue
        target = (directory / text).resolve()
        if target.exists():
            refs.append(target)
    return refs


def kustomize_owned_files(kustomizations: Iterable[Path]) -> set[Path]:
    """Return the resolved files some kustomization references directly.

    Those are validated through ``kustomize build``; a strategic-merge patch
    alone would fail the schema. An unlisted YAML file beside a kustomization
    stays a plain manifest.
    """
    owned: set[Path] = set()
    for directory in kustomizations:
        own = first_file(directory, KUSTOMIZATION_FILES)
        if own is not None:
            owned.add(own.resolve())
        owned.update(ref for ref in kustomization_refs(directory) if ref.is_file())
    return owned


def discover_kustomizations(
    root: Path | str, *, exclude_dirs: Iterable[str] = ()
) -> list[Path]:
    """Return every directory under ``root`` holding a kustomization file, sorted."""
    return sorted(
        here
        for here, filenames in walk_iac(root, exclude_dirs=exclude_dirs)
        if any(name in filenames for name in KUSTOMIZATION_FILES)
    )


def discover_tofu_dirs(
    root: Path | str, *, exclude_dirs: Iterable[str] = ()
) -> list[Path]:
    """Return every directory under ``root`` holding ``.tf`` or ``.tofu`` files."""
    return sorted(
        here
        for here, filenames in walk_iac(root, exclude_dirs=exclude_dirs)
        if any(fn.endswith((".tf", ".tofu")) for fn in filenames)
    )


def discover_ansible_projects(
    root: Path | str, *, exclude_dirs: Iterable[str] = ()
) -> list[Path]:
    """Return every ansible project directory under ``root``, sorted.

    A project is a directory holding ``ansible.cfg``, or one holding both a
    ``playbooks/`` and a ``roles/`` directory.
    """
    found: list[Path] = []
    for here, filenames in walk_iac(root, exclude_dirs=exclude_dirs):
        laid_out = (here / "playbooks").is_dir() and (here / "roles").is_dir()
        if "ansible.cfg" in filenames or laid_out:
            found.append(here)
    return sorted(found)


def discover_manifests(
    root: Path | str, *, exclude_dirs: Iterable[str] = ()
) -> list[Path]:
    """Return plain (already-rendered) k8s manifest YAML files under ``root``.

    A file counts when :func:`_looks_like_manifest` accepts it, it is not inside
    a chart (:func:`_inside_chart`), and no kustomization references it
    (:func:`kustomize_owned_files`).
    """
    root = Path(root)
    owned = kustomize_owned_files(
        discover_kustomizations(root, exclude_dirs=exclude_dirs)
    )
    out: list[Path] = []
    for here, filenames in walk_iac(root, exclude_dirs=exclude_dirs):
        if _inside_chart(here, root):
            continue
        for fn in filenames:
            if not fn.endswith((".yaml", ".yml")):
                continue
            p = here / fn
            if p.resolve() in owned:
                continue
            if _looks_like_manifest(p):
                out.append(p)
    return sorted(out)
