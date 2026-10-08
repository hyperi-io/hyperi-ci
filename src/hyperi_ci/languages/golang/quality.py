# Project:   HyperI CI
# File:      src/hyperi_ci/languages/golang/quality.py
# Purpose:   Golang quality checks (gofmt, govet, golangci-lint, gosec)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Golang quality checks handler."""

from hyperi_ci.common import info, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import (
    get_test_ignore,
    resolve_tool_mode,
    run_gate_tool,
)
from hyperi_ci.quality.ignores import for_tool, load_ignores


def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Run Golang quality checks."""
    info("Running Golang quality checks...")
    ignores = load_ignores(config._raw)
    had_failure = False

    mode = resolve_tool_mode("gofmt", config, language="golang")
    if not run_gate_tool("gofmt", ["gofmt", "-l", "."], mode, output_is_finding=True):
        had_failure = True

    mode = resolve_tool_mode("govet", config, language="golang")
    if not run_gate_tool("go vet", ["go", "vet", "./..."], mode):
        had_failure = True

    # golangci-lint runs twice: production (strict), then tests (relaxed).
    mode = resolve_tool_mode("golangci_lint", config, language="golang")
    test_ignore = get_test_ignore("golang", config)
    gci_user_ignores = for_tool(ignores, "golangci-lint")
    gci_user_disable = [f"--disable={e.id}" for e in gci_user_ignores]

    # The pin is checked here only, so the test pass does not repeat the warning.
    if not run_gate_tool(
        "golangci-lint (src)",
        ["golangci-lint", "run", "--tests=false", "--timeout", "5m"] + gci_user_disable,
        mode,
        pinned="golangci-lint",
    ):
        had_failure = True

    if test_ignore:
        disable_flags = [f"--disable={linter}" for linter in test_ignore]
        if not run_gate_tool(
            "golangci-lint (tests)",
            ["golangci-lint", "run", "--timeout", "5m"]
            + disable_flags
            + gci_user_disable,
            mode,
        ):
            had_failure = True

    mode = resolve_tool_mode("gosec", config, language="golang")
    gosec_cmd = ["gosec", "-quiet", "-tests=false"]
    gosec_ignores = for_tool(ignores, "gosec")
    if gosec_ignores:
        gosec_cmd.extend(["-exclude", ",".join(e.id for e in gosec_ignores)])
    gosec_cmd.append("./...")
    if not run_gate_tool("gosec", gosec_cmd, mode, pinned="gosec"):
        had_failure = True

    # govulncheck has no ignore flag, so say why its quality.ignore entries do nothing.
    mode = resolve_tool_mode("govulncheck", config, language="golang")
    govuln_ignores = for_tool(ignores, "govulncheck")
    if govuln_ignores:
        warn(
            "  govulncheck: quality.ignore entries present but the tool has "
            "no CLI ignore flag. Use //vuln:ignore source annotations or run "
            "via warn mode."
        )
    if not run_gate_tool(
        "govulncheck", ["govulncheck", "./..."], mode, pinned="govulncheck"
    ):
        had_failure = True

    return 1 if had_failure else 0
