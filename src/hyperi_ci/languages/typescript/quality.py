# Project:   HyperI CI
# File:      src/hyperi_ci/languages/typescript/quality.py
# Purpose:   TypeScript quality checks (lint, format, tsc, audit)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""TypeScript quality checks handler.

Runs the linter (eslint, oxlint), the formatter (prettier, Oxfmt), tsc, audit
and osv-scanner, each with a mode from ``quality.typescript`` in
.hyperi-ci.yaml. A ``lint`` or ``format:check`` script is reported under the
tools its body invokes, with Vite+'s ``vp lint``, ``vp fmt`` and ``vp check``
read as oxlint and Oxfmt. The linters use the project's own config, so test
relaxation belongs in its overrides: hyperi-ci has no ``test_ignore`` for
TypeScript.
"""

import re
from pathlib import Path

from hyperi_ci.common import error, info, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import (
    resolve_tool_mode,
    run_gate_tool,
    stricter,
)
from hyperi_ci.languages.typescript._common import (
    detect_package_manager,
    detect_yarn_version,
    ensure_pm_available,
    package_scripts,
    read_package_json,
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
# https://oxc.rs/docs/guide/usage/linter/config.html
_OXLINT_CONFIG_MARKERS = (
    ".oxlintrc.json",
    ".oxlintrc.jsonc",
    "oxlint.config.ts",
    "oxlint.config.mts",
)
# https://oxc.rs/docs/guide/usage/formatter/config.html
_OXFMT_CONFIG_MARKERS = (
    ".oxfmtrc.json",
    ".oxfmtrc.jsonc",
    "oxfmt.config.ts",
    "oxfmt.config.mts",
)

# Vite+ subcommands and the tools they forward to (https://viteplus.dev/guide/check).
_VP_SUBCOMMAND_TOOLS = {
    "lint": ("oxlint",),
    "fmt": ("oxfmt",),
    "format": ("oxfmt",),
    "check": ("oxlint", "oxfmt"),
}
_SCRIPT_TOOLS = ("eslint", "oxlint", "prettier", "oxfmt")
_SHELL_SEPARATORS = re.compile(r"[\s;&|()]+")


def _has_any(markers: tuple[str, ...]) -> bool:
    """Return True if any marker file exists in cwd."""
    return any(Path(m).exists() for m in markers)


def _find_npm_script(candidates: list[str]) -> str | None:
    """Return the first of ``candidates`` package.json defines as a script, or None."""
    scripts = package_scripts()
    return next((name for name in candidates if name in scripts), None)


def _dev_dependencies() -> dict[str, object]:
    """Return package.json's ``devDependencies``, empty when there are none."""
    deps = read_package_json().get("devDependencies")
    return deps if isinstance(deps, dict) else {}


def _script_tools(body: str) -> list[str]:
    """Name the lint and format tools a package script invokes, in order of first use.

    A word counts when it is the tool's own command, bare or as a path
    (``node_modules/.bin/oxlint``), so a config file or plugin named after a
    tool does not.

    Args:
        body: The script's command line from package.json.

    Returns:
        Tool names from ``eslint``, ``oxlint``, ``prettier`` and ``oxfmt``,
        empty when the script runs none of them.

    """
    words = [w for w in _SHELL_SEPARATORS.split(body) if w]
    found: list[str] = []
    for i, word in enumerate(words):
        command = word.rsplit("/", 1)[-1]
        if command == "vp" and i + 1 < len(words):
            tools = _VP_SUBCOMMAND_TOOLS.get(words[i + 1], ())
        elif command in _SCRIPT_TOOLS:
            tools = (command,)
        else:
            continue
        found.extend(tool for tool in tools if tool not in found)
    return found


def _strictest_mode(tools: list[str], config: CIConfig) -> str:
    """Return the hardest mode among ``tools``, as one command runs them all."""
    modes = [resolve_tool_mode(t, config, language="typescript") for t in tools]
    strictest = modes[0]
    for mode in modes[1:]:
        if stricter(mode, strictest):
            strictest = mode
    return strictest


def _run_check(
    config: CIConfig,
    *,
    pm: str,
    scripts: list[str],
    default_tool: str,
    fallbacks: list[tuple[str, list[str]]],
    skip_message: str,
) -> bool:
    """Run a lint or format check from its npm script, else its npx fallbacks.

    A script is reported under the tools its body invokes, joined with ``+``
    and gated at the strictest of their modes. A script running none that
    :func:`_script_tools` recognises keeps ``default_tool``.

    Args:
        config: The merged CI config.
        pm: Package manager running the script.
        scripts: Script names to look for, first match wins.
        default_tool: Label and mode key for an unrecognised script body.
        fallbacks: ``(tool, command)`` pairs run when no script exists.
        skip_message: Warning logged when there is neither.

    Returns:
        True if the pipeline should continue, False on a blocking failure.

    """
    script = _find_npm_script(scripts)
    if script:
        body = package_scripts().get(script)
        tools = _script_tools(body) if isinstance(body, str) else []
        tools = tools or [default_tool]
        mode = _strictest_mode(tools, config)
        return run_gate_tool("+".join(tools), [pm, "run", script], mode)
    if not fallbacks:
        warn(skip_message)
        return True
    passed = True
    for tool, cmd in fallbacks:
        mode = resolve_tool_mode(tool, config, language="typescript")
        if not run_gate_tool(tool, cmd, mode):
            passed = False
    return passed


def _lint_fallbacks(dev_deps: dict[str, object]) -> list[tuple[str, list[str]]]:
    """Return the linters to run through npx when there is no ``lint`` script."""
    fallbacks: list[tuple[str, list[str]]] = []
    if "vite-plus" in dev_deps:
        fallbacks.append(("oxlint", ["npx", "vp", "lint"]))
    elif "oxlint" in dev_deps or _has_any(_OXLINT_CONFIG_MARKERS):
        fallbacks.append(("oxlint", ["npx", "oxlint"]))
    if _has_any(_ESLINT_CONFIG_MARKERS):
        fallbacks.append(("eslint", ["npx", "eslint", "."]))
    return fallbacks


def _format_fallbacks(dev_deps: dict[str, object]) -> list[tuple[str, list[str]]]:
    """Return the format checks to run through npx when there is no check script."""
    fallbacks: list[tuple[str, list[str]]] = []
    if "vite-plus" in dev_deps:
        fallbacks.append(("oxfmt", ["npx", "vp", "fmt", "--check"]))
    elif "oxfmt" in dev_deps or _has_any(_OXFMT_CONFIG_MARKERS):
        fallbacks.append(("oxfmt", ["npx", "oxfmt", "--check"]))
    if _has_any(_PRETTIER_CONFIG_MARKERS):
        fallbacks.append(("prettier", ["npx", "prettier", "--check", "."]))
    return fallbacks


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

    The lint check, the format check and tsc each run their npm script if
    present, else `npx <tool>` when the tool has a config file or, for oxlint
    and Oxfmt, a devDependency, else are skipped with a warning so the lost
    coverage shows in CI output. Set `quality.typescript.<tool>: disabled` to
    drop a tool deliberately.
    """
    info("Running TypeScript quality checks...")
    pm = detect_package_manager()
    if not ensure_pm_available(pm):
        error(f"{pm} is not available and could not be installed")
        return 1
    ignores = load_ignores(config._raw)
    had_failure = False
    dev_deps = _dev_dependencies()

    if not _run_check(
        config,
        pm=pm,
        scripts=["lint"],
        default_tool="eslint",
        fallbacks=_lint_fallbacks(dev_deps),
        skip_message="  lint: no 'lint' script and no eslint or oxlint setup -- "
        "skipping",
    ):
        had_failure = True

    # `format --check` is unsafe, as `format` is often `prettier --write .` and
    # the flag may not reach it. Only check-variant scripts are used.
    if not _run_check(
        config,
        pm=pm,
        scripts=["format:check", "check-format", "check:format"],
        default_tool="prettier",
        fallbacks=_format_fallbacks(dev_deps),
        skip_message="  format: no 'format:check' script and no prettier or Oxfmt "
        "setup -- skipping",
    ):
        had_failure = True

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
    audit_level = config.setting("quality.typescript.audit_level")
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
