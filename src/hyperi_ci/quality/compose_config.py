# Project:   HyperI CI
# File:      src/hyperi_ci/quality/compose_config.py
# Purpose:   `docker compose config` resolution check (GATE, Path C)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Compose resolution check - the compose structural GATE.

``docker compose config`` interpolates every variable, merges every extension
and fails on anything structurally wrong. It needs no daemon and no registry
access, which is what makes it usable as a gate: the check is about the FILE,
not about the stack running.

**Hard-fail keys.** A stack that pins its images through ``${VAR:?message}``
aborts the moment one is unset, which on CI is all of them - so this discovers
those keys from the file itself and supplies a placeholder for each. A real
value already in the environment always wins, so a local run with a pinned
``.env`` validates the real pins. Placeholder injection is what keeps the check
hermetic; the PIN side is :mod:`hyperi_ci.quality.compose_pins`, which is why
handing compose an invented value costs nothing here.

**Overlay fragments are skipped, loudly.** A file whose services carry neither
``image`` nor ``build`` is a patch applied on top of another file (compose
rejects it standalone with "has neither an image nor a build context"), and the
file set it belongs to is a repo convention this verb cannot know. It is named
in the log rather than silently dropped or falsely failed. ``compose-pins``
still reads it, because a fragment can override an image.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from hyperi_ci.common import error, info, is_ci, run_cmd, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_cross_tool_mode
from hyperi_ci.quality import findings as fdg
from hyperi_ci.quality.targets import compose_document
from hyperi_ci.tools import missing_tool_notice

# `${NAME:?message}` / `${NAME?message}` - the keys the file declares mandatory.
_MANDATORY = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*):?\?[^}]*\}")

# Enough to satisfy interpolation and still parse as an image reference.
_PLACEHOLDER = "0.0.0-hyperi-ci-compose-check"

# A key with one of these suffixes lands in a bind-mount source, where compose
# reads a relative-looking string as a NAMED VOLUME and fails - so its
# placeholder has to be an absolute path.
_PATH_KEY_SUFFIXES = ("_ROOT", "_DIR", "_PATH")


def mandatory_keys(path: Path) -> list[str]:
    """Return every ``${VAR:?}`` key the compose file at ``path`` declares."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return sorted({m.group("name") for m in _MANDATORY.finditer(text)})


def placeholder_env(path: Path) -> dict[str, str]:
    """Return the placeholder values that let ``path`` resolve hermetically."""
    root = str(path.parent.resolve())
    return {
        name: root if name.endswith(_PATH_KEY_SUFFIXES) else _PLACEHOLDER
        for name in mandatory_keys(path)
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


def _validate(path: Path) -> fdg.Finding | None:
    """Run ``docker compose config -q`` over one file; return a finding on failure."""
    try:
        result = run_cmd(
            ["docker", "compose", "-f", path.name, "config", "-q"],
            check=False,
            capture=True,
            cwd=path.parent,
            env=placeholder_env(path),
        )
    except OSError as exc:
        return fdg.Finding(
            tool="compose-config",
            path=str(path),
            line=None,
            level="error",
            rule="compose/unrunnable",
            message=f"`docker compose config` could not be run ({exc})",
        )
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
) -> int:
    """Resolve every standalone compose file in ``files``. Returns exit code.

    0 = every stack resolved / disabled / nothing to resolve; 1 = a blocking gate
    hit a file that does not resolve, or docker compose is missing in CI.
    """
    mode = resolve_cross_tool_mode(config, "compose_config", "blocking")
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
        notice = missing_tool_notice("docker compose")
        if mode == "blocking" and is_ci():
            error(notice)
            error("  compose-config: the gate could not run - failing rather than pass")
            return 1
        warn(notice)
        return 0

    info(f"  compose-config: resolving {len(stacks)} compose file(s)...")
    found = [f for f in (_validate(p) for p in stacks) if f is not None]

    dropped = fdg.surface("compose-config", found, sarif_path=sarif_path)
    if dropped:
        info(f"  compose-config: +{dropped} more finding(s) in the job summary")

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
