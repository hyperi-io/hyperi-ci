# Project:   HyperI CI
# File:      src/hyperi_ci/quality/render.py
# Purpose:   Render Helm charts and kustomizations to plain manifests
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Render Helm charts and kustomizations to plain manifests for kubeconform.

A chart renders once per ``ci/*-values.yaml`` (else once on its defaults), with
``iac.helm.values`` and ``iac.helm.set`` applied to every render. Each render
runs twice and must be byte-equal, test hooks aside: ``randAlphaNum``,
``genCA`` or ``now`` make ArgoCD report drift on every sync.

``helm dependency build`` and ``kustomize --enable-helm`` write into the
directory they build, so those builds run on a copy in scratch.
"""

import contextlib
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from hyperi_ci.common import run_cmd, scratch_dir, stage_tree
from hyperi_ci.config import CIConfig
from hyperi_ci.quality import findings as fdg
from hyperi_ci.quality.targets import kustomization, kustomization_refs, yaml_mapping

_STAGE_IGNORE = shutil.ignore_patterns(".git", ".terraform", ".worktrees")


@dataclass(frozen=True, slots=True)
class Rendered:
    """One render of a chart or kustomization.

    Attributes:
        source: The chart or kustomization directory.
        label: What was rendered, for the log (``chart`` or ``chart [values]``).
        output: The rendered manifest file, or None when the render failed.
        finding: Why the render failed or is unstable, or None.

    """

    source: Path
    label: str
    output: Path | None
    finding: fdg.Finding | None

    @property
    def unstable(self) -> bool:
        """Whether the finding is two renders differing, not a failed render."""
        return self.finding is not None and self.finding.rule.endswith(
            "/render-unstable"
        )


def _release_name(chart: Path) -> str:
    """Return a valid release name from the chart dir, which helm may reject as one."""
    name = re.sub(r"[^a-z0-9-]", "-", chart.name.lower()).strip("-")
    return (name or "chart")[:53]


def _slug(path: Path, root: Path) -> str:
    """Return a filename-safe name for ``path`` relative to ``root``."""
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        rel = path.name
    return re.sub(r"[^A-Za-z0-9_.-]", "_", rel) or "root"


def ci_values(chart: Path) -> list[Path]:
    """Return the chart's ``ci/*-values.yaml`` files, sorted (empty when none)."""
    ci_dir = chart / "ci"
    if not ci_dir.is_dir():
        return []
    return sorted(
        p for p in ci_dir.iterdir() if p.is_file() and p.name.endswith("-values.yaml")
    )


def helm_value_args(config: CIConfig, root: Path) -> list[str]:
    """Return the ``-f`` / ``--set`` arguments from ``iac.helm.values`` and ``.set``.

    ``iac.helm.set`` takes a mapping or a list of ``key=value`` strings.
    """
    args: list[str] = []
    values = config.get("iac.helm.values", [])
    if isinstance(values, list):
        for entry in values:
            args += ["-f", str((root / str(entry)).resolve())]
    sets = config.get("iac.helm.set", {})
    if isinstance(sets, dict):
        sets = [f"{key}={value}" for key, value in sets.items()]
    if isinstance(sets, list):
        for entry in sets:
            args += ["--set", str(entry)]
    return args


def _chart_dependencies(chart: Path) -> list[dict]:
    deps = yaml_mapping(chart / "Chart.yaml").get("dependencies")
    return [d for d in deps if isinstance(d, dict)] if isinstance(deps, list) else []


def missing_dependencies(chart: Path) -> list[str]:
    """Return the dependencies ``Chart.yaml`` declares that ``charts/`` lacks."""
    vendored = chart / "charts"
    missing: list[str] = []
    for dep in _chart_dependencies(chart):
        name = str(dep.get("name", ""))
        if not name:
            continue
        has_dir = (vendored / name).is_dir()
        has_tgz = vendored.is_dir() and any(vendored.glob(f"{name}-*.tgz"))
        if not (has_dir or has_tgz):
            missing.append(name)
    return missing


def _file_dependencies(chart: Path) -> list[Path]:
    """Return the local directories a chart's ``file://`` dependencies point at."""
    out: list[Path] = []
    for dep in _chart_dependencies(chart):
        repo = str(dep.get("repository", ""))
        if repo.startswith("file://"):
            out.append((chart / repo.removeprefix("file://")).resolve())
    return out


def _first_line(result: subprocess.CompletedProcess[str]) -> str:
    """Return the first non-empty line of a failed command's output."""
    for text in (result.stderr or "", result.stdout or ""):
        for line in text.splitlines():
            if line.strip():
                return line.strip()
    return f"exited {result.returncode}"


def _first_difference(first: str, second: str) -> int:
    """Return the 1-indexed line where two renders first differ."""
    for number, (a, b) in enumerate(
        zip(first.splitlines(), second.splitlines(), strict=False), start=1
    ):
        if a != b:
            return number
    return min(len(first.splitlines()), len(second.splitlines())) + 1


def _is_test_hook(chunk: str) -> bool:
    """Report whether a YAML document carries ``helm.sh/hook: test...``."""
    if "helm.sh/hook" not in chunk:
        return False
    try:
        doc = yaml.safe_load(chunk)
    except yaml.YAMLError:
        return False
    metadata = doc.get("metadata") if isinstance(doc, dict) else None
    annotations = metadata.get("annotations") if isinstance(metadata, dict) else None
    if not isinstance(annotations, dict):
        return False
    hooks = str(annotations.get("helm.sh/hook", "")).split(",")
    return any(h.strip().startswith("test") for h in hooks)


def without_test_hooks(text: str) -> str:
    """Drop the documents annotated ``helm.sh/hook: test...`` from a multi-doc stream."""
    chunks = re.split(r"(?m)^---[ \t]*$", text)
    return "---".join(c for c in chunks if not _is_test_hook(c))


def _render(
    tool: str,
    source: Path,
    label: str,
    cmd: list[str],
    dest: Path,
    timeout: float | None,
) -> Rendered:
    """Run a render twice; write the first output to ``dest`` and return the render."""

    def finding(rule: str, message: str) -> fdg.Finding:
        path = fdg.relpath(source)
        return fdg.Finding(
            tool, path, None, "error", f"{tool}/{rule}", f"{label}: {message}"
        )

    outputs: list[str] = []
    for _ in range(2):
        result = fdg.run_tool(cmd, finding, timeout=timeout)
        if isinstance(result, fdg.Finding):
            return Rendered(source, label, None, result)
        if result.returncode != 0:
            failed = finding("render-failed", _first_line(result))
            return Rendered(source, label, None, failed)
        outputs.append(result.stdout)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(outputs[0], encoding="utf-8", newline="\n")
    first, second = without_test_hooks(outputs[0]), without_test_hooks(outputs[1])
    unstable = None
    if first != second:
        unstable = finding(
            "render-unstable",
            f"two renders differ from line {_first_difference(first, second)} - a "
            "template calls randAlphaNum, genCA, now or similar, so every sync "
            "reports drift",
        )
    return Rendered(source, label, dest, unstable)


def render_chart(
    helm: str,
    chart: Path,
    *,
    root: Path,
    extra_args: list[str],
    out_dir: Path,
    stage_dir: Path,
    timeout: float | None = None,
) -> list[Rendered]:
    """Render ``chart`` once per value set, twice each; return every render.

    A chart missing a dependency is copied under ``stage_dir`` and built there.
    """
    rel = _slug(chart, root)
    with scratch_dir(stage_dir) as stage:
        source = chart
        if missing_dependencies(chart):
            source = stage_tree(chart, root, stage, _file_dependencies, _STAGE_IGNORE)
            # A failed build surfaces as the template failure that names the chart.
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                run_cmd(
                    [helm, "dependency", "build", str(source)],
                    check=False,
                    capture=True,
                    timeout=timeout,
                    own_group=True,
                )
        base = [helm, "template", _release_name(chart), str(source), "--skip-tests"]
        renders: list[Rendered] = []
        for values in ci_values(source) or [None]:
            cmd, label, suffix = [*base, *extra_args], rel, "defaults"
            if values is not None:
                cmd += ["-f", str(values)]
                label, suffix = f"{rel} [{values.name}]", values.stem
            dest = out_dir / f"{rel}--{suffix}.yaml"
            renders.append(_render("helm", chart, label, cmd, dest, timeout))
    return renders


def render_kustomization(
    kustomize: str,
    directory: Path,
    *,
    root: Path,
    out_dir: Path,
    stage_dir: Path,
    timeout: float | None = None,
) -> Rendered:
    """Build one kustomization, twice; return the render.

    One that inflates Helm charts is copied under ``stage_dir`` and built there.
    """
    rel = _slug(directory, root)
    with scratch_dir(stage_dir) as stage:
        cmd = [kustomize, "build", str(directory)]
        if kustomization(directory).get("helmCharts"):
            staged = stage_tree(
                directory, root, stage, kustomization_refs, _STAGE_IGNORE
            )
            cmd = [kustomize, "build", str(staged), "--enable-helm"]
        return _render(
            "kustomize", directory, rel, cmd, out_dir / f"{rel}.yaml", timeout
        )
