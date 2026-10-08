# Project:   HyperI CI
# File:      src/hyperi_ci/languages/quality_common.py
# Purpose:   Shared quality-stage code: tool modes, the gate runner, test-path splits
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Shared quality-stage code for every language handler.

Resolves each tool's mode, runs each tool through :func:`run_gate_tool`, and
splits checks into two passes:
1. Production pass -- full strict rules on all code except test dirs
2. Test pass -- relaxed rules on test directories only

Test paths and ignore lists are configurable via defaults.yaml and
overridable per project in .hyperi-ci.yaml.
"""

import fnmatch
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from hyperi_ci.common import (
    URL_ATTEMPTS,
    announce,
    backoff,
    env_true,
    error,
    get_exclude_dirs,
    info,
    is_ci,
    run_cmd,
    success,
    warn,
)
from hyperi_ci.config import CIConfig, packaged_default
from hyperi_ci.tools import installed_version, warn_on_pin_drift

# Directory names never scanned as Python source, beside the handler's own
# excludes and every hidden directory.
_NOT_PYTHON_SOURCE = (
    "venv",
    "env",
    "build",
    "dist",
    "docs",
    "node_modules",
    "__pycache__",
    "*.egg-info",
)

# The only valid quality-tool modes. An out-of-vocabulary value is a typo, not a
# silent request to disable the gate (see resolve_tool_mode).
_VALID_MODES = {"blocking", "warn", "disabled"}

_MODE_STRENGTH = {"disabled": 0, "warn": 1, "blocking": 2}

# Lines a non-blocking tool shows inline before the rest is only counted.
WARN_OUTPUT_CAP = 25

# uv's stderr when the tool never started, which the missing-tool check cannot
# see because the rewritten command starts with `uv`.
_SPAWN_FAILURES = ("failed to spawn", "no interpreter found")

# clap, argparse and getopt refusing a flag; rustc diagnostics also say
# "unexpected argument", so only uv-resolved commands are read for these.
_ARGV_REJECTION = (
    "unexpected argument",
    "unrecognized arguments",
    "unrecognized argument",
    "no such option",
)

type Via = Literal["path", "uv", "uvx", "uv-with"]
type Unreachable = Callable[[subprocess.CompletedProcess[str]], bool]

# Tools whose findings are advisories about SECURITY - secrets, SAST, and the
# CVE/advisory feeds. Turning one of these below what hyperi-ci ships without a
# stated reason fails the stage (:func:`note_gate_downgrade`). Every other tool
# may be turned down with a bare mode string.
SECURITY_TOOLS = frozenset(
    {
        "gitleaks",
        "semgrep",
        "bandit",
        "ruff_security",
        "pip_audit",
        "audit",
        "deny",
        "osv_scanner",
        "gosec",
        "govulncheck",
    }
)


class GateReasonRequiredError(ValueError):
    """A security gate is relaxed in config and gives no reason for it.

    Carries the full operator-facing message and the title to report it
    under, so a caller reports ``str(exc)`` and needs to know nothing about
    the gate that raised.

    Attributes:
        title: Annotation title the failure is reported under in CI.
    """

    title = "hyperi-ci security gate needs a reason"


def _one_line(text: str) -> str:
    """Join ``text`` onto one line and strip it.

    Repo config reaches the log through here, and under GitHub Actions a line
    break in a logged message starts a new line the runner parses as a
    workflow command.
    """
    return " ".join(text.splitlines()).strip()


def mode_and_reason(raw: object, default: str) -> tuple[str, str]:
    """Split a configured quality value into its mode and its stated reason.

    A quality key may be a bare mode string or a mapping carrying ``mode``
    plus options - the shape ``quality.rust.feature_matrix`` already uses,
    and the only one that can hold a ``reason`` beside the setting it
    explains.

    Args:
        raw: The configured value, of either shape.
        default: Mode to assume when a mapping carries no ``mode``.

    Returns:
        The mode, lowercased, and the reason (empty when none), each stripped
        and joined onto one line.

    """
    if isinstance(raw, dict):
        mode = str(raw.get("mode", default))
        reason = str(raw.get("reason") or "")
    else:
        mode = str(raw)
        reason = ""
    return _one_line(mode).lower(), _one_line(reason)


def strict_quality() -> bool:
    """Return True when strict quality mode is active.

    Strict mode upgrades ``warn``-tier findings to ``blocking`` so a
    developer sees - and then fixes or explicitly ignores - everything
    CI would surface BEFORE the push, not after. Enabled by
    ``hyperi-ci check --strict`` (which exports ``HYPERCI_QUALITY_STRICT``)
    or by exporting that env var directly.
    """
    return env_true("HYPERCI_QUALITY_STRICT")


def apply_strict(mode: str) -> str:
    """Upgrade a ``warn`` mode to ``blocking`` under :func:`strict_quality`.

    Shared by :func:`resolve_tool_mode` (per-language tools) and the
    dispatch-level cross-language scans (semgrep) so strict behaves
    identically whichever layer resolved the mode. ``disabled`` and
    ``blocking`` pass through unchanged.
    """
    if mode == "warn" and strict_quality():
        return "blocking"
    return mode


def stricter(mode: str, than: str) -> bool:
    """Return True when ``mode`` gates harder than ``than``.

    Orders ``disabled`` < ``warn`` < ``blocking``, for two checks that report
    the same finding and must agree on which one decides whether it fails.
    """
    return _MODE_STRENGTH.get(mode, 0) > _MODE_STRENGTH.get(than, 0)


def quality_skip() -> frozenset[str]:
    """Tool names to forcibly skip this run (``HYPERCI_QUALITY_SKIP``).

    RARE edge-case escape hatch. When a tool's false positive halts CI
    - a semgrep rule misfiring on a dependency, an audit advisory with
    no fix yet - set ``HYPERCI_QUALITY_SKIP=semgrep`` (comma-separated
    for several) on the blocked runs to skip that tool WITHOUT a config
    commit, then remove it once the real fix (a rule ignore / version
    bump) lands. This is deliberately an env override, not a config knob:
    the reviewed config path (``quality.<tool>: disabled`` or the
    ``quality.ignore`` list) stays the normal way to silence a tool. A
    force-skip is logged loudly (:func:`is_skipped`).
    """
    raw = os.environ.get("HYPERCI_QUALITY_SKIP", "")
    return frozenset(t.strip().lower() for t in raw.split(",") if t.strip())


def is_skipped(tool: str) -> bool:
    """Return True if ``tool`` is force-skipped, surfacing it loudly.

    A force-skip is an emergency override that must NOT pass unnoticed -
    especially for a security scanner like gitleaks. In CI it emits a
    real GitHub ``::warning::`` annotation (which escapes the collapsed
    log group and lands in the run summary), not just a logger line that
    hides inside a folded group.
    """
    if tool.lower() not in quality_skip():
        return False
    msg = (
        f"{tool}: FORCE-SKIPPED via HYPERCI_QUALITY_SKIP - rare edge-case "
        f"override; remove it once the false positive is fixed"
    )
    announce(msg, "hyperi-ci quality force-skip")
    return True


_TURNED_DOWN_TITLE = "hyperi-ci gate turned down"
_REASON_DOCS = "docs/quality-gate.md#relaxing-a-security-gate"


def _mapping_example(key: str, setting: str, placeholder: str) -> str:
    """Render ``setting`` and a ``reason`` under ``key`` as a block to paste."""
    parts = key.split(".")
    lines = [f"{'  ' * depth}{part}:" for depth, part in enumerate(parts)]
    indent = "  " * len(parts)
    lines.append(f"{indent}{setting}")
    lines.append(f'{indent}reason: "{placeholder}"')
    return "\n".join(lines)


def _reason_required_message(key: str, mode: str, shipped: str) -> str:
    """Build the message for a security gate relaxed without a reason."""
    tool = key.rsplit(".", 1)[-1]
    example = _mapping_example(
        key, f"mode: {mode}", "<advisory id, why no fix exists, what mitigates it>"
    )
    return "\n".join(
        (
            f"{key}: {tool} is a security gate, hyperi-ci ships '{shipped}', "
            f"and this repo turns it down to '{mode}' without saying why.",
            "  Name what the gate is waiting on, in the config beside the setting:",
            "",
            example,
            "",
            "  The reason prints in every run, so the decision can be re-checked "
            "later. A bare mode string stays valid for every non-security tool, "
            "and HYPERCI_QUALITY_SKIP is unaffected.",
            f"  docs: {_REASON_DOCS}",
        )
    )


def note_gate_downgrade(
    key: str, mode: str, reason: str = "", *, shipped_key: str | None = None
) -> None:
    """Say when a project has turned a gate below what hyperi-ci ships.

    A gate a repo relaxed and a gate that passed read the same in the log,
    so the relaxation has to announce itself where the run can be read.
    For a tool in :data:`SECURITY_TOOLS` the announcement is not enough:
    turning one below its shipped default with no ``reason`` beside it
    raises :class:`GateReasonRequiredError`, and the stage fails with the
    fix to paste. The comparison is against that tool's OWN shipped default,
    whatever it is: semgrep ships ``warn``, so ``semgrep: warn`` is not a
    downgrade.

    Takes the CONFIGURED mode, not the post-``apply_strict`` one, so
    ``hyperi-ci check --strict`` reports the same config problem CI will.

    Args:
        key: Dotted config key, e.g. ``quality.python.pip_audit``.
        mode: Mode the config resolved to, before any strict upgrade.
        reason: Reason stated beside the setting, empty when none.
        shipped_key: Key carrying the shipped default, where the repo wrote
            its override somewhere else (semgrep's legacy per-language key).

    Raises:
        GateReasonRequiredError: A security gate is relaxed with no reason.

    """
    shipped = packaged_default(shipped_key or key)
    if not isinstance(shipped, str):
        return
    shipped = shipped.strip().lower()
    if _MODE_STRENGTH.get(mode, 2) >= _MODE_STRENGTH.get(shipped, 0):
        return
    if key.rsplit(".", 1)[-1] in SECURITY_TOOLS and not reason:
        raise GateReasonRequiredError(_reason_required_message(key, mode, shipped))
    msg = (
        f"{key}: this repo sets '{mode}', hyperi-ci ships '{shipped}' - "
        f"the gate is turned down here"
    )
    if reason:
        msg = f"{msg}; reason: {reason}"
    announce(msg, _TURNED_DOWN_TITLE)


def _security_gates_shipped(language: str) -> list[str]:
    """Keys of the security gates hyperi-ci ships switched on for ``language``.

    Measured against the shipped defaults, the same yardstick
    :func:`note_gate_downgrade` uses: bandit ships ``disabled``, so turning the
    stage off takes nothing from it.
    """
    tools = sorted(SECURITY_TOOLS)
    candidates = [f"quality.{tool}" for tool in tools]
    candidates += [f"quality.{language}.{tool}" for tool in tools]
    return [
        key
        for key in candidates
        if isinstance(shipped := packaged_default(key), str)
        and stricter(shipped.strip().lower(), "disabled")
    ]


def note_quality_disabled(language: str, reason: str = "") -> None:
    """Say which security gates ``quality.enabled: false`` switches off.

    Turning the stage off drops every security gate at once, so it owes a
    ``reason`` exactly as one security gate turned below its shipped default
    does (:func:`note_gate_downgrade`): without one it raises
    :class:`GateReasonRequiredError`, and with one it announces itself the
    same way. The reason sits beside the switch as ``quality.reason``, the
    shape ``quality.rust.feature_matrix`` uses for its own ``enabled``
    opt-out.

    Args:
        language: Handler language, which picks the per-language gates named.
        reason: ``quality.reason`` as configured, empty when none.

    Raises:
        GateReasonRequiredError: The stage is switched off with no reason.

    """
    gates = ", ".join(_security_gates_shipped(language))
    reason = _one_line(reason)
    if not reason:
        example = _mapping_example(
            "quality",
            "enabled: false",
            "<why this repo runs no quality gates, and what covers them instead>",
        )
        owed = "\n".join(
            (
                "quality.enabled: false turns off every security gate hyperi-ci "
                f"ships for this repo ({gates}) without saying why.",
                "  Name what the stage is waiting on, beside the switch:",
                "",
                example,
                "",
                "  The reason prints in every run, so the decision can be "
                "re-checked later.",
                f"  docs: {_REASON_DOCS}",
            )
        )
        raise GateReasonRequiredError(owed)
    announce(
        "quality.enabled: false - the quality stage does not run, and with it "
        f"every security gate hyperi-ci ships for this repo ({gates}); "
        f"reason: {reason}",
        _TURNED_DOWN_TITLE,
    )


def resolve_tool_mode(
    tool: str,
    config: CIConfig,
    *,
    language: str | None = None,
    default: str = "blocking",
) -> str:
    """Resolve a quality tool's mode: ``blocking``, ``warn`` or ``disabled``.

    Reads ``quality.<language>.<tool>`` for a per-language tool, or the
    top-level ``quality.<tool>`` when ``language`` is None (the cross-language
    checks: gitleaks, hadolint, Checkov, the doc and charset checks and the
    rest). ``default`` applies when the key is unset.

    The value may be a plain mode string OR a mapping carrying a ``mode`` plus
    tool options (Checkov's ``frameworks`` / ``skip``, kubeconform's
    ``schema_locations``, and the ``reason`` a relaxed security gate needs). A
    force-skip (:func:`is_skipped`) wins and makes the tool ``disabled`` for
    this run. Otherwise, under strict mode (:func:`strict_quality`) a ``warn``
    tool is upgraded to ``blocking``; ``disabled`` is left untouched.

    Raises:
        GateReasonRequiredError: A security gate is relaxed with no reason.

    """
    if is_skipped(tool):
        return "disabled"
    key = f"quality.{language}.{tool}" if language else f"quality.{tool}"
    mode, reason = checked_mode(key, config.get(key, default), default)
    note_gate_downgrade(key, mode, reason)
    return apply_strict(mode)


def checked_mode(key: str, raw: object, default: str) -> tuple[str, str]:
    """Split ``raw`` into mode and reason, rejecting an out-of-vocabulary mode.

    A typo (`block`, `enabled`, `true`) must NOT silently downgrade a gate to
    advisory - warn loudly and fall back to the tool's default instead.
    """
    mode, reason = mode_and_reason(raw, default)
    if mode not in _VALID_MODES:
        warn(
            f"{key}: unknown mode '{mode}' - expected "
            f"blocking / warn / disabled; using '{default}'"
        )
        mode = default
    return mode, reason


def resolve_tool_cmd(
    cmd: list[str],
    use_uvx: bool = False,
    use_uv_with: bool = False,
    spec: str | None = None,
    python: str | None = None,
) -> list[str]:
    """Resolve a tool command, preferring a pinned spec over whatever is on PATH.

    A pinned ``spec`` (one with an exact ``==`` version) names the version
    hyperi-ci validated and CI runs; a same-named tool on PATH at a
    different version makes `hyperi-ci check` pass or fail differently from
    CI, silently, because `shutil.which` found it first. So a pin wins
    whenever uv can install it, whether or not the tool is already on PATH.

    An unpinned ``spec`` (the bare command name -- ruff, pytest, the
    project's own dependencies) keeps the PATH-first behaviour: those
    resolve through the project's venv, not a version this module tracks.

    When hyperi-ci runs via uvx, project tools (ruff, pytest, etc.)
    live in the project's .venv, not on PATH. Prefix with 'uv run'
    to execute within the project's virtual environment.

    Args:
        cmd: Command and arguments.
        use_uvx: If True, use 'uvx' instead of 'uv run' for tools
            that are standalone (not project deps).
        use_uv_with: If True, use 'uv run --with <tool>' to install
            the tool temporarily and run it within the project's
            venv. Use for tools that scan installed packages
            (e.g. pip-audit) and must see the project's deps.
        spec: Requirement to install, e.g. ``bandit==1.9.4`` from the
            versions SSOT. Defaults to the bare command name, which lets
            the resolver take whatever PyPI serves that morning.
        python: Interpreter version for a ``uvx`` run, e.g. ``3.14``. A tool
            that parses source with ``ast`` reads only the syntax of the Python
            it runs on, and uvx otherwise picks whatever interpreter it finds.

    Returns:
        The command to run. It equals ``cmd`` both when the PATH copy is the
        answer and when nothing can run it, so a caller tells the two apart
        with ``shutil.which(cmd[0])``.

    """
    spec = spec or cmd[0]
    pinned = "==" in spec
    wants_uv_form = use_uvx or use_uv_with
    uv = shutil.which("uv")
    uvx = ["uvx", "--python", python] if python else ["uvx"]

    if pinned and wants_uv_form and uv:
        if use_uv_with:
            return ["uv", "run", "--with", spec, "--", *cmd]
        # --from, not `uvx <spec>`: the package to install and the command
        # to run are different strings once the version is pinned on.
        return [*uvx, "--from", spec, *cmd]

    if shutil.which(cmd[0]):
        if pinned and wants_uv_form:
            installed = installed_version(cmd[0])
            seen = f" ({installed})" if installed else ""
            warn(
                f"  {cmd[0]}: uv is not on PATH, so the pinned {spec} cannot be "
                f"installed -- running the PATH copy instead{seen}"
            )
        return cmd
    if uv:
        if use_uv_with:
            return ["uv", "run", "--with", spec, "--", *cmd]
        if use_uvx:
            return [*uvx, "--from", spec, *cmd]
        return ["uv", "run", *cmd]
    return cmd


def emit_tool_output(
    tool_name: str, output: str | None, *, cap: int | None = None
) -> None:
    """Log a tool's output line by line through the logger, optionally capped.

    `print()` would write to stdout while the surrounding verdicts go to
    stderr, and the two interleave out of order. A capped run keeps the last
    line, where ruff and ty print their own finding count.

    Args:
        tool_name: Name for the truncation note.
        output: The tool's stdout or stderr.
        cap: Lines to show before the rest is counted, None for all of them.

    """
    if not output or not output.strip():
        return
    lines = output.rstrip().splitlines()
    if cap is not None and len(lines) > cap:
        hidden = len(lines) - cap - 1
        shown = lines[:cap]
        if hidden:
            shown.append(
                f"... +{hidden} more lines from {tool_name}; "
                "raise its mode to see them all"
            )
        lines = [*shown, lines[-1]]
    for line in lines:
        info(f"    {line}")


def _run_until_reachable(
    tool_name: str, cmd: list[str], unreachable: Unreachable
) -> subprocess.CompletedProcess[str]:
    """Run an advisory-DB scan, again after a backoff while the DB is unreachable.

    Up to ``URL_ATTEMPTS`` runs, waiting about 1s, 2s, then 4s between them,
    each wait cut by up to half at random. Any other outcome, a finding
    included, ends it at once.

    Args:
        tool_name: Name for the retry log line.
        cmd: The resolved command.
        unreachable: Whether a finished run failed only on the advisory DB.

    Returns:
        The last run.

    """
    for attempt in range(1, URL_ATTEMPTS):
        result = run_cmd(cmd, check=False, capture=True)
        if not unreachable(result):
            return result
        lines = (result.stderr or "").strip().splitlines()
        reason = lines[-1] if lines else f"exit {result.returncode}"
        delay = backoff(attempt)
        info(
            f"  {tool_name}: advisory DB unreachable ({reason}), retrying in "
            f"{delay:.1f}s (retry {attempt} of {URL_ATTEMPTS - 1})"
        )
        time.sleep(delay)
    return run_cmd(cmd, check=False, capture=True)


def run_gate_tool(
    tool_name: str,
    cmd: list[str],
    mode: str,
    *,
    via: Via = "path",
    spec: str | None = None,
    python: str | None = None,
    pinned: str | None = None,
    retry_unreachable: Unreachable | None = None,
    unscanned: Callable[[str | None], int] | None = None,
    output_is_finding: bool = False,
) -> bool:
    """Run one quality tool and decide the gate from its result and mode.

    A tool that is not installed fails a ``blocking`` gate in CI, where every
    tool must be present, and warn-skips everywhere else so a local
    ``hyperi-ci check`` still runs whatever is installed.

    Args:
        tool_name: Name used in every log line.
        cmd: Command and arguments, before resolution.
        mode: ``blocking``, ``warn`` or ``disabled``.
        via: ``path`` runs ``cmd`` as given. The others resolve it through
            :func:`resolve_tool_cmd`: ``uv`` falls back to ``uv run`` in the
            project's environment, ``uvx`` installs the tool standalone, and
            ``uv-with`` installs it into the project's environment. A
            uv-resolved run that could not start, or whose tool refused its
            command line, checked nothing and is reported as such.
        spec: Requirement to install for ``uvx`` or ``uv-with``.
        python: Interpreter version for a ``uvx`` run.
        pinned: ``versions.yaml`` key of the binary behind ``cmd``, whose PATH
            copy is checked against the pin before it runs.
        retry_unreachable: Whether a run failed only because the advisory DB
            was unreachable. Such a run is made again with a backoff, and one
            that never reaches the DB is then decided by ``mode``.
        unscanned: Counts the files a tool's stdout says it skipped. A
            ``blocking`` pass that skipped any fails.
        output_is_finding: Treat any stdout as a finding, for a tool such as
            ``gofmt -l`` that lists what it found and still exits 0.

    Returns:
        True if the pipeline should continue, False on a blocking failure.

    """
    if mode == "disabled":
        info(f"  {tool_name}: disabled")
        return True

    resolved = cmd
    if via != "path":
        resolved = resolve_tool_cmd(
            cmd,
            use_uvx=via == "uvx",
            use_uv_with=via == "uv-with",
            spec=spec,
            python=python,
        )
    if resolved == cmd and not shutil.which(cmd[0]):
        if mode == "blocking" and is_ci():
            error(f"  {tool_name}: not installed (required)")
            return False
        warn(f"  {tool_name}: not installed (skipping locally)")
        return True

    if pinned:
        warn_on_pin_drift(pinned)
    if retry_unreachable:
        result = _run_until_reachable(tool_name, resolved, retry_unreachable)
    else:
        result = run_cmd(resolved, check=False, capture=True)
    skipped = unscanned(result.stdout) if unscanned else 0
    found = output_is_finding and bool((result.stdout or "").strip())

    if result.returncode == 0 and not found:
        if skipped and mode == "blocking":
            error(f"  {tool_name}: failed, {skipped} file(s) were not scanned")
            return False
        success(f"  {tool_name}: passed")
        return True

    stderr = (result.stderr or "").lower()
    if via != "path" and any(marker in stderr for marker in _SPAWN_FAILURES):
        if mode == "blocking" and is_ci():
            error(f"  {tool_name}: could not start (required)")
            emit_tool_output(tool_name, result.stderr)
            return False
        warn(f"  {tool_name}: could not start, so it checked nothing")
        emit_tool_output(tool_name, result.stderr, cap=WARN_OUTPUT_CAP)
        return True

    if via != "path" and any(marker in stderr for marker in _ARGV_REJECTION):
        note = (
            f"  {tool_name}: rejected the command line and checked nothing "
            f"-- tool-version mismatch, not a finding"
        )
        if mode == "warn":
            warn(note)
        else:
            error(note)
        emit_tool_output(tool_name, result.stderr)
        return mode == "warn"

    unreachable_note = ""
    if retry_unreachable and retry_unreachable(result):
        unreachable_note = (
            f"  {tool_name}: advisory DB unreachable after {URL_ATTEMPTS} "
            f"attempts, so nothing was checked"
        )

    if mode == "warn":
        warn(f"  {tool_name}: issues found (non-blocking)")
        if unreachable_note:
            warn(unreachable_note)
        emit_tool_output(tool_name, result.stdout, cap=WARN_OUTPUT_CAP)
        emit_tool_output(tool_name, result.stderr, cap=WARN_OUTPUT_CAP)
        return True

    error(f"  {tool_name}: failed")
    if unreachable_note:
        error(unreachable_note)
    emit_tool_output(tool_name, result.stdout)
    emit_tool_output(tool_name, result.stderr)
    return False


def get_test_paths(config: CIConfig) -> list[str]:
    """Get configured test directories that exist on disk.

    Reads quality.test_paths from config, falling back to the shipped default.
    Only returns paths that actually exist as directories.
    """
    configured = config.get("quality.test_paths")
    if not isinstance(configured, list):
        configured = packaged_default("quality.test_paths", [])
    return [p for p in configured if Path(p).is_dir()]


def _ignored_dir(name: str, excluded: list[str]) -> bool:
    """Whether a directory called ``name`` is never searched for Python source."""
    if name.startswith("."):
        return True
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in excluded)


def _holds_python(top: Path, excluded: list[str]) -> bool:
    """Whether ``top`` holds a ``.py`` file outside every ignored directory."""
    for _dirpath, dirnames, filenames in top.walk():
        if any(name.endswith(".py") for name in filenames):
            return True
        dirnames[:] = [d for d in dirnames if not _ignored_dir(d, excluded)]
    return False


def get_python_source_paths(config: CIConfig) -> list[str]:
    """Find the directories that hold a Python project's source.

    A ``src/`` directory holding a ``.py`` file is the whole answer. Otherwise
    each top-level directory holding a ``.py`` file at any depth counts, apart from
    the test paths, hidden directories, the handler's quality excludes, and
    build, docs and environment directories. Modules at the repo root are
    left out: ``setup.py``, ``conftest.py`` and ``noxfile.py`` are tooling.

    Args:
        config: Merged CI configuration, read for the test paths and excludes.

    Returns:
        Existing directories with a trailing ``/``, sorted, or an empty list
        when the repo has no Python source directory.

    """
    excluded = [
        *_NOT_PYTHON_SOURCE,
        *(e.rstrip("/") for e in get_exclude_dirs(config._raw)),
    ]
    src = Path("src")
    if src.is_dir() and _holds_python(src, excluded):
        return ["src/"]
    tests = {Path(p).as_posix() for p in get_test_paths(config)}
    found = sorted(
        entry.name
        for entry in Path.cwd().iterdir()
        if entry.is_dir()
        and entry.name not in tests
        and not _ignored_dir(entry.name, excluded)
        and _holds_python(entry, excluded)
    )
    if found:
        info(
            f"Python source: no src/ package, scanning {', '.join(f'{d}/' for d in found)}"
        )
    return [f"{d}/" for d in found]


def get_test_ignore(language: str, config: CIConfig) -> list[str]:
    """Get test_ignore rules for a language.

    Projects override entirely via quality.<language>.test_ignore in
    .hyperi-ci.yaml. Otherwise the shipped defaults.yaml list applies, and a
    language that ships none gets an empty list.
    """
    configured = config.get(f"quality.{language}.test_ignore")
    if not isinstance(configured, list):
        configured = packaged_default(f"quality.{language}.test_ignore", [])
    return [str(r) for r in configured]
