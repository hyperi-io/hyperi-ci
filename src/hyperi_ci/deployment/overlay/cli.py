# Project:   HyperI CI
# File:      src/hyperi_ci/deployment/overlay/cli.py
# Purpose:   `hyperi-ci overlay-render` subcommand handler
#
# License:   Proprietary - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""``hyperi-ci overlay-render`` - subprocess into the consumer's
contract generator, splice declared overlays, write the final artefact.

Used by:
  * The container/helm/argocd build stages internally (subprocess
    helper called from stage.py).
  * Developers running local builds: ``hyperi-ci overlay-render
    --kind dockerfile -o Dockerfile.final && docker build -f
    Dockerfile.final .``.
  * Inspection / debugging.

Three artefact kinds:
  * ``dockerfile`` - single text file output (stdout or -o)
  * ``helm`` - directory output (-o required, defaults to ./chart)
  * ``argocd`` - single YAML output (stdout or -o)

Subprocess contract: the consumer must expose ``<binary> emit-{kind}``
on its CLI. For Helm, the subcommand is ``emit-chart <output-dir>``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from hyperi_ci.common import error, info
from hyperi_ci.config import load_config


def render(
    *,
    kind: str | None,
    project_dir: Path,
    output: Path | None,
    binary: str | None = None,
) -> int:
    """Run overlay-render for one or all artefact kinds.

    Args:
        kind: ``dockerfile`` | ``helm`` | ``argocd``. ``None`` means
            "emit all three" (default — mirrors the deployment
            contract's bulk-output behaviour). When emitting all three,
            ``output`` must be a directory; the layout is::

                <output>/Dockerfile
                <output>/chart/             (Helm chart directory)
                <output>/argocd-application.yaml

        project_dir: Project root containing ``.hyperi-ci.yaml`` and
            the consumer binary.
        output: Where to write the final artefact(s). For single-kind
            renders, stdout if None (only meaningful for single-file
            artefacts — Helm requires ``--output``). For all-three
            renders, defaults to ``./ci-overlay/`` if None.
        binary: Override the consumer binary path. Defaults to
            ``<project_dir>/<project_name>`` resolved against PATH.

    Returns:
        Process exit code.
    """
    handlers: dict[str, Callable[..., int]] = {
        "dockerfile": _render_dockerfile,
        "helm": _render_helm,
        "argocd": _render_argocd,
    }

    # Default "all three" path
    if kind is None:
        out_dir = output or (project_dir / "ci-overlay")
        out_dir.mkdir(parents=True, exist_ok=True)
        info(f"  overlay-render: emitting all three artefacts into {out_dir}")
        rc = _render_dockerfile(
            project_dir=project_dir,
            output=out_dir / "Dockerfile",
            binary=binary,
        )
        if rc != 0:
            return rc
        rc = _render_helm(
            project_dir=project_dir,
            output=out_dir / "chart",
            binary=binary,
        )
        if rc != 0:
            return rc
        rc = _render_argocd(
            project_dir=project_dir,
            output=out_dir / "argocd-application.yaml",
            binary=binary,
        )
        return rc

    handler = handlers.get(kind)
    if handler is None:
        error(
            f"Unknown overlay kind {kind!r} - expected one of "
            f"{sorted(handlers)!r}"
        )
        return 2
    return handler(project_dir=project_dir, output=output, binary=binary)


def _resolve_binary(project_dir: Path, binary: str | None) -> str:
    """Pick the consumer binary to subprocess into.

    Priority:
      1. Explicit ``--binary`` flag (if absolute path, used as-is;
         if relative, resolved against ``project_dir``).
      2. PATH lookup of ``<project_dir.name>``.

    Returns the resolved binary path. Errors are caller's problem
    when the subprocess invocation itself fails.
    """
    if binary:
        path = Path(binary)
        if not path.is_absolute():
            path = project_dir / path
        return str(path)
    return project_dir.name


def _emit_subprocess(
    *,
    binary: str,
    subcommand: list[str],
    project_dir: Path,
    capture_text: bool = True,
) -> tuple[int, str]:
    """Run the consumer binary's emit-* subcommand."""
    cmd = [binary, *subcommand]
    info(f"  overlay-render: invoking {' '.join(cmd)}")
    proc = subprocess.run(
        cmd,
        cwd=project_dir,
        capture_output=capture_text,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        if proc.stderr:
            error(proc.stderr.rstrip())
        return proc.returncode, ""
    return 0, proc.stdout


def _load_publish_block(project_dir: Path) -> dict:
    """Load `publish:` from .hyperi-ci.yaml. Empty dict if missing.

    Uses ``reload=True`` because a single CLI invocation may render
    across multiple project_dirs (e.g. the all-three default path) and
    `load_config` caches at module level — without reload, the second
    call returns the first project's config.
    """
    cfg = load_config(project_dir=project_dir, reload=True)
    publish = cfg.get("publish", {})
    return publish if isinstance(publish, dict) else {}


# ---- per-kind handlers -----------------------------------------------------


def _render_dockerfile(
    *, project_dir: Path, output: Path | None, binary: str | None
) -> int:
    from hyperi_ci.deployment.overlay import apply_overlays
    from hyperi_ci.deployment.overlay.anchors.dockerfile import (
        DockerfileAnchorResolver,
    )
    from hyperi_ci.deployment.overlay.model import parse_overlay_config

    resolved_bin = _resolve_binary(project_dir, binary)
    rc, base = _emit_subprocess(
        binary=resolved_bin,
        subcommand=["emit-dockerfile"],
        project_dir=project_dir,
    )
    if rc != 0:
        return rc

    publish = _load_publish_block(project_dir)
    overlay_cfg = parse_overlay_config(publish)
    binary_name = (
        publish.get("container", {}).get("binary_name") or project_dir.name
    )
    resolver = DockerfileAnchorResolver(binary_name=binary_name)
    final = apply_overlays(
        base=base,
        overlays=overlay_cfg.container,
        resolver=resolver,
        base_dir=project_dir,
        artefact="container",
    )

    if output is None:
        sys.stdout.write(final)
        return 0
    output.write_text(final, encoding="utf-8", newline="\n")
    info(f"  overlay-render: wrote {output}")
    return 0


def _render_helm(
    *, project_dir: Path, output: Path | None, binary: str | None
) -> int:
    from hyperi_ci.deployment.overlay.anchors.helm import HelmAnchorResolver
    from hyperi_ci.deployment.overlay.model import parse_overlay_config

    if output is None:
        error("--output is required for --kind helm (chart is a directory)")
        return 2
    output.mkdir(parents=True, exist_ok=True)

    resolved_bin = _resolve_binary(project_dir, binary)
    # emit-chart writes the chart into the directory passed as argument.
    # We use a temp dir, then run adds, then copy/move to the final output.
    with tempfile.TemporaryDirectory(prefix="hyperi-overlay-helm-") as tmpdir:
        tmpchart = Path(tmpdir) / "chart"
        proc = subprocess.run(
            [resolved_bin, "emit-chart", str(tmpchart)],
            cwd=project_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if proc.returncode != 0:
            if proc.stderr:
                error(proc.stderr.rstrip())
            return proc.returncode

        publish = _load_publish_block(project_dir)
        overlay_cfg = parse_overlay_config(publish)
        resolver = HelmAnchorResolver()
        if overlay_cfg.helm.adds:
            written = resolver.apply_adds(
                chart_dir=tmpchart,
                adds=overlay_cfg.helm.adds,
                base_dir=project_dir,
            )
            for w in written:
                info(f"  overlay-render: helm add wrote {w.relative_to(tmpchart)}")
        # Patches are post-render - they apply to `helm template` output,
        # not to the chart dir. The helm/stage.py orchestrator handles
        # that step. `overlay-render --kind helm` writes the (adds-applied)
        # chart and leaves patching to the caller.

        # Copy chart into the final output (clean target first to avoid
        # mixing with a stale chart from a previous run).
        if output.exists():
            for child in output.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        else:
            output.mkdir(parents=True)
        for child in tmpchart.iterdir():
            target = output / child.name
            if child.is_dir():
                shutil.copytree(child, target)
            else:
                shutil.copy2(child, target)

    info(f"  overlay-render: chart at {output}")
    return 0


def _render_argocd(
    *, project_dir: Path, output: Path | None, binary: str | None
) -> int:
    from hyperi_ci.deployment.overlay import apply_overlays
    from hyperi_ci.deployment.overlay.anchors.argocd import (
        ArgoCDAnchorResolver,
    )
    from hyperi_ci.deployment.overlay.model import parse_overlay_config

    resolved_bin = _resolve_binary(project_dir, binary)
    rc, base = _emit_subprocess(
        binary=resolved_bin,
        subcommand=["emit-argocd"],
        project_dir=project_dir,
    )
    if rc != 0:
        return rc

    publish = _load_publish_block(project_dir)
    overlay_cfg = parse_overlay_config(publish)
    resolver = ArgoCDAnchorResolver()
    final = apply_overlays(
        base=base,
        overlays=overlay_cfg.argocd,
        resolver=resolver,
        base_dir=project_dir,
        artefact="argocd",
    )

    if output is None:
        sys.stdout.write(final)
        return 0
    output.write_text(final, encoding="utf-8", newline="\n")
    info(f"  overlay-render: wrote {output}")
    return 0
