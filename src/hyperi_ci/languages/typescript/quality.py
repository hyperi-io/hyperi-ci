# Project:   HyperI CI
# File:      src/hyperi_ci/languages/typescript/quality.py
# Purpose:   TypeScript quality checks (eslint, prettier, tsc, audit)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""TypeScript quality checks handler.

Runs eslint, prettier, tsc, audit and osv-scanner, each with a mode from
``quality.typescript`` in .hyperi-ci.yaml. eslint uses the project's own config,
so test relaxation belongs in its overrides: hyperi-ci has no ``test_ignore``
for TypeScript.
"""

from pathlib import Path

from hyperi_ci.common import error, info, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_tool_mode, run_gate_tool
from hyperi_ci.languages.typescript._common import (
    detect_package_manager,
    detect_yarn_version,
    ensure_pm_available,
    package_scripts,
)
from hyperi_ci.quality import osv_scanner
from hyperi_ci.quality.ignores import for_tool, load_ignores

# The lockfile osv-scanner reads for each package manager.
_PM_LOCKFILE = {
    "npm": "package-lock.json",
    "pnpm": "pnpm-lock.yaml",
    "yarn": "yarn.lock",
}

# Config files that mean a tool is wanted without an npm script, so run it via npx.
_ESLINT_CONFIG_MARKERS = (
    "eslint.config.js",
    "eslint.config.mjs",
    "eslint.config.cjs",
    "eslint.config.ts",
    ".eslintrc",
    ".eslintrc.js",
    ".eslintrc.cjs",
    ".eslintrc.json",
    ".eslintrc.yaml",
    ".eslintrc.yml",
)
_PRETTIER_CONFIG_MARKERS = (
    ".prettierrc",
    ".prettierrc.js",
    ".prettierrc.cjs",
    ".prettierrc.mjs",
    ".prettierrc.json",
    ".prettierrc.yaml",
    ".prettierrc.yml",
    ".prettierrc.toml",
    "prettier.config.js",
    "prettier.config.cjs",
    "prettier.config.mjs",
)


def _has_any(markers: tuple[str, ...]) -> bool:
    """Return True if any marker file exists in cwd."""
    return any(Path(m).exists() for m in markers)


def _find_npm_script(candidates: list[str]) -> str | None:
    """Return the first of ``candidates`` package.json defines as a script, or None."""
    scripts = package_scripts()
    return next((name for name in candidates if name in scripts), None)


def _audit_command(*, audit_level: str, pm: str, yarn_major: int) -> list[str]:
    """Build the security-audit command for the package manager.

    Yarn Berry (v2+) replaced ``yarn audit`` with ``yarn npm audit --severity``,
    and the Classic form exits non-zero there. Yarn Classic, npm and pnpm take
    ``--audit-level``.

    Args:
        audit_level: Minimum severity to fail on (e.g. ``"moderate"``).
        pm: Package manager -- one of ``npm``, ``yarn``, ``pnpm``.
        yarn_major: Yarn major version; only consulted when ``pm`` is
            ``yarn``. ``>= 2`` selects the Berry command.

    Returns:
        The audit command as an argv list.

    """
    if pm == "yarn" and yarn_major >= 2:
        return ["yarn", "npm", "audit", "--severity", audit_level]
    if pm == "yarn":
        return ["yarn", "audit", f"--audit-level={audit_level}"]
    return [pm, "audit", f"--audit-level={audit_level}"]


def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Run TypeScript/JavaScript quality checks.

    Each of eslint, prettier and tsc runs its npm script if present, else
    `npx <tool>` when the tool has a config file, else is skipped with a
    warning so the lost coverage shows in CI output. Set
    `quality.typescript.<tool>: disabled` to drop a tool deliberately.
    """
    info("Running TypeScript quality checks...")
    pm = detect_package_manager()
    if not ensure_pm_available(pm):
        error(f"{pm} is not available and could not be installed")
        return 1
    ignores = load_ignores(config._raw)
    had_failure = False

    # --- eslint ---
    mode = resolve_tool_mode("eslint", config, language="typescript")
    if _find_npm_script(["lint"]):
        if not run_gate_tool("eslint", [pm, "run", "lint"], mode):
            had_failure = True
    elif _has_any(_ESLINT_CONFIG_MARKERS):
        if not run_gate_tool("eslint", ["npx", "eslint", "."], mode):
            had_failure = True
    else:
        warn("  eslint: no 'lint' script and no eslint config -- skipping")

    # --- prettier ---
    # `format --check` is unsafe, as `format` is often `prettier --write .` and
    # the flag may not reach it. Only check-variant scripts are used.
    mode = resolve_tool_mode("prettier", config, language="typescript")
    format_check_script = _find_npm_script(
        ["format:check", "check-format", "check:format"]
    )
    if format_check_script:
        if not run_gate_tool("prettier", [pm, "run", format_check_script], mode):
            had_failure = True
    elif _has_any(_PRETTIER_CONFIG_MARKERS):
        if not run_gate_tool("prettier", ["npx", "prettier", "--check", "."], mode):
            had_failure = True
    else:
        warn("  prettier: no 'format:check' script and no prettier config -- skipping")

    # --- tsc ---
    # Without a tsconfig.json tsc would crawl cwd on defaults, which is noisy on
    # pure-JS projects detected through the javascript->typescript alias.
    mode = resolve_tool_mode("tsc", config, language="typescript")
    tsc_script = _find_npm_script(["typecheck", "check-types"])
    if tsc_script:
        if not run_gate_tool("tsc", [pm, "run", tsc_script], mode):
            had_failure = True
    elif Path("tsconfig.json").exists():
        if not run_gate_tool("tsc", ["npx", "tsc", "--noEmit"], mode):
            had_failure = True
    else:
        warn("  tsc: no typecheck script and no tsconfig.json -- skipping")

    # --- audit: runs on any JS/TS project, independent of npm scripts ---
    mode = resolve_tool_mode("audit", config, language="typescript")
    audit_level = config.get("quality.typescript.audit_level", "moderate")
    yarn_major = detect_yarn_version() if pm == "yarn" else 0
    audit_cmd = _audit_command(audit_level=audit_level, pm=pm, yarn_major=yarn_major)
    audit_ignores = for_tool(ignores, f"{pm}-audit")
    if audit_ignores:
        if pm == "pnpm":
            for entry in audit_ignores:
                audit_cmd.extend(["--ignore-cve", entry.id])
        else:
            # npm/yarn audit have no CLI ignore flag, so the entries are not applied.
            warn(
                f"  {pm} audit: quality.ignore entries present for "
                f"{pm}-audit but the tool has no CLI ignore flag. Use "
                f"package.json overrides instead."
            )
    if not run_gate_tool("audit", audit_cmd, mode):
        had_failure = True

    # osv-scanner finds malicious packages (MAL-*). The audit commands read only
    # the GitHub Advisory DB's CVEs and miss the OSSF malicious-packages feed.
    mode = resolve_tool_mode("osv_scanner", config, language="typescript")
    osv_lockfile = Path(_PM_LOCKFILE.get(pm, "package-lock.json"))
    if not osv_scanner.run(osv_lockfile, for_tool(ignores, osv_scanner.SLUG), mode):
        had_failure = True

    return 1 if had_failure else 0
