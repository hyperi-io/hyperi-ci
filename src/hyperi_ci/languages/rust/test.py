# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/test.py
# Purpose:   Rust test runner (cargo nextest with tiered execution)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Rust test handler.

Runs cargo nextest or cargo test with optional tiered execution, resolving
which one from ``test.rust.nextest`` and announcing the choice. Supports
coverage via cargo-tarpaulin or cargo-llvm-cov.
"""

import shutil
import subprocess
from pathlib import Path

from hyperi_ci.common import (
    error,
    escape_command_data,
    info,
    is_ci,
    normalise_tristate,
    success,
    warn,
)
from hyperi_ci.config import CIConfig

_RESULTS_DIR = Path("test-results")

# Tri-state gate for the runner, the house shape (`normalise_tristate`):
# `true` requires nextest, `false` pins cargo test, `auto` takes nextest iff it
# is installed.
_NEXTEST_KEY = "test.rust.nextest"

# Shared by every message that reports which runner was chosen: the swap changes
# test semantics, so it must never read as a formatting detail.
_DIVERGENCE = (
    "The two are not interchangeable: nextest isolates each test in its own "
    "process, cargo test shares one across threads, so process-global state "
    "(metrics recorders, OnceLock, env vars a test sets) behaves differently - "
    "and doctests run only under cargo test."
)


def _split_feature_sets(features: str) -> list[str]:
    """Split pipe-separated feature sets into individual sets."""
    if features in ("all", "default"):
        return [features]
    return [f.strip() for f in features.split("|") if f.strip()]


def _has_nextest() -> bool:
    """Check if cargo-nextest is installed."""
    return shutil.which("cargo-nextest") is not None


def _announce_degradation() -> None:
    """Surface an ``auto`` fall back to cargo test where it cannot be missed.

    A logger line lands inside a collapsed log group and is read by nobody, so
    this also emits a real GitHub ``::warning::`` annotation, which escapes the
    group into the run summary - the same reasoning as
    :func:`hyperi_ci.languages.quality_common.is_skipped`.
    """
    msg = (
        f"cargo-nextest not found - running `cargo test` instead. {_DIVERGENCE} "
        f"This run's result does not predict a nextest run's. Set "
        f"`{_NEXTEST_KEY}: true` to fail here rather than degrade, or `false` "
        f"to choose cargo test deliberately."
    )
    warn(f"  {msg}")
    if is_ci():
        print(
            f"::warning title=hyperi-ci test runner degraded::"
            f"{escape_command_data(msg)}",
            flush=True,
        )


def _resolve_runner(config: CIConfig) -> str | None:
    """Decide the test runner, and say which one out loud.

    Returns ``"nextest"`` or ``"cargo"``; ``None`` when the repo REQUIRES
    nextest and it is absent, which the caller turns into a failed stage rather
    than testing something else and calling it a pass (design principle 3, no
    silent skips).
    """
    gate = normalise_tristate(config.get(_NEXTEST_KEY, "auto"), key=_NEXTEST_KEY)

    if gate == "false":
        info(f"  Test runner: cargo test ({_NEXTEST_KEY}: false)")
        return "cargo"

    if _has_nextest():
        info("  Test runner: cargo nextest")
        return "nextest"

    if gate == "true":
        error(
            f"cargo-nextest not found, and `{_NEXTEST_KEY}: true` requires it. "
            f"{_DIVERGENCE}"
        )
        info(
            "  The ARC runner image bakes cargo-nextest in (bootstrap.yaml "
            "`rust.cargo_tools`); a hosted/free runner does not. Install it "
            "with `cargo binstall cargo-nextest`, run on ARC, or set "
            f"`{_NEXTEST_KEY}: auto` to accept the cargo-test fallback."
        )
        return None

    _announce_degradation()
    return "cargo"


def _build_test_cmd(
    features: str, tier: str | None = None, *, runner: str = "cargo"
) -> list[str]:
    """Build the cargo test command for the resolved runner.

    Integration and e2e tests default to single-threaded execution to avoid
    port conflicts from parallel test processes binding the same addresses.
    """
    use_nextest = runner == "nextest"
    cmd = ["cargo"]

    if use_nextest:
        cmd.append("nextest")
        cmd.append("run")
    else:
        cmd.append("test")

    if features == "all":
        cmd.append("--all-features")
    elif features != "default":
        cmd.extend(["--features", features])

    if tier == "unit":
        cmd.append("--lib")
    elif tier == "integration":
        cmd.extend(["--test", "*"])
    elif tier == "e2e":
        cmd.extend(["--test", "e2e*"])

    # Limit integration/e2e tests to 1 thread to avoid port conflicts
    if tier in ("integration", "e2e"):
        if use_nextest:
            cmd.extend(["--jobs", "1"])
        else:
            cmd.extend(["--", "--test-threads=1"])

    return cmd


def _note_coverage_runner(runner: str, tool: str) -> None:
    """Report when a coverage tool overrides the resolved runner.

    tarpaulin drives cargo's own test harness, so a repo that resolved to
    nextest does not get nextest here - the same divergence the fallback warns
    about, arriving by a different door. llvm-cov is exempt because
    `cargo llvm-cov nextest` composes the two and keeps the resolved runner.
    """
    if runner != "nextest" or tool == "cargo-llvm-cov":
        return
    warn(f"  {tool} drives cargo's test harness, not nextest. {_DIVERGENCE}")


def _run_coverage(features: str, *, runner: str = "cargo") -> int:
    """Run tests with coverage using tarpaulin or llvm-cov.

    Returns exit code (0 = success).
    """
    _RESULTS_DIR.mkdir(exist_ok=True)

    if shutil.which("cargo-tarpaulin"):
        cmd = [
            "cargo",
            "tarpaulin",
            "--out",
            "Lcov",
            "--out",
            "Html",
            "--output-dir",
            str(_RESULTS_DIR),
        ]
        if features == "all":
            cmd.append("--all-features")
        elif features != "default":
            cmd.extend(["--features", features])

        info("  Running tests with cargo-tarpaulin for coverage...")
        _note_coverage_runner(runner, "cargo-tarpaulin")
        result = subprocess.run(cmd)
        if result.returncode != 0:
            error("Rust coverage tests failed")
            return result.returncode
        info(f"  Coverage report: {_RESULTS_DIR}/tarpaulin-report.html")
        return 0

    if shutil.which("cargo-llvm-cov"):
        lcov_path = _RESULTS_DIR / "lcov.info"
        html_dir = _RESULTS_DIR / "coverage-html"
        cmd = ["cargo", "llvm-cov"]
        # `cargo llvm-cov nextest` keeps the resolved runner instead of
        # swapping it for cargo's harness, so a repo on nextest measures the
        # tests it actually ships. This composition is why llvm-cov was chosen
        # over tarpaulin, which cannot do it (issue #140).
        if runner == "nextest":
            cmd.append("nextest")
        cmd.extend(["--lcov", "--output-path", str(lcov_path)])
        if features == "all":
            cmd.append("--all-features")
        elif features != "default":
            cmd.extend(["--features", features])

        info("  Running tests with cargo-llvm-cov for coverage...")
        _note_coverage_runner(runner, "cargo-llvm-cov")
        result = subprocess.run(cmd)
        if result.returncode != 0:
            error("Rust coverage tests failed")
            return result.returncode

        # Generate HTML report
        subprocess.run(
            [
                "cargo",
                "llvm-cov",
                "report",
                "--html",
                "--output-dir",
                str(html_dir),
            ],
            check=False,
        )
        info(f"  Coverage report: {html_dir}")
        return 0

    # `test.coverage` defaults to true, so a repo that never mentioned coverage
    # lands here too. Annotated rather than logged because no runner image
    # carries either tool, which makes this the path every Rust repo takes
    # (issue #140).
    missing = (
        "coverage was requested and did NOT run -- neither cargo-tarpaulin nor "
        "cargo-llvm-cov is installed, so the tests ran plain and there is no "
        "report. Install one, or set test.coverage: false to stop asking."
    )
    warn(f"  {missing}")
    if is_ci():
        print(f"::warning title=hyperi-ci coverage skipped::{missing}")
    return -1


def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Run Rust tests.

    Args:
        config: Merged CI configuration.
        extra_env: Additional env vars (RUST_FEATURES).

    Returns:
        Exit code (0 = success).

    """
    info("Running Rust tests...")
    runner = _resolve_runner(config)
    if runner is None:
        return 1
    features = (extra_env or {}).get("RUST_FEATURES", "all")
    tier = config.get("test.rust.tier", "all")
    feature_sets = _split_feature_sets(features)

    for feature_set in feature_sets:
        label = f" ({feature_set})" if len(feature_sets) > 1 else ""

        # Try coverage first if enabled (only for first feature set)
        if config.get("test.coverage", True) and tier == "all":
            rc = _run_coverage(feature_set, runner=runner)
            if rc >= 0:
                if rc == 0:
                    success(f"Rust tests passed{label} (with coverage)")
                else:
                    return rc
                continue

        # Standard test execution (no coverage tool, or tiered)
        if tier == "all":
            cmd = _build_test_cmd(feature_set, runner=runner)
            result = subprocess.run(cmd)
            if result.returncode != 0:
                error(f"Rust tests failed{label}")
                return result.returncode
            success(f"Rust tests passed{label}")
            continue

        # Tiered execution
        for t in ("unit", "integration", "e2e"):
            if tier != "all" and tier != t:
                continue
            cmd = _build_test_cmd(feature_set, tier=t, runner=runner)
            info(f"  Running {t} tests{label}...")
            result = subprocess.run(cmd)
            if result.returncode != 0:
                error(f"  {t} tests failed{label}")
                return result.returncode
            success(f"  {t} tests passed{label}")

    return 0
