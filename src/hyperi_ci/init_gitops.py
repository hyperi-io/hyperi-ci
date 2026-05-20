# Project:   HyperI CI
# File:      src/hyperi_ci/init_gitops.py
# Purpose:   Scaffold a new hyperi-io/gitops monorepo from templates
#
# License:   Proprietary - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Scaffold a new hyperi-io/gitops monorepo from packaged templates.

Provides :func:`init_gitops` (full repo skeleton: topologies, argocd,
values, terraform, docs site, workflows) and :func:`init_topology`
(single topology directory inside an existing gitops repo).
Templates live under ``src/hyperi_ci/gitops_templates/`` and are
packaged into the wheel via ``[tool.hatch.build.targets.wheel.force-include]``.
"""

from __future__ import annotations

import re
from importlib.resources import files
from pathlib import Path

from hyperi_ci.common import info, success, warn


class GitopsInitError(Exception):
    """Raised when the gitops repo cannot be scaffolded."""


def init_gitops(
    target: Path | str,
    *,
    org: str = "hyperi-io",
    force: bool = False,
) -> int:
    """Scaffold a new gitops monorepo at *target* from bundled templates.

    Args:
        target: Destination directory (created if it does not exist).
        org:    GitHub org name substituted into CODEOWNERS (default: hyperi-io).
        force:  If True, write into a non-empty directory without deleting
                existing files.  If False and the directory is non-empty,
                raise GitopsInitError.

    Returns:
        0 on success.

    Raises:
        GitopsInitError: When *target* is non-empty and *force* is False.

    """
    target = Path(target).resolve()

    if target.exists() and any(target.iterdir()) and not force:
        raise GitopsInitError(
            f"target directory {target} is not empty; pass --force to overwrite"
        )

    target.mkdir(parents=True, exist_ok=True)

    templates_root = files("hyperi_ci").joinpath("gitops_templates")
    written = 0
    for tpl in _walk_templates(templates_root):
        rel = Path(str(tpl)).relative_to(str(templates_root))

        # Rewrite workflow files: workflows/ → .github/workflows/
        if rel.parts and rel.parts[0] == "workflows":
            dest = target / ".github" / "workflows" / rel.relative_to("workflows")
        else:
            dest = target / rel

        dest.parent.mkdir(parents=True, exist_ok=True)

        # .gitkeep markers preserve empty dirs in git; skip them in the output
        if tpl.name == ".gitkeep":
            continue

        content = Path(str(tpl)).read_text(encoding="utf-8")
        content = content.replace("{{ ORG }}", org)
        dest.write_text(content, encoding="utf-8", newline="\n")
        info(f"  wrote {rel}")
        written += 1

    success(f"Initialised gitops repo at {target} ({written} files written)")
    return 0


# ---------------------------------------------------------------------------
# Topology scaffolding
# ---------------------------------------------------------------------------

_TOPO_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,49}[a-z0-9]$")


def init_topology(
    *,
    gitops_root: Path | str,
    name: str,
    apps: list[str],
    third_party: list[dict[str, str]] | None = None,
) -> int:
    """Scaffold a new topology directory inside an existing gitops repo.

    Args:
        gitops_root: Path to the root of the gitops repository.
        name:        Topology name.  Must match ``[a-z][a-z0-9-]{1,49}[a-z0-9]``.
        apps:        List of HyperI application chart names to include.
        third_party: Optional list of third-party chart specs.

    Returns:
        0 on success.

    Raises:
        GitopsInitError: For invalid name or if the topology directory already
            exists.

    """
    if not _TOPO_NAME_RE.match(name):
        raise GitopsInitError(
            f"topology name {name!r} must be lowercase RFC-1123-ish "
            f"(e.g. 'default', 'prod-au'); got {name!r}"
        )

    root = Path(gitops_root).resolve()
    topo_dir = root / "topologies" / name

    if topo_dir.exists():
        raise GitopsInitError(f"topology directory already exists: {topo_dir}")

    topo_dir.mkdir(parents=True)
    (topo_dir / "glue").mkdir()

    # topology.yaml
    import yaml as _yaml  # noqa: PLC0415 — deferred to avoid top-level dep at import

    topology_doc = {
        "apiVersion": "hyperi.io/v1",
        "kind": "DeploymentTopology",
        "metadata": {"name": name},
        "spec": {
            "umbrella": {
                "name": f"hyperi-deployment-{name}",
                "description": f"{name} HyperI deployment",
                "appVersion": "1.0",
            },
            "apps": [{"name": app, "version": "^1.0"} for app in apps],
            "thirdParty": third_party or [],
            "glue": [],
            "argocd": {
                "appOfApps": True,
                "appProject": "default",
                "syncWaves": {app: 0 for app in apps},
            },
        },
    }
    (topo_dir / "topology.yaml").write_text(
        _yaml.safe_dump(topology_doc, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
        newline="\n",
    )

    # values.yaml
    values_doc: dict[str, object] = {app: {"enabled": True} for app in apps}
    for tp in third_party or []:
        alias = tp.get("alias") or tp["name"]
        values_doc[alias] = {"enabled": True}
    (topo_dir / "values.yaml").write_text(
        _yaml.safe_dump(values_doc, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
        newline="\n",
    )

    # README.md
    apps_list = "\n".join(f"- `{a}`" for a in apps)
    readme = (
        f"# Topology: {name}\n\n"
        "Describe what this topology does and when to use it.\n\n"
        "## Apps\n\n"
        f"{apps_list}\n"
    )
    (topo_dir / "README.md").write_text(readme, encoding="utf-8", newline="\n")

    if not apps:
        warn(f"topology {name!r} has no apps — edit topology.yaml to add some")

    success(f"Scaffolded topology at {topo_dir}")
    return 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _walk_templates(root) -> list:
    """Recursively yield all file entries under *root* (importlib Traversable)."""
    out = []
    for entry in root.iterdir():
        if entry.is_dir():
            out.extend(_walk_templates(entry))
        else:
            out.append(entry)
    return out
