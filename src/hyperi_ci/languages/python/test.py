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

Under ``full`` a skip fails the run unless its reason matches
``test.full.python.allow_skip``: a skip there is a test that could not run,
which the full tier exists to rule out.

In CI both tiers list the slowest tests with ``--durations``, unless the
project sets its own.
"""

import re
import shutil
from dataclasses import dataclass

from hyperi_ci.common import (
    echo_chunk,
    error,
    info,
    is_ci,
    stream_cmd,
    strip_ansi,
    success,
    warn,
)
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.python.parallel import parallel_args
from hyperi_ci.languages.python.pytest_args import option_values, project_args
from hyperi_ci.languages.tiering import (
    KeptLines,
    SuiteTier,
    announce_tier,
    handler_tier,
)

_FULL_MARKERS_KEY = "test.full.python.markers"
_ALLOW_SKIP_KEY = "test.full.python.allow_skip"

# How many of the slowest tests a CI run lists, so a core test that has grown
# slow is seen and can move to the full tier.
_CI_DURATIONS = 25


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

# pytest's exits that end with a summary: passed, tests failed, nothing
# collected. Interrupted, internal and usage errors leave nothing to check.
_COMPLETED_EXITS = (0, 1, _PYTEST_NO_TESTS_COLLECTED)

# pytest's report chars when a project sets none. ``-r`` is last-wins, so full
# appends ``s`` to the project's chars rather than passing it alone.
_DEFAULT_REPORT_CHARS = "fE"

_SHORT_SUMMARY = re.compile(r"^=+ short test summary info =+$")

# The default, folded form: "SKIPPED [2] tests/t.py:12: needs kafka". The line
# number is absent when a skip marker covers a whole module.
_FOLDED_SKIP = re.compile(r"^SKIPPED \[(\d+)\] (.+?(?::\d+)?): (.*)$")

# --no-fold-skipped: "SKIPPED tests/t.py::test_a - Skipped: needs kafka".
_UNFOLDED_SKIP = re.compile(r"^SKIPPED (\S.*?)(?: - (.*))?$")
_UNFOLDED_PREFIX = "Skipped: "

# Locations named per skip reason; any beyond these are counted, not named.
_MAX_LOCATIONS = 3

# The lines tier_detail and the skip check read, kept while pytest streams: the
# xdist header is at the start of the output, the skips and summary at the end.
_KEEP = re.compile(
    f"{_XDIST_HEADER.pattern}|{_SUMMARY_DURATION.pattern}"
    "|^SKIPPED |short test summary info"
)


@dataclass(frozen=True, slots=True)
class Skip:
    """One entry of pytest's short test summary for skipped tests.

    Attributes:
        count: Tests this entry stands for; pytest folds identical skips.
        where: ``path:line``, ``path``, or a node id when unfolded.
        reason: The skip reason, ``Skipped`` when none was given.

    """

    count: int
    where: str
    reason: str


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


def parse_skips(output: str) -> list[Skip]:
    """Read the skips out of pytest's short test summary (``-rs``).

    Args:
        output: pytest's combined output.

    Returns:
        One entry per summary line, in pytest's order; empty when the output
        has no short test summary section.

    """
    lines = strip_ansi(output).splitlines()
    starts = [i for i, line in enumerate(lines) if _SHORT_SUMMARY.match(line.strip())]
    if not starts:
        return []
    skips: list[Skip] = []
    for line in lines[starts[-1] + 1 :]:
        if folded := _FOLDED_SKIP.match(line):
            count, where, reason = folded.groups()
            skips.append(Skip(int(count), where, reason))
        elif unfolded := _UNFOLDED_SKIP.match(line):
            where, reason = unfolded.groups()
            reason = (reason or "").removeprefix(_UNFOLDED_PREFIX)
            skips.append(Skip(1, where, reason))
    return skips


def _locations(skips: list[Skip]) -> str:
    """Name where a reason's skips are, the first few of them."""
    named = ", ".join(skip.where for skip in skips[:_MAX_LOCATIONS])
    rest = len(skips) - _MAX_LOCATIONS
    return f"{named} and {rest} more" if rest > 0 else named


def check_skips(output: str, allow: list[re.Pattern[str]]) -> bool:
    """Report a full-tier run's skips, refusing any the allow list does not match.

    Args:
        output: pytest's combined output, run with ``s`` in its report chars.
        allow: ``test.full.python.allow_skip``, compiled.

    Returns:
        True when every skip's reason matches a pattern. False when one does
        not, and when the summary's skip count cannot be matched to listed
        reasons, because a skip that cannot be checked cannot pass.

    """
    counts = summary_counts(output)
    skips = parse_skips(output)
    listed = sum(skip.count for skip in skips)
    counted = None if counts is None else counts.get("skipped", 0)
    if counted != listed:
        seen = "no summary line" if counted is None else f"{counted} skipped"
        error(
            f"  Full tier: pytest reported {seen} and listed {listed} skip "
            f"reasons, so the skips could not be checked against {_ALLOW_SKIP_KEY}"
        )
        return False

    by_reason: dict[str, list[Skip]] = {}
    for skip in skips:
        by_reason.setdefault(skip.reason, []).append(skip)
    refused = False
    for reason, group in by_reason.items():
        total = sum(skip.count for skip in group)
        where = _locations(group)
        if any(pattern.search(reason) for pattern in allow):
            info(
                f"  Full tier: {total} skipped, allowed by {_ALLOW_SKIP_KEY}: "
                f"{reason} ({where})"
            )
        else:
            error(f"  Full tier: {total} skipped, not allowed: {reason} ({where})")
            refused = True
    if refused:
        error(
            "  Under the full tier a skip is a test that did not run. Fix what it "
            "needs, or add a regular expression matching its reason to "
            f"{_ALLOW_SKIP_KEY}"
        )
    return not refused


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
    args: list[str],
    test_tier: SuiteTier,
    dir_tier: str | None = None,
    allow_skip: list[re.Pattern[str]] | None = None,
) -> int:
    """Run pytest with given arguments and announce what it ran.

    Args:
        args: pytest arguments.
        test_tier: The test tier, for the notice.
        dir_tier: The ``test.use_tiers`` directory, when running split.
        allow_skip: The skip reasons full tolerates; None leaves skips alone.

    Returns:
        pytest's exit code, or 1 when it passed with a skip not allowed.

    """
    label = f" ({dir_tier})" if dir_tier else ""
    cmd = _resolve_cmd(["pytest"] + args)
    info(f"  Running pytest{label}: {' '.join(cmd)}")
    kept = KeptLines(_KEEP)
    rc, _tail = stream_cmd(cmd, on_line=kept, on_chunk=echo_chunk)
    output = kept.text()
    prefix = f"{dir_tier}: " if dir_tier else ""
    announce_tier(test_tier, f"{prefix}{tier_detail(output)}")
    if allow_skip is None or rc not in _COMPLETED_EXITS:
        return rc
    return rc if check_skips(output, allow_skip) else 1


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


def _allow_skip_patterns(config: CIConfig) -> list[re.Pattern[str]] | None:
    """Compile ``test.full.python.allow_skip``, None when misconfigured."""
    raw = config.get(_ALLOW_SKIP_KEY, [])
    if raw is None:
        raw = []
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        error(f"{_ALLOW_SKIP_KEY} must be a list of regular expressions, got {raw!r}")
        return None
    patterns: list[re.Pattern[str]] = []
    for item in raw:
        try:
            patterns.append(re.compile(item))
        except re.error as exc:
            error(f"{_ALLOW_SKIP_KEY}: {item!r} is not a regular expression: {exc}")
            return None
    info(
        f"  Full tier: a skip fails the run unless {_ALLOW_SKIP_KEY} matches its "
        f"reason ({len(patterns)} patterns)"
    )
    return patterns


def _durations_args(args: list[str]) -> list[str]:
    """Return ``--durations`` for a CI run, empty locally or when the project sets it."""
    if not is_ci():
        return []
    if option_values(project_args(args), "--durations"):
        info("  Slowest tests: the project sets its own --durations, leaving it alone")
        return []
    return [f"--durations={_CI_DURATIONS}"]


def _report_chars_arg(args: list[str]) -> str:
    """Return ``-r`` with the project's report chars plus ``s``, for skip reasons."""
    given = option_values(project_args(args), "--report-chars", "-r")
    chars = given[-1] if given else _DEFAULT_REPORT_CHARS
    return f"-r{chars}s"


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
    base_args.extend(_durations_args(base_args))

    allow_skip: list[re.Pattern[str]] | None = None
    if test_tier is SuiteTier.FULL:
        full_args = _full_tier_args(config)
        allow_skip = _allow_skip_patterns(config)
        if full_args is None or allow_skip is None:
            return 1
        base_args.append(_report_chars_arg(base_args))
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
                _run_pytest(
                    base_args + [dir_path],
                    test_tier,
                    dir_tier=dir_tier,
                    allow_skip=allow_skip,
                ),
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
    rc = _absolve_empty_run(
        _run_pytest(base_args, test_tier, allow_skip=allow_skip), config
    )
    if rc == 0:
        success("Tests passed")
    return rc
