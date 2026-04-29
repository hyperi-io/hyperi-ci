# Project:   HyperI CI
# File:      src/hyperi_ci/languages/typescript/quality.py
# Purpose:   TypeScript quality checks (eslint, prettier, tsc, audit)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""TypeScript quality checks handler.

Orchestrates: eslint, prettier, tsc, npm audit, semgrep.
Each tool's mode is configurable via .hyperi-ci.yaml quality.typescript section.

Note: TypeScript eslint is invoked via npm scripts, which use the project's
eslint config. Test relaxation (test_ignore) is applied at the eslint config
level (overrides section), not at the hyperi-ci invocation level. The
test_ignore config is available for projects using direct eslint invocation.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from hyperi_ci.common import error, info, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.typescript._common import (
    detect_package_manager,
    ensure_pm_available,
)

# Config-file markers that signal a tool is meant to run even without an
# npm script wrapper — we fall back to direct `npx <tool>` invocation.
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


_DEFAULT_TS_TEST_IGNORE = [
    "@typescript-eslint/no-explicit-any",
    "@typescript-eslint/no-non-null-assertion",
    "no-console",
]


def _find_npm_script(
    candidates: list[str],
    pm: str,
) -> str | None:
    """Find the first matching npm script from candidates.

    Args:
        candidates: Script names to try in order.
        pm: Package manager command.

    Returns:
        First matching script name, or None.

    """
    import json
    from pathlib import Path

    pkg = Path("package.json")
    if not pkg.exists():
        return None

    try:
        data = json.loads(pkg.read_text())
        scripts = data.get("scripts", {})
        for name in candidates:
            if name in scripts:
                return name
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def _get_tool_mode(tool: str, config: CIConfig) -> str:
    return str(config.get(f"quality.typescript.{tool}", "blocking"))


def _resolve_tool_cmd(cmd: list[str], use_uvx: bool = False) -> list[str]:
    """Resolve tool command, using uvx for standalone tools not on PATH."""
    if shutil.which(cmd[0]):
        return cmd
    if use_uvx and shutil.which("uvx"):
        return ["uvx", *cmd]
    return cmd


def _run_tool(
    tool_name: str,
    cmd: list[str],
    mode: str,
    use_uvx: bool = False,
) -> bool:
    if mode == "disabled":
        info(f"  {tool_name}: disabled")
        return True

    resolved = _resolve_tool_cmd(cmd, use_uvx=use_uvx)
    if use_uvx and resolved == cmd and not shutil.which(cmd[0]):
        if mode == "blocking":
            error(f"  {tool_name}: not installed (required)")
            return False
        warn(f"  {tool_name}: not installed (skipping)")
        return True

    result = subprocess.run(resolved, capture_output=True, text=True)
    if result.returncode == 0:
        success(f"  {tool_name}: passed")
        return True

    if mode == "warn":
        warn(f"  {tool_name}: issues found (non-blocking)")
        if result.stdout:
            print(result.stdout)
        return True

    error(f"  {tool_name}: failed")
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr)
    return False


def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Run TypeScript/JavaScript quality checks.

    Tool resolution order for eslint/prettier/tsc:
      1. If an npm script with the canonical name exists, run it.
      2. Else if a config file for the tool exists, fall back to a
         direct `npx <tool>` invocation.
      3. Else skip the tool with a warn-level message.

    Skip messages are `warn` (not `info`) so missing tool coverage is
    visible in CI output — silently degrading a lint step is a worse
    failure mode than a hard error. Projects that genuinely don't want
    a tool can set `quality.typescript.<tool>: disabled`.
    """
    info("Running TypeScript quality checks...")
    pm = detect_package_manager()
    if not ensure_pm_available(pm):
        error(f"{pm} is not available and could not be installed")
        return 1
    had_failure = False

    # --- eslint ---
    # Prefer the project's `lint` script (respects its own config).
    # Fall back to `npx eslint .` if an eslint config file is present.
    # Skip with a warning if neither — don't silently drop coverage.
    mode = _get_tool_mode("eslint", config)
    if _find_npm_script(["lint"], pm):
        if not _run_tool("eslint", [pm, "run", "lint"], mode):
            had_failure = True
    elif _has_any(_ESLINT_CONFIG_MARKERS):
        if not _run_tool("eslint", ["npx", "eslint", "."], mode):
            had_failure = True
    else:
        warn("  eslint: no 'lint' script and no eslint config — skipping")

    # --- prettier ---
    # `npm run format --check` is unsafe: many projects define `format`
    # as `prettier --write .` and the `--check` arg may not propagate.
    # Prefer explicit check-variant scripts; fall back to direct
    # invocation with `--check` which is unambiguous.
    mode = _get_tool_mode("prettier", config)
    format_check_script = _find_npm_script(
        ["format:check", "check-format", "check:format"], pm
    )
    if format_check_script:
        if not _run_tool("prettier", [pm, "run", format_check_script], mode):
            had_failure = True
    elif _has_any(_PRETTIER_CONFIG_MARKERS):
        if not _run_tool("prettier", ["npx", "prettier", "--check", "."], mode):
            had_failure = True
    else:
        warn("  prettier: no 'format:check' script and no prettier config — skipping")

    # --- tsc ---
    # Try typecheck script, else fall back to `npx tsc --noEmit` only if
    # a tsconfig.json exists. Skipping without tsconfig avoids tsc
    # crawling cwd with default settings (noisy / error-prone on pure-JS
    # projects that pass detection via the javascript→typescript alias).
    mode = _get_tool_mode("tsc", config)
    tsc_script = _find_npm_script(["typecheck", "check-types"], pm)
    if tsc_script:
        if not _run_tool("tsc", [pm, "run", tsc_script], mode):
            had_failure = True
    elif Path("tsconfig.json").exists():
        if not _run_tool("tsc", ["npx", "tsc", "--noEmit"], mode):
            had_failure = True
    else:
        warn("  tsc: no typecheck script and no tsconfig.json — skipping")

    # --- audit + semgrep — run on any JS/TS project; orthogonal to npm scripts ---
    mode = _get_tool_mode("audit", config)
    audit_level = config.get("quality.typescript.audit_level", "moderate")
    if not _run_tool("audit", [pm, "audit", f"--audit-level={audit_level}"], mode):
        had_failure = True

    mode = _get_tool_mode("semgrep", config)
    semgrep_cmd = ["semgrep", "scan", "--config", "auto", "--error", "--quiet"]
    if not _run_tool("semgrep", semgrep_cmd, mode, use_uvx=True):
        had_failure = True

    return 1 if had_failure else 0
