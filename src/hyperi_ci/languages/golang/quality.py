# Project:   HyperI CI
# File:      src/hyperi_ci/languages/golang/quality.py
# Purpose:   Golang quality checks (gofmt, govet, golangci-lint, gosec)
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Golang quality checks handler."""

from __future__ import annotations

import shutil
import subprocess

from hyperi_ci.common import error, info, is_ci, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import get_test_ignore, resolve_tool_mode
from hyperi_ci.quality.ignores import for_tool, load_ignores

_DEFAULT_GO_TEST_IGNORE = ["errcheck", "gosec"]


def _get_tool_mode(tool: str, config: CIConfig) -> str:
    return resolve_tool_mode(tool, config, "golang")


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
    if resolved == cmd and not shutil.which(cmd[0]):
        # A missing tool fails the gate only in CI, where every tool MUST
        # be present -- a silent skip would mask a coverage gap. Locally it
        # is an environment gap, not a quality finding: warn and carry on
        # so `hyperi-ci check` still runs whatever IS installed (matches
        # the gitleaks stage's local-vs-CI handling).
        if mode == "blocking" and is_ci():
            error(f"  {tool_name}: not installed (required)")
            return False
        warn(f"  {tool_name}: not installed (skipping locally)")
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
    """Run Golang quality checks."""
    info("Running Golang quality checks...")
    ignores = load_ignores(config._raw)
    had_failure = False

    mode = _get_tool_mode("gofmt", config)
    if not _run_tool("gofmt", ["gofmt", "-l", "."], mode):
        had_failure = True

    mode = _get_tool_mode("govet", config)
    if not _run_tool("go vet", ["go", "vet", "./..."], mode):
        had_failure = True

    # golangci-lint — two-pass: production (strict) + test (relaxed)
    mode = _get_tool_mode("golangci_lint", config)
    test_ignore = get_test_ignore("golang", config, _DEFAULT_GO_TEST_IGNORE)
    gci_user_ignores = for_tool(ignores, "golangci-lint")
    gci_user_disable = [f"--disable={e.id}" for e in gci_user_ignores]

    # Production pass — skip test files
    if not _run_tool(
        "golangci-lint (src)",
        ["golangci-lint", "run", "--tests=false", "--timeout", "5m"] + gci_user_disable,
        mode,
    ):
        had_failure = True

    # Test pass — include tests, disable specific linters
    if test_ignore:
        disable_flags = [f"--disable={linter}" for linter in test_ignore]
        if not _run_tool(
            "golangci-lint (tests)",
            ["golangci-lint", "run", "--timeout", "5m"]
            + disable_flags
            + gci_user_disable,
            mode,
        ):
            had_failure = True

    mode = _get_tool_mode("gosec", config)
    gosec_cmd = ["gosec", "-quiet", "-tests=false"]
    gosec_ignores = for_tool(ignores, "gosec")
    if gosec_ignores:
        gosec_cmd.extend(["-exclude", ",".join(e.id for e in gosec_ignores)])
    gosec_cmd.append("./...")
    if not _run_tool("gosec", gosec_cmd, mode):
        had_failure = True

    # govulncheck has no native --ignore flag; emit a notice when entries
    # exist for it so operators understand why their config isn't applied.
    mode = _get_tool_mode("govulncheck", config)
    govuln_ignores = for_tool(ignores, "govulncheck")
    if govuln_ignores:
        warn(
            "  govulncheck: quality.ignore entries present but the tool has "
            "no CLI ignore flag. Use //vuln:ignore source annotations or run "
            "via warn mode."
        )
    if not _run_tool("govulncheck", ["govulncheck", "./..."], mode):
        had_failure = True

    return 1 if had_failure else 0
