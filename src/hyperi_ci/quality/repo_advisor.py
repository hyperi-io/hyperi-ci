# Project:   HyperI CI
# File:      src/hyperi_ci/quality/repo_advisor.py
# Purpose:   Optional, non-blocking repo-hygiene advisory via `alint`
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Optional, non-blocking repo-hygiene advisory via ``alint``.

Wires the external ``alint`` linter (asamarts/alint) as an ADVISORY step.
alint is profile-gated - it detects the ecosystem (python/rust/node/go/...)
and runs matching rulesets, surfacing recommendations at info/warning level
(missing ``.gitignore`` / ``.editorconfig``, tracked build artefacts, absent
lockfile, ...). We surface those WITHOUT ever failing the build.

Zero per-repo config. hyperi-ci ships an opinionated default
(``config/alint/hyperi.alint.yml``, just alint's own bundled baseline for our
four languages) and passes it via ``alint check -c <default>``, so the advisory
works with no file the developer has to add. A repo that DOES want to tune it
runs ``alint init`` to drop its own ``.alint.yml``; that wins and we step aside
(let alint discover it). Turn the whole thing off with ``quality.alint:
disabled``.

alint is NOT a hyperi-ci dependency. Missing -> the step skips via
:func:`hyperi_ci.tools.find_tool` (an info nudge under ``auto``, a louder warn
under ``enabled``). It never installs anything and never fails the build.

Config (``.hyperi-ci.yaml``):

    quality.alint: auto      # run if alint is installed, else info-skip (default)
    quality.alint: enabled   # run, warn (still non-fatal) if alint is missing
    quality.alint: disabled  # never run
"""

from __future__ import annotations

from pathlib import Path

from hyperi_ci.common import is_ci, run_cmd, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.tools import find_tool

# Shipped opinionated default (packaged under hyperi_ci/config/, so it travels
# in the wheel). config/ is a sibling of quality/ inside the package.
_DEFAULT_CONFIG = (
    Path(__file__).resolve().parents[1] / "config" / "alint" / "hyperi.alint.yml"
)


def run(config: CIConfig, project_dir: Path | None = None) -> int:
    """Run the alint advisory. ALWAYS returns 0 - it never gates a build.

    ``quality.alint`` selects the mode (auto / enabled / disabled). Findings
    stream straight to the log (``--format github`` in CI so they land as
    annotations, ``human`` locally).
    """
    mode = str(config.get("quality.alint", "auto")).strip().lower()
    if mode in ("disabled", "off", "false", "none"):
        return 0

    exe = find_tool("alint", recommended=(mode == "enabled"))
    if not exe:
        return 0

    root = project_dir or Path.cwd()
    cmd = [exe, "check", "--format", "github" if is_ci() else "human"]
    # A repo's own .alint.yml wins - let alint auto-discover it. Otherwise ship
    # the HyperI default explicitly so the advisory works with no per-repo file.
    if not (root / ".alint.yml").exists():
        cmd += ["-c", str(_DEFAULT_CONFIG)]

    try:
        result = run_cmd(cmd, check=False, cwd=root)
    except OSError as exc:
        # The binary resolved but could not be exec'd (removed between which()
        # and exec, broken symlink, no exec bit). Advisory - never fail.
        warn(f"alint could not be run ({exc}) - advisory only, not failing.")
        return 0
    # ADVISORY: alint exits 1 on error-level findings, 0 otherwise (warnings /
    # info never fail it). It is a recommendation surface here, not a gate, so
    # we never propagate a non-zero. Exit 2 (config) / 3 (internal) means alint
    # itself misbehaved - note it, still don't fail the build.
    if result.returncode >= 2:
        warn(
            f"alint exited {result.returncode} (config/internal issue) - "
            "advisory only, not failing the build."
        )
    return 0
