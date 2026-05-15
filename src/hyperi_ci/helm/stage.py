# Project:   HyperI CI
# File:      src/hyperi_ci/helm/stage.py
# Purpose:   Helm stage orchestrator: emit-chart → adds → lint → template
#            → patches → package → push (oci://ghcr.io/hyperi-io/helm-charts)
#
# License:   Proprietary - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Helm stage handler.

Reads ``publish.helm`` from ``.hyperi-ci.yaml``:

* ``enabled`` (bool, default false) — gate the whole stage.
* ``registry`` (str, default ``oci://ghcr.io/hyperi-io/helm-charts``)
  — push target.
* ``overlays`` (mapping with ``adds`` + ``patches``) — see the overlay
  framework spec for the schema.
* ``binary_name`` (str, default ``Path.cwd().name``) — consumer
  binary that exposes ``emit-chart``.

The push step is skipped on push-to-main (validate mode) and runs on
workflow_dispatch / publish-trailer flow (push mode), mirroring the
existing container stage behaviour.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from hyperi_ci.common import error, group, info, success, warn
from hyperi_ci.config import CIConfig


def run(config: CIConfig) -> int:
    """Run the helm stage.

    Returns process exit code (0 = success or skipped).
    """
    helm_cfg = config.get("publish.helm", {})
    if not isinstance(helm_cfg, dict):
        helm_cfg = {}
    if not helm_cfg.get("enabled"):
        info("Helm publish disabled (publish.helm.enabled: false) — skipping")
        return 0

    if shutil.which("helm") is None:
        error("`helm` binary not found on PATH — cannot run helm stage")
        return 1

    project_dir = Path.cwd()
    binary_name = helm_cfg.get("binary_name") or project_dir.name
    registry = helm_cfg.get("registry") or "oci://ghcr.io/hyperi-io/helm-charts"
    publish_mode = _is_publish_mode()

    with group(f"Helm Stage ({'push' if publish_mode else 'validate'})"):
        with tempfile.TemporaryDirectory(prefix="hyperi-helm-") as tmpdir:
            workspace = Path(tmpdir)
            chart_dir = workspace / "chart"

            rc = _emit_chart(binary_name=binary_name, chart_dir=chart_dir)
            if rc != 0:
                return rc

            rc = _apply_adds(
                helm_cfg=helm_cfg,
                chart_dir=chart_dir,
                project_dir=project_dir,
            )
            if rc != 0:
                return rc

            rc = _helm_lint(chart_dir)
            if rc != 0:
                return rc

            patches_present = bool(
                helm_cfg.get("overlays", {}).get("patches")
            )
            rendered_path: Path | None = None
            if patches_present:
                rendered_path, rc = _helm_template_and_patch(
                    helm_cfg=helm_cfg,
                    chart_dir=chart_dir,
                    workspace=workspace,
                    project_dir=project_dir,
                )
                if rc != 0:
                    return rc

            tgz_path, rc = _helm_package(chart_dir=chart_dir, dest=workspace)
            if rc != 0:
                return rc

            if not publish_mode:
                success(
                    f"Helm chart built and validated at {tgz_path.name} "
                    "(no push on validate mode)"
                )
                return 0

            return _helm_push(tgz_path=tgz_path, registry=registry)


# ---- pipeline steps ------------------------------------------------------


def _emit_chart(*, binary_name: str, chart_dir: Path) -> int:
    """Subprocess ``<binary_name> emit-chart <chart_dir>``."""
    info(f"  helm: invoking {binary_name} emit-chart {chart_dir}")
    proc = subprocess.run(
        [binary_name, "emit-chart", str(chart_dir)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        if proc.stderr:
            error(proc.stderr.rstrip())
        error(f"emit-chart failed (exit {proc.returncode})")
        return proc.returncode
    if not chart_dir.exists() or not (chart_dir / "Chart.yaml").exists():
        error(
            f"emit-chart returned 0 but no Chart.yaml at {chart_dir} — "
            "consumer's emit-chart subcommand is broken"
        )
        return 1
    return 0


def _apply_adds(
    *,
    helm_cfg: dict,
    chart_dir: Path,
    project_dir: Path,
) -> int:
    """Apply ``publish.helm.overlays.adds`` to the chart dir."""
    overlays_raw = helm_cfg.get("overlays")
    if not overlays_raw:
        return 0

    from hyperi_ci.deployment.overlay.anchors.helm import HelmAnchorResolver
    from hyperi_ci.deployment.overlay.errors import OverlayError
    from hyperi_ci.deployment.overlay.model import parse_helm_overlays

    try:
        helm_overlays = parse_helm_overlays(overlays_raw)
    except OverlayError as exc:
        error(str(exc))
        return 1
    if not helm_overlays.adds:
        return 0

    resolver = HelmAnchorResolver()
    try:
        written = resolver.apply_adds(
            chart_dir=chart_dir,
            adds=helm_overlays.adds,
            base_dir=project_dir,
        )
    except OverlayError as exc:
        error(str(exc))
        return 1
    for w in written:
        info(f"  helm: added {w.relative_to(chart_dir)}")
    return 0


def _helm_lint(chart_dir: Path) -> int:
    info(f"  helm: linting chart at {chart_dir}")
    proc = subprocess.run(
        ["helm", "lint", str(chart_dir)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        if proc.stdout:
            error(proc.stdout.rstrip())
        if proc.stderr:
            error(proc.stderr.rstrip())
        return proc.returncode
    return 0


def _helm_template_and_patch(
    *,
    helm_cfg: dict,
    chart_dir: Path,
    workspace: Path,
    project_dir: Path,
) -> tuple[Path | None, int]:
    """Render chart with ``helm template`` then apply patches.

    Writes the patched output back into the chart's ``templates/`` as a
    single ``_overlay-rendered.yaml`` so ``helm package`` includes it.
    Original templates are removed in favour of the rendered output to
    avoid double-rendering.

    Returns ``(rendered_path, exit_code)``. ``rendered_path`` is None
    if no patches were applied.
    """
    from hyperi_ci.deployment.overlay.anchors.helm import HelmAnchorResolver
    from hyperi_ci.deployment.overlay.errors import OverlayError
    from hyperi_ci.deployment.overlay.model import parse_helm_overlays

    helm_overlays = parse_helm_overlays(helm_cfg.get("overlays"))
    if not helm_overlays.patches:
        return None, 0

    info(f"  helm: rendering chart for post-render patching ({len(helm_overlays.patches)} patches)")
    proc = subprocess.run(
        ["helm", "template", "release-name", str(chart_dir)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        if proc.stderr:
            error(proc.stderr.rstrip())
        return None, proc.returncode

    resolver = HelmAnchorResolver()
    try:
        patched = resolver.apply_patches(
            rendered_yaml=proc.stdout,
            patches=helm_overlays.patches,
            base_dir=project_dir,
        )
    except OverlayError as exc:
        error(str(exc))
        return None, 1

    # Replace templates/ with the post-rendered single-file output.
    # NOTE: this changes the chart's value-substitution semantics —
    # consumers using `--set` at install time will NOT have those values
    # applied to the post-rendered manifest. Document this in CLAUDE.md
    # for any consumer that uses both patches AND install-time values.
    templates_dir = chart_dir / "templates"
    if templates_dir.exists():
        for child in templates_dir.iterdir():
            if child.is_file():
                child.unlink()
            else:
                shutil.rmtree(child)
    templates_dir.mkdir(parents=True, exist_ok=True)
    overlay_file = templates_dir / "_overlay-rendered.yaml"
    overlay_file.write_text(patched, encoding="utf-8", newline="\n")
    info(
        "  helm: post-rendered manifest written into chart templates "
        f"({overlay_file.name})"
    )
    return overlay_file, 0


def _helm_package(*, chart_dir: Path, dest: Path) -> tuple[Path, int]:
    """Run ``helm package`` and return the tarball path + exit code."""
    info(f"  helm: packaging {chart_dir}")
    proc = subprocess.run(
        ["helm", "package", str(chart_dir), "-d", str(dest)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        if proc.stderr:
            error(proc.stderr.rstrip())
        return Path(), proc.returncode

    # `helm package` prints the tgz path on stdout.
    tgz_line = proc.stdout.strip().splitlines()[-1] if proc.stdout else ""
    tgz_path = Path(tgz_line.split(":")[-1].strip()) if ":" in tgz_line else None
    if tgz_path is None or not tgz_path.exists():
        # Fallback: scan dest dir for the only .tgz.
        candidates = list(dest.glob("*.tgz"))
        if len(candidates) == 1:
            tgz_path = candidates[0]
        else:
            error(
                f"helm package exited 0 but couldn't locate the .tgz in {dest} "
                f"(stdout: {proc.stdout!r})"
            )
            return Path(), 1
    info(f"  helm: packaged {tgz_path.name}")
    return tgz_path, 0


def _helm_push(*, tgz_path: Path, registry: str) -> int:
    """``helm push <tgz> <registry>`` to GHCR OCI."""
    if not registry.startswith("oci://"):
        warn(
            f"helm registry {registry!r} doesn't look OCI — push semantics "
            "may differ. Expected oci://ghcr.io/hyperi-io/helm-charts."
        )
    info(f"  helm: pushing {tgz_path.name} → {registry}")

    # Helm reads $HELM_REGISTRY_USERNAME / $HELM_REGISTRY_PASSWORD for OCI
    # auth. For GHCR the username can be anything non-empty; the password
    # is the GH token. Wire these from existing GHCR auth env vars so
    # consumers don't need separate configuration.
    env = os.environ.copy()
    if "HELM_REGISTRY_PASSWORD" not in env:
        token = (
            env.get("GHCR_TOKEN")
            or env.get("GITHUB_TOKEN")
            or env.get("GITHUB_WRITE_TOKEN")
        )
        if token:
            env["HELM_REGISTRY_USERNAME"] = env.get(
                "GITHUB_REPOSITORY_OWNER", "hyperi-io"
            )
            env["HELM_REGISTRY_PASSWORD"] = token
        else:
            warn(
                "No GHCR / GH token in environment — `helm push` will fail "
                "if the registry requires auth"
            )

    proc = subprocess.run(
        ["helm", "push", str(tgz_path), registry],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        if proc.stdout:
            error(proc.stdout.rstrip())
        if proc.stderr:
            error(proc.stderr.rstrip())
        return proc.returncode
    success(f"Helm chart published: {tgz_path.name} → {registry}")
    return 0


def _is_publish_mode() -> bool:
    """Return True when the workflow has signalled this is a publish run.

    Same logic as ``container/stage.py`` and ``argocd/stage.py``.
    """
    flag = os.environ.get("HYPERCI_PUBLISH_MODE", "").strip().lower()
    if flag in ("true", "1", "yes"):
        return True
    if flag in ("false", "0", "no"):
        return False
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        return True
    if (
        os.environ.get("GITHUB_EVENT_NAME") == "push"
        and os.environ.get("GITHUB_REF") == "refs/heads/main"
    ):
        return False
    return False
