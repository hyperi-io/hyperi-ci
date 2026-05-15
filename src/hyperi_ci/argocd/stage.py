# Project:   HyperI CI
# File:      src/hyperi_ci/argocd/stage.py
# Purpose:   ArgoCD stage orchestrator: emit-argocd → splice → push to gitops
#
# License:   Proprietary - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""ArgoCD stage handler.

Reads ``publish.argocd`` from ``.hyperi-ci.yaml``:

* ``enabled`` (bool, default false) — gate the stage.
* ``repo`` (str, default ``hyperi-io/gitops``) — central gitops repo.
* ``path`` (str template, default
  ``applications/${{ APP }}/${{ ENV }}.yaml``) — destination inside repo.
* ``envs`` (list of ``{name, push_mode}``) — env-specific settings.
  ``push_mode`` is ``direct`` or ``pr``. dev/staging default direct;
  prod defaults to ``pr``.
* ``overlays`` (list of overlays per spec section 3.3).
* ``binary_name`` (str, default ``Path.cwd().name``) — consumer
  binary that exposes ``emit-argocd``.

Push happens only in publish mode (workflow_dispatch / publish trailer);
push-to-main runs validate-only (renders Application YAML + parses it,
no push).
"""

from __future__ import annotations

import os
import string
import subprocess
from pathlib import Path

from hyperi_ci.common import error, group, info, success, warn
from hyperi_ci.config import CIConfig

_DEFAULT_REPO = "hyperi-io/gitops"
_DEFAULT_PATH_TEMPLATE = "applications/${APP}/${ENV}.yaml"
_DEFAULT_PROD_PUSH_MODE = "pr"
_DEFAULT_NONPROD_PUSH_MODE = "direct"


def run(config: CIConfig) -> int:
    """Run the ArgoCD stage. Returns process exit code."""
    argocd_cfg = config.get("publish.argocd", {})
    if not isinstance(argocd_cfg, dict):
        argocd_cfg = {}
    if not argocd_cfg.get("enabled"):
        info("ArgoCD publish disabled (publish.argocd.enabled: false) — skipping")
        return 0

    project_dir = Path.cwd()
    binary_name = argocd_cfg.get("binary_name") or project_dir.name
    repo = argocd_cfg.get("repo") or _DEFAULT_REPO
    path_template = argocd_cfg.get("path") or _DEFAULT_PATH_TEMPLATE
    envs = _resolve_envs(argocd_cfg)
    publish_mode = _is_publish_mode()

    with group(f"ArgoCD Stage ({'push' if publish_mode else 'validate'})"):
        # Generate base Application YAML once (env-specific differences
        # are typically values-only and handled inside the chart, not in
        # the Application YAML; if a consumer needs per-env Application
        # divergence, they can declare per-env overlays).
        base_yaml, rc = _emit_argocd(binary_name=binary_name)
        if rc != 0:
            return rc

        final_yaml, rc = _apply_overlays(
            argocd_cfg=argocd_cfg, base_yaml=base_yaml, project_dir=project_dir
        )
        if rc != 0:
            return rc

        if not publish_mode:
            success(
                "ArgoCD Application YAML generated and validated "
                "(no push on validate mode)"
            )
            return 0

        return _push_to_gitops(
            repo=repo,
            path_template=path_template,
            envs=envs,
            final_yaml=final_yaml,
        )


# ---- pipeline steps -----------------------------------------------------


def _emit_argocd(*, binary_name: str) -> tuple[str, int]:
    info(f"  argocd: invoking {binary_name} emit-argocd")
    proc = subprocess.run(
        [binary_name, "emit-argocd"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        if proc.stderr:
            error(proc.stderr.rstrip())
        return "", proc.returncode
    return proc.stdout, 0


def _apply_overlays(
    *,
    argocd_cfg: dict,
    base_yaml: str,
    project_dir: Path,
) -> tuple[str, int]:
    raw_overlays = argocd_cfg.get("overlays")
    if not raw_overlays:
        return base_yaml, 0

    from hyperi_ci.deployment.overlay import apply_overlays
    from hyperi_ci.deployment.overlay.anchors.argocd import (
        ArgoCDAnchorResolver,
    )
    from hyperi_ci.deployment.overlay.errors import OverlayError
    from hyperi_ci.deployment.overlay.model import parse_simple_overlays

    try:
        overlays = parse_simple_overlays(raw_overlays, artefact="argocd")
        final = apply_overlays(
            base=base_yaml,
            overlays=overlays,
            resolver=ArgoCDAnchorResolver(),
            base_dir=project_dir,
            artefact="argocd",
        )
    except OverlayError as exc:
        error(str(exc))
        return "", 1
    info(f"  argocd: applied {len(overlays)} overlay(s) to Application YAML")
    return final, 0


def _push_to_gitops(
    *,
    repo: str,
    path_template: str,
    envs: list[tuple[str, str]],
    final_yaml: str,
) -> int:
    from hyperi_ci.argocd.gitops_push import GitopsPushConfig, push

    if not envs:
        warn("publish.argocd.enabled: true but no envs declared — nothing to push")
        return 0

    app = Path.cwd().name
    rcs: list[int] = []
    for env_name, push_mode in envs:
        path = string.Template(path_template).safe_substitute(APP=app, ENV=env_name)
        cfg = GitopsPushConfig(
            repo=repo,
            path=path,
            content=final_yaml,
            commit_message=(
                f"chore({app}): update {env_name} application yaml from "
                f"{os.environ.get('GITHUB_SHA', '')[:8] or 'local'}"
            ),
            push_mode=push_mode,
        )
        rcs.append(push(cfg))
    return max(rcs) if rcs else 0


def _resolve_envs(argocd_cfg: dict) -> list[tuple[str, str]]:
    """Read ``publish.argocd.envs`` into ``[(env_name, push_mode), ...]``.

    Defaults: prod → pr, others → direct. If a single string env name
    is given, treat as direct unless its name is "prod".
    """
    raw = argocd_cfg.get("envs") or []
    out: list[tuple[str, str]] = []
    for entry in raw:
        if isinstance(entry, str):
            mode = (
                _DEFAULT_PROD_PUSH_MODE
                if entry == "prod"
                else _DEFAULT_NONPROD_PUSH_MODE
            )
            out.append((entry, mode))
        elif isinstance(entry, dict):
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                continue
            mode = entry.get("push_mode") or (
                _DEFAULT_PROD_PUSH_MODE
                if name == "prod"
                else _DEFAULT_NONPROD_PUSH_MODE
            )
            out.append((name, str(mode)))
    return out


def _is_publish_mode() -> bool:
    """Return True when the workflow has signalled this is a publish run.

    Same logic as ``container/stage.py`` and ``helm/stage.py``.
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
