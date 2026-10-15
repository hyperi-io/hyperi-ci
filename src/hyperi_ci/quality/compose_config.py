# Project:   HyperI CI
# File:      src/hyperi_ci/quality/compose_config.py
# Purpose:   `docker compose config` resolution check (GATE, Path C)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Compose resolution gate: ``docker compose config`` over each compose file.

It needs no daemon and no registry. Each ``${VAR:?message}`` key the
environment leaves unset gets a placeholder, so the check runs on CI and a
local run with real values checks those. Pins are
:mod:`hyperi_ci.quality.compose_pins`' job. A file that fails alone and is
named like an overlay (``docker-compose.<x>.yml``) is retried on top of its
base, see :func:`overlay_stack`. A file whose services carry neither ``image``
nor ``build`` is a fragment that compose rejects standalone, so it is named in
the log and skipped.
"""

import os
import re
import shutil
from pathlib import Path

from hyperi_ci.common import error, info, run_cmd, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_tool_mode
from hyperi_ci.quality import findings as fdg
from hyperi_ci.quality.targets import compose_document, first_file
from hyperi_ci.tools import missing_tool

# `${NAME:?message}` / `${NAME?message}` - the keys the file declares mandatory.
_MANDATORY = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*):?\?[^}]*\}")

# docker compose prints variable notices as `time=... level=warning msg=...`.
_NOTICE_LINE = re.compile(r"\blevel=(warning|info|debug)\b")

_COMPOSE_STEMS = ("docker-compose", "compose")
_COMPOSE_SUFFIXES = (".yml", ".yaml")

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


def placeholder_env(path: Path, *layers: Path) -> dict[str, str]:
    """Return placeholders for the mandatory keys the environment leaves empty.

    Keys come from ``path`` and any further ``layers``. ``run_cmd`` lays these
    over ``os.environ``, so a key set there is left out rather than overwritten.
    """
    root = str(path.parent.resolve())
    names = {name for p in (path, *layers) for name in mandatory_keys(p)}
    return {
        name: root if name.endswith(_PATH_KEY_SUFFIXES) else _PLACEHOLDER
        for name in sorted(names)
        if not os.environ.get(name)
    }


def _stem_and_suffix(name: str) -> tuple[str, str] | None:
    """Split ``docker-compose.dfe.dev.yml`` into (``docker-compose.dfe.dev``, ``.yml``)."""
    for suffix in _COMPOSE_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)], suffix
    return None


def overlay_stack(path: Path) -> list[Path]:
    """Return the files ``path`` resolves with, or ``[]`` when it stands alone.

    ``docker-compose.<x>.yml`` and ``compose.<x>.yml`` (``.yaml`` too) overlay the
    ``docker-compose.yml`` or ``compose.yml`` in the same directory. The stack is
    that base, then the file named by the overlay's last dot-segment when it
    exists (``docker-compose.dev.yml`` for ``docker-compose.dfe.dev.yml``), then
    ``path``. An overlay with no base beside it stands alone.
    """
    split = _stem_and_suffix(path.name)
    if split is None:
        return []
    stem, _suffix = split
    for base_stem in _COMPOSE_STEMS:
        if not stem.startswith(f"{base_stem}."):
            continue
        base = first_file(
            path.parent, [f"{base_stem}{suffix}" for suffix in _COMPOSE_SUFFIXES]
        )
        if base is None:
            return []
        parts = stem[len(base_stem) + 1 :].split(".")
        layers = [base]
        if len(parts) > 1:
            middle = first_file(
                path.parent, [f"{base_stem}.{parts[-1]}{s}" for s in _COMPOSE_SUFFIXES]
            )
            if middle is not None:
                layers.append(middle)
        return [*layers, path]
    return []


def error_detail(output: str, returncode: int) -> str:
    """Return the line that says why compose failed, not a variable notice.

    Notice lines (``level=warning``) are dropped and the last remaining line
    wins, since compose prints the error after its notices.
    """
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    errors = [line for line in lines if not _NOTICE_LINE.search(line)]
    if errors:
        return errors[-1]
    return lines[-1] if lines else f"docker compose exited {returncode}"


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
    """Resolve ``path`` alone; if it fails and is an overlay, retry it with its stack.

    A file that resolves alone is never merged with its base. The stacked result
    replaces the standalone one, see :func:`overlay_stack`.
    """
    alone = _resolve([path], timeout)
    if alone is None:
        return None
    stack = overlay_stack(path)
    return _resolve(stack, timeout) if stack else alone


def _resolve(layers: list[Path], timeout: float | None) -> fdg.Finding | None:
    """Run ``docker compose config -q`` over ``layers``; return a finding on failure."""
    path = layers[-1]
    files = [arg for layer in layers for arg in ("-f", layer.name)]
    result = fdg.run_tool(
        ["docker", "compose", *files, "config", "-q"],
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
        env=placeholder_env(*layers),
    )
    if isinstance(result, fdg.Finding):
        return result
    if result.returncode == 0:
        return None
    return fdg.Finding(
        tool="compose-config",
        path=str(path),
        line=None,
        level="error",
        rule="compose/unresolvable",
        message=error_detail(result.stderr or result.stdout, result.returncode),
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
