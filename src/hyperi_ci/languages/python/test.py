# Project:   HyperI CI
# File:      src/hyperi_ci/languages/python/test.py
# Purpose:   Python test runner (pytest with tiered execution)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Python test handler.

Runs pytest with optional tiered execution (unit -> integration -> e2e).
Supports coverage reporting, configurable test arguments, and a worker
count derived from the host when the project opts into parallelism.

The test tier decides the marker selection: ``core`` leaves the project's own
``addopts -m`` in force, ``full`` passes ``-m <test.full.python.markers>``,
which pytest applies in place of it. Every run reports its passed, skipped and
deselected counts as a ``test tier`` notice.
"""

import re
import shutil

from hyperi_ci.common import (
    echo_chunk,
    error,
    info,
    stream_cmd,
    strip_ansi,
    success,
    warn,
)
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.python.parallel import parallel_args
from hyperi_ci.languages.tiering import (
    KeptLines,
    SuiteTier,
    announce_tier,
    handler_tier,
)

_FULL_MARKERS_KEY = "test.full.python.markers"


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

# The closing line: "== 3 passed, 1 skipped, 2 deselected in 0.12s ==", with
# no rule under -q, or "no tests ran in 0.01s".
_SUMMARY_DURATION = re.compile(r"\bin \d+(?:\.\d+)?s\b")
_SUMMARY_COUNT = re.compile(
    r"(\d+) (passed|failed|skipped|deselected|xfailed|xpassed|errors?|warnings?)\b"
)

# pytest-xdist's header lines, -v and -q forms. Its controller never learns the
# deselected count, so the summary omits it under -n.
_XDIST_HEADER = re.compile(
    r"^(created: \d+/\d+ workers?|bringing up nodes|\d+ workers? \[)"
)

# The lines tier_detail reads, kept while pytest streams: the xdist header is
# at the start of the output and the summary at the end.
_KEEP = re.compile(f"{_XDIST_HEADER.pattern}|{_SUMMARY_DURATION.pattern}")


def summary_counts(output: str) -> dict[str, int] | None:
    """Read the counts off pytest's closing summary line.

    Args:
        output: pytest's combined output.

    Returns:
        Outcome to count, e.g. ``{"passed": 3, "deselected": 2}``, empty for
        "no tests ran", or None when no summary line is present.

    """
    for line in reversed(strip_ansi(output).splitlines()):
        if not _SUMMARY_DURATION.search(line):
            continue
        counts = {kind: int(n) for n, kind in _SUMMARY_COUNT.findall(line)}
        if counts or "no tests ran" in line:
            return counts
    return None


def tier_detail(output: str) -> str:
    """Describe what a pytest run ran and did not run, for the tier notice.

    Args:
        output: pytest's combined output.

    Returns:
        E.g. ``"7892 passed, 17 skipped, 281 deselected"``.

    """
    counts = summary_counts(output)
    if counts is None:
        return "no pytest summary line in the output, counts unknown"
    parts = [f"{counts.get('passed', 0)} passed"]
    if counts.get("failed"):
        parts.append(f"{counts['failed']} failed")
    parts.append(f"{counts.get('skipped', 0)} skipped")
    lines = strip_ansi(output).splitlines()
    under_xdist = any(_XDIST_HEADER.match(line) for line in lines)
    if "deselected" in counts or not under_xdist:
        parts.append(f"{counts.get('deselected', 0)} deselected")
    else:
        parts.append("deselected count not reported under pytest-xdist")
    return ", ".join(parts)


def _run_pytest(
    args: list[str], test_tier: SuiteTier, dir_tier: str | None = None
) -> int:
    """Run pytest with given arguments and announce what it ran.

    Returns exit code.
    """
    label = f" ({dir_tier})" if dir_tier else ""
    cmd = _resolve_cmd(["pytest"] + args)
    info(f"  Running pytest{label}: {' '.join(cmd)}")
    kept = KeptLines(_KEEP)
    rc, _tail = stream_cmd(cmd, on_line=kept, on_chunk=echo_chunk)
    prefix = f"{dir_tier}: " if dir_tier else ""
    announce_tier(test_tier, f"{prefix}{tier_detail(kept.text())}")
    return rc


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
    warn(f"No tests found{where} - allowed by test.fail_on_missing: false")
    return 0


def _full_tier_args(config: CIConfig) -> list[str] | None:
    """Return the marker override for the full tier, None when misconfigured.

    pytest keeps the LAST ``-m``, and ``addopts`` is placed before the command
    line, so this replaces the project's own selection; an empty expression
    selects everything.
    """
    markers = config.get(_FULL_MARKERS_KEY, "")
    if markers is None:
        markers = ""
    if not isinstance(markers, str):
        error(
            f"{_FULL_MARKERS_KEY} must be a marker expression string, got {markers!r}"
        )
        return None
    shown = repr(markers) if markers else '"" (every test)'
    info(f"  Full tier: -m {shown} replaces the project's own marker selection")
    return ["-m", markers]


def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Run Python tests.

    Args:
        config: Merged CI configuration.
        extra_env: ``TEST_TIER`` from the stage; nothing else is read.

    Returns:
        Exit code (0 = success).

    """
    if not shutil.which("pytest") and not shutil.which("uv"):
        error("pytest not installed")
        return 1

    info("Running Python tests...")
    test_tier = handler_tier(extra_env)

    base_args: list[str] = list(config.get("test.python.args", ["-v", "--tb=short"]))

    # Coverage
    if config.get("test.coverage", True):
        coverage_format = config.get("test.python.coverage_format", "xml")
        base_args.extend(["--cov=src", f"--cov-report={coverage_format}"])
        min_cov = config.get("test.min_coverage", 0)
        if min_cov and int(min_cov) > 0:
            base_args.append(f"--cov-fail-under={min_cov}")

    # Worker data is combined by pytest-cov before it reports, so coverage
    # stays accurate across workers and needs no extra configuration.
    base_args.extend(parallel_args(config, base_args, _resolve_cmd(["pytest"])))

    if test_tier is SuiteTier.FULL:
        full_args = _full_tier_args(config)
        if full_args is None:
            return 1
        base_args.extend(full_args)

    # Directory split (test.use_tiers), independent of the test tier.
    if config.get("test.use_tiers", False):
        dir_tiers = [
            ("unit", "tests/unit/"),
            ("integration", "tests/integration/"),
            ("e2e", "tests/e2e/"),
        ]
        for dir_tier, dir_path in dir_tiers:
            dir_tier_config = config.get(f"test.tiers.{dir_tier}", {})
            if not dir_tier_config.get("enabled", dir_tier != "e2e"):
                info(f"  {dir_tier} tests: disabled")
                continue

            rc = _absolve_empty_run(
                _run_pytest(base_args + [dir_path], test_tier, dir_tier=dir_tier),
                config,
                label=dir_tier,
            )
            if rc != 0:
                if dir_tier_config.get("fail_fast", True):
                    error(f"  {dir_tier} tests failed - stopping pipeline")
                    return rc
                warn(f"  {dir_tier} tests failed (non-blocking)")

        success("All test tiers complete")
        return 0

    # Single run (no directory split)
    rc = _absolve_empty_run(_run_pytest(base_args, test_tier), config)
    if rc == 0:
        success("Tests passed")
    return rc
