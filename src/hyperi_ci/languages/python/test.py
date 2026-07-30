# Project:   HyperI CI
# File:      src/hyperi_ci/languages/python/test.py
# Purpose:   Python test runner (pytest with tiered execution)
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Python test handler.

Runs pytest with optional tiered execution (unit -> integration -> e2e).
Supports coverage reporting and configurable test arguments.
"""

from __future__ import annotations

import shutil
import subprocess

from hyperi_ci.common import error, info, success, warn
from hyperi_ci.config import CIConfig


def _resolve_cmd(cmd: list[str]) -> list[str]:
    """Resolve command, preferring `uv run` for uv projects.

    System-PATH pytest lives outside the project venv and won't see
    project-local plugins (pytest-cov, pytest-xdist). When this is a
    uv project (uv.lock present), always go through `uv run` so the
    project's own pytest + plugins are used.
    """
    from pathlib import Path

    if shutil.which("uv") and Path("uv.lock").exists():
        return ["uv", "run", *cmd]
    if shutil.which(cmd[0]):
        return cmd
    if shutil.which("uv"):
        return ["uv", "run", *cmd]
    return cmd


# pytest's exit code for "collected nothing". Distinct from a test failure (1),
# which is what makes `test.fail_on_missing` implementable at all.
_PYTEST_NO_TESTS_COLLECTED = 5


def _run_pytest(args: list[str], tier_name: str | None = None) -> int:
    """Run pytest with given arguments.

    Returns exit code.
    """
    label = f" ({tier_name})" if tier_name else ""
    cmd = _resolve_cmd(["pytest"] + args)
    info(f"  Running pytest{label}: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    return result.returncode


def _absolve_empty_run(rc: int, config: CIConfig, *, label: str = "") -> int:
    """Map pytest's no-tests-collected exit to success unless configured fatal.

    A project with no tests yet is not a broken project, so
    ``test.fail_on_missing`` defaults to false. Only exit 5 is remapped: a real
    failure keeps its code.

    Args:
        rc: pytest's exit code.
        config: Merged CI configuration.
        label: Tier name for the message, when running tiered.

    Returns:
        0 when the run collected nothing and that is permitted, else ``rc``.

    """
    if rc != _PYTEST_NO_TESTS_COLLECTED:
        return rc
    where = f" in {label}" if label else ""
    if config.get("test.fail_on_missing", False):
        error(f"No tests found{where} and test.fail_on_missing is set")
        return rc
    warn(f"No tests found{where} — allowed by test.fail_on_missing: false")
    return 0


def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Run Python tests.

    Args:
        config: Merged CI configuration.
        extra_env: Additional environment variables (unused for Python).

    Returns:
        Exit code (0 = success).

    """
    if not shutil.which("pytest") and not shutil.which("uv"):
        error("pytest not installed")
        return 1

    info("Running Python tests...")

    base_args: list[str] = list(config.get("test.python.args", ["-v", "--tb=short"]))

    # Coverage
    if config.get("test.coverage", True):
        coverage_format = config.get("test.python.coverage_format", "xml")
        base_args.extend(["--cov=src", f"--cov-report={coverage_format}"])
        min_cov = config.get("test.min_coverage", 0)
        if min_cov and int(min_cov) > 0:
            base_args.append(f"--cov-fail-under={min_cov}")

    # Tiered execution
    if config.get("test.use_tiers", False):
        tiers = [
            ("unit", "tests/unit/"),
            ("integration", "tests/integration/"),
            ("e2e", "tests/e2e/"),
        ]
        for tier_name, tier_path in tiers:
            tier_config = config.get(f"test.tiers.{tier_name}", {})
            if not tier_config.get("enabled", tier_name != "e2e"):
                info(f"  {tier_name} tests: disabled")
                continue

            rc = _absolve_empty_run(
                _run_pytest(base_args + [tier_path], tier_name=tier_name),
                config,
                label=tier_name,
            )
            if rc != 0:
                if tier_config.get("fail_fast", True):
                    error(f"  {tier_name} tests failed — stopping pipeline")
                    return rc
                warn(f"  {tier_name} tests failed (non-blocking)")

        success("All test tiers complete")
        return 0

    # Single run (no tiers)
    rc = _absolve_empty_run(_run_pytest(base_args), config)
    if rc == 0:
        success("Tests passed")
    return rc
