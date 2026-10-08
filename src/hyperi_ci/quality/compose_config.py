# Project:   HyperI CI
# File:      src/hyperi_ci/quality/compose_config.py
# Purpose:   `docker compose config` resolution check (GATE, Path C)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Compose resolution gate: ``docker compose config`` over each standalone file.

It needs no daemon and no registry. Each ``${VAR:?message}`` key the
environment leaves unset gets a placeholder, so the check runs on CI and a
local run with real values checks those. Pins are
:mod:`hyperi_ci.quality.compose_pins`' job. A file whose services
carry neither ``image`` nor ``build`` is an overlay fragment that compose
rejects standalone, so it is named in the log and skipped.
"""

import os
import re
import shutil
from pathlib import Path

from hyperi_ci.common import error, info, run_cmd, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_tool_mode
from hyperi_ci.quality import findings as fdg
from hyperi_ci.quality.targets import compose_document
from hyperi_ci.tools import missing_tool

# `${NAME:?message}` / `${NAME?message}` - the keys the file declares mandatory.
_MANDATORY = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*):?\?[^}]*\}")

# Enough to satisfy interpolation and still parse as an image reference.
_PLACEHOLDER = "0.0.0-hyperi-ci-compose-check"

# These keys land in bind-mount sources, where a non-absolute value is read as
# a named volume.
_PATH_KEY_SUFFIXES = ("_ROOT", "_DIR", "_PATH")


def mandatory_keys(path: Path) -> list[str]:
    """Return every ``${VAR:?}`` key the compose file at ``path`` declares."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return sorted({m.group("name") for m in _MANDATORY.finditer(text)})


def placeholder_env(path: Path) -> dict[str, str]:
    """Return placeholders for the mandatory keys the environment leaves empty.

    ``run_cmd`` lays these over ``os.environ``, so a key set there is left out
    rather than overwritten.
    """
    root = str(path.parent.resolve())
    return {
        name: root if name.endswith(_PATH_KEY_SUFFIXES) else _PLACEHOLDER
        for name in mandatory_keys(path)
        if not os.environ.get(name)
    }


def is_fragment(path: Path) -> bool:
    """Report whether ``path`` is an overlay patch rather than a standalone stack."""
    doc = compose_document(path)
    if doc is None:
        return False
    services = doc.get("services") or {}
    return not any(
        isinstance(svc, dict) and ("image" in svc or "build" in svc)
        for svc in services.values()
    )


def compose_available() -> bool:
    """Report whether the docker CLI carries a working ``compose`` subcommand."""
    if shutil.which("docker") is None:
        return False
    try:
        return (
            run_cmd(
                ["docker", "compose", "version"], check=False, capture=True
            ).returncode
            == 0
        )
    except OSError:
        return False


def _validate(path: Path, timeout: float | None = None) -> fdg.Finding | None:
    """Run ``docker compose config -q`` over one file; return a finding on failure."""
    result = fdg.run_tool(
        ["docker", "compose", "-f", path.name, "config", "-q"],
        lambda kind, why: fdg.Finding(
            "compose-config",
            str(path),
            None,
            "error",
            f"compose/{kind}",
            f"`docker compose config`: {why}",
        ),
        timeout=timeout,
        cwd=path.parent,
        env=placeholder_env(path),
    )
    if isinstance(result, fdg.Finding):
        return result
    if result.returncode == 0:
        return None
    detail = (result.stderr or result.stdout).strip().splitlines()
    return fdg.Finding(
        tool="compose-config",
        path=str(path),
        line=None,
        level="error",
        rule="compose/unresolvable",
        message=detail[0] if detail else f"docker compose exited {result.returncode}",
    )


def run(
    files: list[Path],
    config: CIConfig,
    *,
    sarif_path: str | Path | None = None,
    timeout: float | None = None,
) -> int:
    """Resolve every standalone compose file in ``files``; return the exit code.

    Returns 1 when a blocking gate hits a file that does not resolve, or docker
    compose is missing in CI.
    """
    mode = resolve_tool_mode("compose_config", config, default="blocking")
    if mode == "disabled":
        info("  compose-config: disabled")
        return 0
    if not files:
        info("  compose-config: no compose files to resolve - skipping")
        return 0

    fragments = [p for p in files if is_fragment(p)]
    stacks = [p for p in files if p not in fragments]
    for path in fragments:
        info(
            f"  compose-config: {path} declares no image or build - an overlay "
            "fragment, resolved only as part of a file set this verb cannot name"
        )
    if not stacks:
        info("  compose-config: every compose file is an overlay fragment - skipping")
        return 0

    if not compose_available():
        if missing_tool("docker compose", mode):
            error("  compose-config: the gate could not run - failing rather than pass")
            return 1
        return 0

    info(f"  compose-config: resolving {len(stacks)} compose file(s)...")
    found = [f for f in (_validate(p, timeout) for p in stacks) if f is not None]

    fdg.report("compose-config", found, mode, sarif_path=sarif_path)

    if not found:
        success(f"  compose-config: all {len(stacks)} compose file(s) resolve")
        return 0
    if mode == "blocking":
        error(f"  compose-config: {len(found)} compose file(s) do not resolve")
        return 1
    warn(
        f"  compose-config: {len(found)} compose file(s) do not resolve (non-blocking)"
    )
    return 0
