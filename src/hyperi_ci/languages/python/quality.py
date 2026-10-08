# Project:   HyperI CI
# File:      src/hyperi_ci/languages/python/quality.py
# Purpose:   Python quality checks (ruff, ty, bandit, pip-audit, vulture)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Python quality checks handler.

Runs ruff (lint, format, S rules, D rules), ty, bandit, pip-audit and vulture,
each with a blocking/warn/disabled mode from ``quality.python`` in
.hyperi-ci.yaml. semgrep runs once at dispatch level, not here.

The bandit-class check is ruff's S rules, run as their own pass whatever the
repo's ruff selects. bandit itself ships disabled. Docstring coverage uses ruff
D rules because interrogate is unmaintained and pulls in the vulnerable 'py'
package.
"""

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from packaging.version import InvalidVersion, Version

from hyperi_ci.common import get_exclude_dirs, info, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import (
    WARN_OUTPUT_CAP,
    Via,
    emit_tool_output,
    get_python_source_paths,
    get_test_ignore,
    get_test_paths,
    resolve_tool_cmd,
    resolve_tool_mode,
    run_gate_tool,
)
from hyperi_ci.python_version import resolve as resolve_python_version
from hyperi_ci.quality.ignores import IgnoreEntry, for_tool, load_ignores
from hyperi_ci.versions import tool_version

# `ruff format` accepts --extend-exclude from 0.15.21, a release earlier than
# Markdown formatting (0.16).
_RUFF_FORMAT_EXTEND_EXCLUDE_MIN = Version("0.15.21")

# pip-audit's summary line when it has vulnerabilities to report.
_PIP_AUDIT_FINDING = re.compile(r"^Found \d+ known vulnerabilit", re.MULTILINE)

# pip-audit 2.10's lines for an unreachable advisory DB, which exits 1 exactly
# as a finding does.
_ADVISORY_DB_UNREACHABLE = re.compile(
    r"^(?:requests\.exceptions\.(?:ConnectionError|ProxyError|ReadTimeout"
    r"|ChunkedEncodingError)\b"
    r"|urllib3\.exceptions\.ProtocolError\b"
    r"|requests\.exceptions\.HTTPError: 5\d\d "
    r"|ERROR:pip_audit\._cli:Could not connect to "
    r"|ERROR:pip_audit\._cli:PyPI is not redirecting properly)",
    re.MULTILINE,
)


def _component_patterns(excludes: list[str]) -> list[str]:
    """Rewrite each bare name as a glob that matches it as a whole path component.

    bandit and vulture match a pattern without wildcards as a substring of the
    path, so a bare ``data`` would also drop ``src/pkg/metadata.py``.
    """
    return [e if "/" in e else f"*/{e}/*" for e in excludes]


def _build_exclude_args(tool: str, excludes: list[str]) -> list[str]:
    """Build exclusion arguments for a quality tool."""
    if not excludes:
        return []
    if tool == "ruff":
        # --exclude replaces the repo's own ruff excludes, --extend-exclude adds.
        return [f"--extend-exclude={','.join(excludes)}"]
    if tool in ("bandit", "vulture"):
        return [f"--exclude={','.join(_component_patterns(excludes))}"]
    return []


def _ruff_ignore_flag(ignores: list[IgnoreEntry]) -> list[str]:
    """Translate ``quality.ignore`` entries for ruff into one ``--extend-ignore``."""
    if not ignores:
        return []
    return [f"--extend-ignore={','.join(e.id for e in ignores)}"]


def _build_ruff_security_cmd(
    sources: list[str], excludes: list[str], ignores: list[IgnoreEntry]
) -> list[str]:
    """Build the ruff S (flake8-bandit) pass over production code.

    ``--select S`` replaces the repo's rule selection AND its ruff ``ignore``
    list. ``per-file-ignores``, ``# noqa`` and ``quality.ignore`` entries for
    ruff still apply. Concise output keeps one finding per line, so the warn
    tier's line cap shows findings rather than one code frame.

    Args:
        sources: Source directories to scan, from ``get_python_source_paths``.
        excludes: Handler excludes, added to the repo's own ruff excludes.
        ignores: ``quality.ignore`` entries for the ``ruff`` slug.

    Returns:
        The command, before resolution.

    """
    return (
        ["ruff", "check", "--select", "S", "--output-format=concise", *sources]
        + _build_exclude_args("ruff", excludes)
        + _ruff_ignore_flag(ignores)
    )


def _build_pip_audit_cmd(ignores: list[IgnoreEntry]) -> list[str]:
    """Build the pip-audit command, one ``--ignore-vuln <id>`` per ignore entry.

    pip-audit scans the active environment, so with uv it runs as
    ``uv run --with pip-audit==<pin> -- pip-audit`` to see the project's .venv
    and not ~/.venv or the system Python. Without uv it is bare ``pip-audit``.
    """
    base = ["pip-audit"]
    for entry in ignores:
        base.extend(["--ignore-vuln", entry.id])
    if shutil.which("uv"):
        spec = f"pip-audit=={tool_version('pip-audit')}"
        return ["uv", "run", "--with", spec, "--", *base]
    return base


_BANDIT_SKIPPED = re.compile(r"^Files skipped \((\d+)\):$", re.MULTILINE)


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _parse_python() -> str:
    """Return the interpreter version for tools that parse source with ``ast``.

    bandit and vulture read only the syntax of the Python they run on, so they
    take the project's declared Python, or the running one when that is newer.
    """
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    declared, _ = resolve_python_version()
    if not declared:
        return running
    return max(declared, running, key=_version_key)


def _warn_bandit_skips(stdout: str | None) -> int:
    """Name the files bandit could not parse, and return how many there were.

    bandit still exits 0 over them, so without this they read as scanned.
    """
    match = _BANDIT_SKIPPED.search(stdout or "")
    if not match or match.group(1) == "0":
        return 0
    warn(f"  bandit: {match.group(1)} file(s) could not be parsed and were NOT scanned")
    listed = (stdout or "")[match.end() :].strip().splitlines()
    emit_tool_output("bandit", "\n".join(listed), cap=WARN_OUTPUT_CAP)
    return int(match.group(1))


def _advisory_db_unreachable(result: subprocess.CompletedProcess[str]) -> bool:
    """Whether a pip-audit run failed only because the advisory DB was unreachable.

    A run that reported vulnerabilities never counts.

    Args:
        result: One finished pip-audit run.

    Returns:
        True for a failed run whose output names a connection-level failure and
        no finding.

    """
    if result.returncode == 0:
        return False
    output = f"{result.stdout or ''}\n{result.stderr or ''}"
    if _PIP_AUDIT_FINDING.search(output):
        return False
    return _ADVISORY_DB_UNREACHABLE.search(output) is not None


def _run_source_tool(
    tool_name: str,
    cmd: list[str],
    mode: str,
    sources: list[str],
    *,
    via: Via,
    spec: str | None = None,
    python: str | None = None,
    unscanned: Callable[[str | None], int] | None = None,
) -> bool:
    """Run a tool that scans the source directories, or say why it cannot.

    Pointed at a directory that does not exist, ruff warns, checks no file and
    exits 0, which reads as a pass.

    Args:
        tool_name: Name used in every log line.
        cmd: Command and arguments, before resolution.
        mode: ``blocking``, ``warn`` or ``disabled``.
        sources: Source directories the command scans.
        via: How the command resolves, as for ``run_gate_tool``.
        spec: Requirement to install for a ``uvx`` run.
        python: Interpreter version for a ``uvx`` run.
        unscanned: Counts the files the tool's stdout says it skipped.

    Returns:
        True if the pipeline should continue, False on a blocking failure.

    """
    if mode != "disabled" and not sources:
        warn(f"  {tool_name}: skipped, no Python source directory found")
        return True
    return run_gate_tool(
        tool_name, cmd, mode, via=via, spec=spec, python=python, unscanned=unscanned
    )


def _ruff_format_takes_extend_exclude() -> bool:
    """Whether the project's ruff accepts --extend-exclude on `format`.

    Asks the resolved ruff, as consumers' ruff is not pinned. An unreadable
    answer keeps the flag, and `run_gate_tool`'s argument-rejection path then
    names the mismatch.
    """
    try:
        result = subprocess.run(
            resolve_tool_cmd(["ruff", "--version"], via="uv"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return True
    if result.returncode != 0:
        return True

    parts = result.stdout.split()
    if len(parts) < 2:
        return True
    try:
        return Version(parts[1]) >= _RUFF_FORMAT_EXTEND_EXCLUDE_MIN
    except InvalidVersion:
        return True


def _build_ruff_format_cmd(
    excludes: list[str], *, extend_exclude: bool = True
) -> list[str]:
    """Build the ruff format command.

    Markdown is excluded because ruff 0.16 formats it, which would pull every
    consumer's docs into a Python-only gate.

    `extend_exclude` is False for a ruff below 0.15.21, which rejects the flag
    and never walks Markdown, so only the project's own excludes are lost.
    """
    cmd = ["ruff", "format", "--check", "."]
    if extend_exclude:
        cmd.append(f"--extend-exclude={','.join([*excludes, '*.md'])}")
    return cmd


def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Run Python quality checks.

    Args:
        config: Merged CI configuration.
        extra_env: Additional environment variables (unused for Python).

    Returns:
        Exit code (0 = success).

    """
    info("Running Python quality checks...")
    excludes = get_exclude_dirs(config._raw)
    ignores = load_ignores(config._raw)
    had_failure = False

    # Ruff lint runs twice: production (strict), then tests (relaxed).
    mode = resolve_tool_mode("ruff", config, language="python")
    exclude_args = _build_exclude_args("ruff", excludes)
    output_fmt = ["--output-format=github"] if os.environ.get("GITHUB_ACTIONS") else []

    test_paths = get_test_paths(config)
    test_ignore = get_test_ignore("python", config)

    # Production pass: test dirs excluded, full rules.
    prod_exclude = exclude_args + [f"--exclude={p}" for p in test_paths]
    ruff_user_ignores = for_tool(ignores, "ruff")
    if not run_gate_tool(
        "ruff check (src)",
        ["ruff", "check", "."]
        + output_fmt
        + prod_exclude
        + _ruff_ignore_flag(ruff_user_ignores),
        mode,
        via="uv",
    ):
        had_failure = True

    # Test pass: relaxed rules, same mode.
    if test_paths and test_ignore:
        combined_ignore = test_ignore + [e.id for e in ruff_user_ignores]
        ignore_flag = [f"--extend-ignore={','.join(combined_ignore)}"]
        for tp in test_paths:
            if not run_gate_tool(
                f"ruff check ({tp})",
                ["ruff", "check", tp] + output_fmt + ignore_flag + exclude_args,
                mode,
                via="uv",
            ):
                had_failure = True

    # Own mode, not ruff check's: sharing the key would force a project to relax
    # the lint gate to defer adopting the formatter.
    format_extend_exclude = _ruff_format_takes_extend_exclude()
    if not format_extend_exclude and excludes:
        warn(
            f"  ruff format: this ruff predates {_RUFF_FORMAT_EXTEND_EXCLUDE_MIN} and "
            f"rejects --extend-exclude, so {', '.join(excludes)} stays in the format "
            f"check. Raise the project's ruff to honour it."
        )
    if not run_gate_tool(
        "ruff format",
        _build_ruff_format_cmd(excludes, extend_exclude=format_extend_exclude),
        resolve_tool_mode("ruff_format", config, language="python"),
        via="uv",
    ):
        had_failure = True

    # Type checking (ty from Astral, or pyright as fallback)
    ty_mode = resolve_tool_mode("ty", config, language="python")
    pyright_mode = resolve_tool_mode("pyright", config, language="python")
    if ty_mode != "disabled":
        # In the project's environment, so it resolves the project's imports.
        ty_spec = f"ty=={tool_version('ty')}"
        if not run_gate_tool(
            "ty", ["ty", "check"], ty_mode, via="uv-with", spec=ty_spec
        ):
            had_failure = True
    elif pyright_mode != "disabled":
        if not run_gate_tool("pyright", ["pyright"], pyright_mode, via="uv"):
            had_failure = True

    sources = get_python_source_paths(config)
    parse_python = _parse_python()

    mode = resolve_tool_mode("bandit", config, language="python")
    bandit_cmd = ["bandit", "-r", *sources, "-ll"]
    if Path("pyproject.toml").exists():
        bandit_cmd.extend(["-c", "pyproject.toml"])
    # bandit's --exclude is action="store": a second flag replaces the first
    # rather than adding to it, so test paths and quality excludes share one.
    bandit_test_excludes = (
        test_paths if config.get("quality.python.bandit_exclude_tests", True) else []
    )
    bandit_excludes = [*bandit_test_excludes, *_component_patterns(excludes)]
    if bandit_excludes:
        bandit_cmd.extend(["--exclude", ",".join(bandit_excludes)])
    bandit_ignores = for_tool(ignores, "bandit")
    if bandit_ignores:
        bandit_cmd.extend(["--skip", ",".join(e.id for e in bandit_ignores)])
    bandit_spec = f"bandit=={tool_version('bandit')}"
    if not _run_source_tool(
        "bandit",
        bandit_cmd,
        mode,
        sources,
        via="uvx",
        spec=bandit_spec,
        python=parse_python,
        unscanned=_warn_bandit_skips,
    ):
        had_failure = True

    # Own key, so it can go blocking without touching the lint gate.
    if not _run_source_tool(
        "ruff security",
        _build_ruff_security_cmd(sources, excludes, ruff_user_ignores),
        resolve_tool_mode("ruff_security", config, language="python"),
        sources,
        via="uv",
    ):
        had_failure = True

    mode = resolve_tool_mode("pip_audit", config, language="python")
    pip_audit_cmd = _build_pip_audit_cmd(for_tool(ignores, "pip-audit"))
    if not run_gate_tool(
        "pip-audit",
        pip_audit_cmd,
        mode,
        via="uv",
        retry_unreachable=_advisory_db_unreachable,
    ):
        had_failure = True

    # Concise output is one finding per line, so the warn tier's cap counts findings.
    mode = resolve_tool_mode("ruff_docstrings", config, language="python")
    ruff_doc_cmd = [
        "ruff", "check", "--select", "D", "--output-format=concise", *sources
    ]  # fmt: skip
    ruff_doc_cmd += _build_exclude_args("ruff", excludes)
    ruff_doc_cmd += _ruff_ignore_flag(ruff_user_ignores)
    if not _run_source_tool("ruff docstrings", ruff_doc_cmd, mode, sources, via="uv"):
        had_failure = True

    mode = resolve_tool_mode("vulture", config, language="python")
    vulture_cmd = ["vulture", *sources] + _build_exclude_args("vulture", excludes)
    vulture_spec = f"vulture=={tool_version('vulture')}"
    if not _run_source_tool(
        "vulture",
        vulture_cmd,
        mode,
        sources,
        via="uvx",
        spec=vulture_spec,
        python=parse_python,
    ):
        had_failure = True

    return 1 if had_failure else 0
