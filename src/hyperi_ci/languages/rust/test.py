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

The test tier is separate from ``test.rust.tier``: ``core`` runs what the
project selects by default, ``full`` adds ``#[ignore]`` tests and, under
nextest, the tests the profile's ``default-filter`` leaves out. Every run
reports its run and skipped counts as a ``test tier`` notice.

Where the root Cargo.toml is both a package and a workspace with no
``default-members``, every command takes ``--workspace``: cargo would
otherwise test the root package alone.
"""

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from hyperi_ci.common import (
    echo_chunk,
    error,
    escape_command_data,
    info,
    is_ci,
    normalise_tristate,
    stream_cmd,
    strip_ansi,
    success,
    warn,
)
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.rust._manifest import is_root_package_workspace
from hyperi_ci.languages.tiering import (
    KeptLines,
    SuiteTier,
    announce_tier,
    handler_tier,
)

_RESULTS_DIR = Path("test-results")

# The full tier under nextest: run #[ignore] tests too, and drop the profile's
# default-filter. `cargo llvm-cov nextest` takes the same switches.
_NEXTEST_FULL_ARGS = ["--run-ignored", "all", "--ignore-default-filter"]

# The full tier under libtest (cargo test, tarpaulin, plain llvm-cov), after
# `--`. libtest has no default-filter.
_LIBTEST_FULL_ARG = "--include-ignored"

_FULL_FILTER_KEY = "test.full.rust.filter"
_FULL_SKIP_KEY = "test.full.rust.skip"


@dataclass(frozen=True, slots=True)
class FullSelection:
    """The tests the full tier leaves out, for a runner that cannot run them.

    Attributes:
        filterset: nextest filterset from ``test.full.rust.filter``, passed as
            ``-E``. libtest has no equivalent.
        skip: Test-name substrings from ``test.full.rust.skip``, passed as
            ``--skip`` after ``--``, which nextest and libtest both accept.

    """

    filterset: str = ""
    skip: tuple[str, ...] = ()

    def nextest_args(self) -> list[str]:
        """Return nextest's own full-tier switches, the filterset included."""
        args = list(_NEXTEST_FULL_ARGS)
        if self.filterset:
            args.extend(["-E", self.filterset])
        return args

    def harness_args(self, *, include_ignored: bool) -> list[str]:
        """Return the full-tier arguments that go after ``--``."""
        args = [_LIBTEST_FULL_ARG] if include_ignored else []
        for name in self.skip:
            args.extend(["--skip", name])
        return args


_NO_EXCLUSIONS = FullSelection()


def _filterset_unusable(harness: str) -> str:
    """Explain why a filterset cannot run under a libtest harness."""
    return (
        f"{_FULL_FILTER_KEY} is a nextest filterset, and this run uses {harness}, "
        f"which cannot apply it. The tests it excludes would run. Exclude them "
        f"with {_FULL_SKIP_KEY}, which both runners take, or run under nextest."
    )


def full_selection(config: CIConfig) -> FullSelection | None:
    """Read what the full tier leaves out, None when the keys are malformed.

    Args:
        config: Merged CI configuration.

    Returns:
        The selection, or None after logging why it is unusable.

    """
    filterset = config.get(_FULL_FILTER_KEY, "")
    skip = config.get(_FULL_SKIP_KEY, [])
    filterset = "" if filterset is None else filterset
    skip = [] if skip is None else skip
    if not isinstance(filterset, str):
        error(
            f"{_FULL_FILTER_KEY} must be a nextest filterset string, got {filterset!r}"
        )
        return None
    if not isinstance(skip, list) or not all(isinstance(s, str) and s for s in skip):
        error(f"{_FULL_SKIP_KEY} must be a list of test-name substrings, got {skip!r}")
        return None
    return FullSelection(filterset.strip(), tuple(skip))


# "Summary [   0.007s] 1 test run: 1 passed, 2 skipped", or "3/10 tests run"
# when fail-fast cancelled the rest.
_NEXTEST_SUMMARY = re.compile(r"^\s*Summary \[[^\]]*\]\s*(?P<body>.+)$")
_NEXTEST_RAN = re.compile(r"(\d+(?:/\d+)?) tests? run")
_NEXTEST_COUNT = re.compile(r"(\d+) (failed|skipped)\b")

# "test result: ok. 2 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out"
_LIBTEST_RESULT = re.compile(r"^test result: \w+\. (?P<body>.+)$")
_LIBTEST_COUNT = re.compile(r"(\d+) (passed|failed|ignored)\b")

# The lines tier_detail reads, kept while the run streams: libtest prints one
# result line per test binary, spread through the output.
_KEEP = re.compile(r"^\s*(Summary \[|test result: )")

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


def _scope_args(features: str, *, workspace: bool) -> list[str]:
    """Return the package and feature switches every test command shares.

    Args:
        features: ``all``, ``default`` or a feature list.
        workspace: Whether to pass ``--workspace``.

    Returns:
        The switches, ``--workspace`` first.

    """
    args = ["--workspace"] if workspace else []
    if features == "all":
        args.append("--all-features")
    elif features != "default":
        args.extend(["--features", features])
    return args


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
    features: str,
    rust_tier: str | None = None,
    *,
    runner: str = "cargo",
    test_tier: SuiteTier = SuiteTier.CORE,
    selection: FullSelection = _NO_EXCLUSIONS,
    workspace: bool = False,
) -> list[str]:
    """Build the cargo test command for the resolved runner.

    Integration and e2e tests default to single-threaded execution to avoid
    port conflicts from parallel test processes binding the same addresses.

    Args:
        features: ``all``, ``default`` or a feature list.
        rust_tier: ``test.rust.tier`` subset (unit, integration, e2e), or None.
        runner: ``nextest`` or ``cargo``.
        test_tier: The test tier; ``full`` adds the ignored tests.
        selection: What full leaves out; ignored under ``core``.
        workspace: Pass ``--workspace``, for a root-package workspace.

    Returns:
        The command.

    """
    use_nextest = runner == "nextest"
    cmd = ["cargo"]
    harness_args: list[str] = []

    if use_nextest:
        cmd.append("nextest")
        cmd.append("run")
    else:
        cmd.append("test")

    cmd.extend(_scope_args(features, workspace=workspace))

    if rust_tier == "unit":
        cmd.append("--lib")
    elif rust_tier == "integration":
        cmd.extend(["--test", "*"])
    elif rust_tier == "e2e":
        cmd.extend(["--test", "e2e*"])

    # Limit integration/e2e tests to 1 thread to avoid port conflicts
    if rust_tier in ("integration", "e2e"):
        if use_nextest:
            cmd.extend(["--jobs", "1"])
        else:
            harness_args.append("--test-threads=1")

    if test_tier is SuiteTier.FULL:
        if use_nextest:
            cmd.extend(selection.nextest_args())
        harness_args.extend(selection.harness_args(include_ignored=not use_nextest))

    if harness_args:
        cmd.extend(["--", *harness_args])
    return cmd


def tier_detail(output: str) -> str:
    """Describe what a Rust test run ran and did not run, for the tier notice.

    Reads nextest's closing ``Summary`` line, else sums libtest's per-binary
    ``test result:`` lines.

    Args:
        output: The runner's combined output.

    Returns:
        E.g. ``"2425 run, 32 skipped"`` or ``"976 passed, 8 ignored"``.

    """
    lines = strip_ansi(output).splitlines()
    for line in reversed(lines):
        summary = _NEXTEST_SUMMARY.match(line)
        if summary is None:
            continue
        body = summary["body"]
        ran = _NEXTEST_RAN.search(body)
        counts = {kind: int(n) for n, kind in _NEXTEST_COUNT.findall(body)}
        parts = [f"{ran[1] if ran else 'unknown'} run"]
        if counts.get("failed"):
            parts.append(f"{counts['failed']} failed")
        parts.append(f"{counts.get('skipped', 0)} skipped")
        return ", ".join(parts)

    totals = {"passed": 0, "failed": 0, "ignored": 0}
    results = [m for line in lines if (m := _LIBTEST_RESULT.match(line.strip()))]
    if not results:
        return "no test summary in the output, counts unknown"
    for result in results:
        for n, kind in _LIBTEST_COUNT.findall(result["body"]):
            totals[kind] += int(n)
    parts = [f"{totals['passed']} passed"]
    if totals["failed"]:
        parts.append(f"{totals['failed']} failed")
    parts.append(f"{totals['ignored']} ignored")
    return ", ".join(parts)


def _run_tests(cmd: list[str], test_tier: SuiteTier, what: str = "") -> int:
    """Run one test command, passing its output through, and announce it.

    Args:
        cmd: The test command.
        test_tier: The tier it ran under.
        what: Prefix for the notice, e.g. the feature set.

    Returns:
        The command's exit code.

    """
    kept = KeptLines(_KEEP)
    rc, _tail = stream_cmd(cmd, on_line=kept, on_chunk=echo_chunk)
    prefix = f"{what}: " if what else ""
    announce_tier(test_tier, f"{prefix}{tier_detail(kept.text())}")
    return rc


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


def _run_coverage(
    features: str,
    *,
    runner: str = "cargo",
    test_tier: SuiteTier = SuiteTier.CORE,
    selection: FullSelection = _NO_EXCLUSIONS,
    what: str = "",
    workspace: bool = False,
) -> int:
    """Run tests with coverage using tarpaulin or llvm-cov.

    Both tools spell the all-members switch ``--workspace``, as cargo does.

    Returns exit code (0 = success), or -1 when neither tool is installed.
    """
    _RESULTS_DIR.mkdir(exist_ok=True)
    full = test_tier is SuiteTier.FULL

    if shutil.which("cargo-tarpaulin"):
        if full and selection.filterset:
            error(_filterset_unusable("cargo-tarpaulin"))
            return 1
        cmd = [
            "cargo",
            "tarpaulin",
            "--out",
            "Lcov",
            "--out",
            "Html",
            "--output-dir",
            str(_RESULTS_DIR),
            *_scope_args(features, workspace=workspace),
        ]
        if full:
            cmd.extend(["--", *selection.harness_args(include_ignored=True)])

        info("  Running tests with cargo-tarpaulin for coverage...")
        _note_coverage_runner(runner, "cargo-tarpaulin")
        rc = _run_tests(cmd, test_tier, what)
        if rc != 0:
            error("Rust coverage tests failed")
            return rc
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
        cmd.extend(_scope_args(features, workspace=workspace))
        if full and runner == "nextest":
            cmd.extend(selection.nextest_args())
            if harness := selection.harness_args(include_ignored=False):
                cmd.extend(["--", *harness])
        elif full:
            cmd.extend(["--", *selection.harness_args(include_ignored=True)])

        info("  Running tests with cargo-llvm-cov for coverage...")
        _note_coverage_runner(runner, "cargo-llvm-cov")
        rc = _run_tests(cmd, test_tier, what)
        if rc != 0:
            error("Rust coverage tests failed")
            return rc

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
        extra_env: ``RUST_FEATURES`` and ``TEST_TIER`` from the stage.

    Returns:
        Exit code (0 = success).

    """
    info("Running Rust tests...")
    runner = _resolve_runner(config)
    if runner is None:
        return 1
    test_tier = handler_tier(extra_env)
    selection = _NO_EXCLUSIONS
    if test_tier is SuiteTier.FULL:
        info("  Full tier: #[ignore] tests run, and nextest's default-filter is off")
        resolved = full_selection(config)
        if resolved is None:
            return 1
        if resolved.filterset and runner != "nextest":
            error(_filterset_unusable("cargo test"))
            return 1
        selection = resolved
    workspace = is_root_package_workspace()
    if workspace:
        info("  Root package is also a workspace: --workspace, so every member runs")
    features = (extra_env or {}).get("RUST_FEATURES", "all")
    rust_tier = config.get("test.rust.tier", "all")
    feature_sets = _split_feature_sets(features)

    for feature_set in feature_sets:
        label = f" ({feature_set})" if len(feature_sets) > 1 else ""
        what = f"features {feature_set}" if len(feature_sets) > 1 else ""

        # Try coverage first if enabled (only for first feature set)
        if config.get("test.coverage", True) and rust_tier == "all":
            rc = _run_coverage(
                feature_set,
                runner=runner,
                test_tier=test_tier,
                selection=selection,
                what=what,
                workspace=workspace,
            )
            if rc >= 0:
                if rc == 0:
                    success(f"Rust tests passed{label} (with coverage)")
                else:
                    return rc
                continue

        # Standard test execution (no coverage tool, or a test.rust.tier subset)
        if rust_tier == "all":
            cmd = _build_test_cmd(
                feature_set,
                runner=runner,
                test_tier=test_tier,
                selection=selection,
                workspace=workspace,
            )
            rc = _run_tests(cmd, test_tier, what)
            if rc != 0:
                error(f"Rust tests failed{label}")
                return rc
            success(f"Rust tests passed{label}")
            continue

        # test.rust.tier subset
        for kind in ("unit", "integration", "e2e"):
            if rust_tier != kind:
                continue
            cmd = _build_test_cmd(
                feature_set,
                rust_tier=kind,
                runner=runner,
                test_tier=test_tier,
                selection=selection,
                workspace=workspace,
            )
            info(f"  Running {kind} tests{label}...")
            rc = _run_tests(cmd, test_tier, f"{what} {kind}".strip())
            if rc != 0:
                error(f"  {kind} tests failed{label}")
                return rc
            success(f"  {kind} tests passed{label}")

    return 0
