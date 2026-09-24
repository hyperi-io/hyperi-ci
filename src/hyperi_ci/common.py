# Project:   HyperI CI
# File:      src/hyperi_ci/common.py
# Purpose:   Shared utilities for CI scripts (output, subprocess, exclusions)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Shared utilities for HyperI CI.

Uses scalo logger for structured output with automatic environment
detection (GitHub Actions workflow commands, Solarized terminal, plain CI).
"""

import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from scalo.logger import logger

# Initialise logger for CI use (auto-detects GH Actions, CI, terminal)
from scalo.logger import setup as _setup_logger
from scalo.logger.scrub import ScrubConfig, SecretsConfig

if TYPE_CHECKING:
    from hyperi_ci.config import CIConfig

# scalo's default scrubber minus gitleaks' generic-api-key rule, which matches
# log prose containing the word "keys" and ate our own config warnings (#255).
SCRUB_CONFIG = ScrubConfig(
    secrets=SecretsConfig(exclude_rules=frozenset({"generic-api-key"}))
)

_setup_logger(ci_mode=None, scrub_config=SCRUB_CONFIG)


def sanitize_ref_name(ref: str) -> str:
    """Sanitize a git ref name for use in file paths.

    Replaces '/' (from branch names like 'fix/reconcile-release') with '-'
    so the ref can be safely used in artifact filenames.
    """
    return ref.replace("/", "-")


def resolve_release_version() -> str | None:
    """Resolve the version being released - single SSoT for every stage.

    Precedence (issue #27): the Plan job's predicted ``next-version``, threaded
    in via ``HYPERCI_VERSION``, is authoritative -- the same value Build stamps
    and Tag-and-Publish tags, so every job in a run agrees. The committed
    ``VERSION`` file is a fallback only (local runs); it is stale in CI now that
    stamping is central and not committed back. Leading ``v`` is stripped.
    Returns None when neither is set (caller decides whether that's fatal).

    Container, binary and registry publish all call this -- do NOT re-implement
    version reading per stage, or they drift (which is exactly how the GH
    release shipped a stale tag once set-version.py was removed).
    """
    explicit = os.environ.get("HYPERCI_VERSION", "").strip()
    if explicit:
        return explicit.removeprefix("v")
    version_file = Path("VERSION")
    if version_file.exists():
        value = version_file.read_text().strip()
        if value:
            return value.removeprefix("v")
    return latest_version_tag()


def latest_version_tag() -> str | None:
    """Highest final-release ``vX.Y.Z`` tag as a bare version, or None outside a repo.

    Last-resort fallback for a checkout with no ``VERSION`` file (issue #85 --
    the file is an artefact, so a repo may legitimately not carry one). The
    tag is the released version, so this is one behind mid-release; callers
    that need the version being released read ``HYPERCI_VERSION``.
    """
    result = run_cmd(
        ["git", "tag", "--list", "v[0-9]*", "--sort=-v:refname"],
        capture=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    # Plain vX.Y.Z only -- a prerelease sorts above its own release under
    # -v:refname, so the raw top line resolves 1.1.2-beta.1 over the
    # released 1.1.1.
    for line in result.stdout.splitlines():
        candidate = line.strip().removeprefix("v")
        if _SEMVER_RE.match(candidate):
            return candidate
    return None


_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def explicit_version(value: str | None) -> str | None:
    """Bare ``X.Y.Z`` if ``value`` is an explicit version, else None.

    The from-head ``bump`` channel doubles as an explicit-version override
    (``hyperi-ci publish --version X.Y.Z``): ``auto``/``patch``/``minor`` are
    bump levels resolved at release time; a bare semver is taken verbatim and
    tagged at HEAD. A leading ``v`` is tolerated. Only plain ``X.Y.Z`` is
    accepted (no pre-release / build metadata) -- releases here are always
    plain semver and the tag format is ``v${version}``.
    """
    candidate = value.strip().removeprefix("v") if value else ""
    return candidate if _SEMVER_RE.match(candidate) else None


def is_ci() -> bool:
    """Detect if running in a CI/runner environment."""
    return any(
        os.environ.get(v)
        for v in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL", "BUILDKITE")
    )


def is_github_actions() -> bool:
    """Detect if running in GitHub Actions specifically."""
    return bool(os.environ.get("GITHUB_ACTIONS"))


def is_macos() -> bool:
    """Detect if running on macOS."""
    return sys.platform == "darwin"


def is_linux() -> bool:
    """Detect if running on Linux."""
    return sys.platform.startswith("linux")


_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def truthy(value: object) -> bool:
    """Return True when a config value reads as on (1/true/yes/on, any case)."""
    return str(value).strip().lower() in _TRUTHY


def env_true(name: str) -> bool:
    """Return True when env var ``name`` holds an opt-in value (1/true/yes/on)."""
    return truthy(os.environ.get(name, ""))


def skip_optimize(config: CIConfig | None = None) -> bool:
    """Whether this run drops the optimisation stage.

    Language-agnostic: Rust reads it as no PGO and no BOLT with Tier 1
    (allocator + LTO) still applied, and a language with no optimisation
    stage ignores it. ``HYPERCI_SKIP_OPTIMIZE``, set by the reusable
    workflows from their ``skip-optimize`` input, beats ``build.skip_optimize``.
    It is read here rather than through the ``HYPERCI_*`` config mapping,
    which splits on underscores and would land it at ``skip.optimize``.
    """
    raw = os.environ.get("HYPERCI_SKIP_OPTIMIZE", "").strip().lower()
    if raw in _TRUTHY or raw in _FALSY:
        return raw in _TRUTHY
    if raw:
        warn(
            f"HYPERCI_SKIP_OPTIMIZE={raw!r} is not a boolean -- using build.skip_optimize"
        )
    if config is None:
        return False
    return truthy(config.get("build.skip_optimize", False))


def release_unoptimized() -> bool:
    """Whether this run consents to shipping a skipped-optimisation build as a release.

    A separate consent from ``skip_optimize``: skipping gets a build out fast,
    and publishing that build under a release tag is its own deliberate act.
    Read from ``HYPERCI_RELEASE_UNOPTIMIZED`` only, which the reusable
    workflows set from their per-run ``release-unoptimized`` input. There is
    no repo variable and no config key, so the consent never outlives the run.
    """
    return env_true("HYPERCI_RELEASE_UNOPTIMIZED")


def is_prerelease_build() -> bool:
    """Whether this run ships a prerelease version rather than a stable one.

    ``HYPERCI_PRERELEASE``, set by the reusable workflows from the plan job's
    ``prerelease`` output, answers first; otherwise the version being released
    answers for itself, so a local run needs no extra variable. Identity is
    separate from the optimisation tier (``HYPERCI_CHANNEL``): a prerelease may
    be built at any tier, which is what makes a full release rehearsable
    without spending a stable version (issue #144).
    """
    from hyperi_ci.release_branches import is_prerelease_version

    raw = os.environ.get("HYPERCI_PRERELEASE", "").strip().lower()
    if raw in _TRUTHY or raw in _FALSY:
        return raw in _TRUTHY
    return is_prerelease_version(resolve_release_version())


def info(msg: str) -> None:
    """Info message -- delegates to scalo logger."""
    logger.info(msg)


def success(msg: str) -> None:
    """Success message -- delegates to scalo logger."""
    logger.success(msg)


def warn(msg: str) -> None:
    """Warning -- delegates to scalo logger."""
    logger.warning(msg)


def error(msg: str) -> None:
    """Error -- delegates to scalo logger."""
    logger.error(msg)


def fatal(msg: str) -> None:
    """Fatal error -- log and exit with code 1."""
    logger.critical(msg)
    sys.exit(1)


@contextmanager
def group(title: str) -> Iterator[None]:
    """Collapsible group in GH Actions logs. No-op elsewhere."""
    # Flush: stdout is a pipe under CI, so an unflushed marker orders after
    # output from a child process that inherited the fd.
    if is_github_actions():
        print(f"::group::{title}", flush=True)
    try:
        yield
    finally:
        if is_github_actions():
            print("::endgroup::", flush=True)


def escape_command_data(value: str) -> str:
    """Percent-encode a value for the data half of a workflow command.

    The runner runs the inverse (``UnescapeData``) on whatever follows the
    ``::``, so anything not encoded here arrives as a DIFFERENT string than we
    sent. ``%`` first, then the line breaks -- the other order would re-encode
    the ``%`` this function just inserted. Same sequence as
    ``@actions/core``'s ``escapeData``.

    Encoding the line breaks is also what keeps the command on one line, so a
    newline in the value cannot close it and have the remainder parsed as a
    further command.
    """
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def mask(value: str) -> None:
    """Register a value for redaction in GH Actions logs.

    ``::add-mask::`` is the redaction primitive, not a log line -- the runner
    consumes the command and replaces every later occurrence of the value with
    ``***``. CodeQL reads the write as clear-text logging of a secret
    (``py/clear-text-logging-sensitive-data``) and the taint it traces is real --
    the one caller passes an R2 secret key -- but this is the mitigation for that
    taint, not an instance of it, so the alert is dismissed as a false positive.

    Editing this function re-raises it under a NEW alert number, because the
    dismissal is keyed to the code fingerprint rather than the rule.

    The value is escaped rather than split on newlines: an unescaped ``%``
    registers a different string and leaves the real secret unmasked, and the
    runner already registers both the whole value and each of its lines.

    The write is flushed because masking is not retroactive -- under CI stdout
    is a pipe, and an unflushed command reaches the log after a child process
    the caller spawns has already printed the secret.

    Whitespace-only values are dropped; the runner rejects them.
    """
    if not is_github_actions() or not value.strip():
        return
    print(f"::add-mask::{escape_command_data(value)}", flush=True)


def normalise_tristate(raw: object, *, key: str) -> str:
    """Coerce a YAML on/off/auto setting into ``true`` / ``false`` / ``auto``.

    The house shape for a stage gate: ``false`` never runs, ``true``
    always runs (and fails loudly when it can't), ``auto`` runs iff
    detection finds a signal. YAML hands us a real bool for ``true`` /
    ``false`` and a string for ``auto``, so both are accepted.

    An unrecognised value warns and falls back to ``auto`` -- a typo in
    a config key shouldn't turn a build red on its own, and the warning
    names the key so it's findable.

    Args:
        raw: The value as read from the config cascade.
        key: Dotted config key, used in the warning text.

    Returns:
        One of ``"true"``, ``"false"``, ``"auto"``.

    """
    if raw is True:
        return "true"
    if raw is False:
        return "false"
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"true", "false", "auto"}:
            return lowered
    elif raw is None:
        # Key absent, or present with an empty value -- the default.
        return "auto"
    # Anything else (a bare `1`, a float, a list) is a config mistake.
    # `producer: 1` reads as "on" to a human and would otherwise do the
    # opposite in silence.
    warn(f"Unknown {key} value {raw!r} — falling back to 'auto'")
    return "auto"


def run_cmd(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess with consistent error handling.

    Args:
        cmd: Command as list of strings.
        check: Raise CalledProcessError on non-zero exit.
        capture: Capture stdout/stderr instead of passing through.
        cwd: Working directory.
        env: Additional env vars (merged with os.environ).
        timeout: Seconds before the child is killed and
            ``subprocess.TimeoutExpired`` raised. None waits for it to exit.

    Returns:
        CompletedProcess with text output.

    """
    run_env = None
    if env:
        run_env = {**os.environ, **env}

    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        env=run_env,
        timeout=timeout,
    )


def _log_line(line: str) -> None:
    info(f"  {line}")


def stream_cmd(
    cmd: list[str],
    *,
    on_line: Callable[[str], None] = _log_line,
    on_heartbeat: Callable[[float, int], None] | None = None,
    heartbeat_seconds: float = 30.0,
    cwd: str | Path | None = None,
) -> tuple[int, str]:
    """Run a subprocess, handing over each output line as it arrives.

    For a step whose silence is itself the symptom: a process that hangs still
    leaves every line it printed, and ``on_heartbeat`` fires on an interval
    while it runs, so the log says how long it has been going (issue #261).

    Args:
        cmd: Command as list of strings.
        on_line: Called with each line of combined stdout and stderr.
        on_heartbeat: Called with elapsed seconds and the child's pid every
            ``heartbeat_seconds`` until the process exits.
        heartbeat_seconds: Interval between heartbeats.
        cwd: Working directory.

    Returns:
        The exit code and the combined output.

    Raises:
        OSError: The command could not be started -- ``FileNotFoundError``
            when it does not exist, ``PermissionError`` when it lacks the
            execute bit.

    """
    # The python36 compatibility rules cannot apply on the 3.14 floor.
    # nosemgrep: python.lang.compatibility.python36.python36-compatibility-Popen1, python.lang.compatibility.python36.python36-compatibility-Popen2
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
    )
    lines: list[str] = []

    def _read() -> None:
        if proc.stdout is None:
            return
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            lines.append(line)
            on_line(line)

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    started = time.monotonic()
    while True:
        try:
            returncode = proc.wait(timeout=heartbeat_seconds)
            break
        except subprocess.TimeoutExpired:
            if on_heartbeat is not None:
                on_heartbeat(time.monotonic() - started, proc.pid)

    # Bounded: a grandchild that inherited the pipe can hold it open after the
    # child exits, and waiting on that would be a new hang.
    reader.join(timeout=10)
    return returncode, "\n".join(lines)


# Common directories to exclude from quality checks
_COMMON_EXCLUDES = [
    ".venv",
    "venv",
    "env",
    ".env",
    "virtualenv",
    ".virtualenv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".hypothesis",
    "*.egg-info",
    ".eggs",
    "dist",
    "build",
    "wheelhouse",
    ".tox",
    ".nox",
    ".git",
    ".github",
    "node_modules",
    ".npm",
    ".yarn",
    ".pnpm-store",
    ".next",
    ".nuxt",
    ".output",
    ".svelte-kit",
    "target",
    "vendor",
    ".idea",
    ".vscode",
    ".vs",
    "htmlcov",
    "coverage",
    ".coverage",
    ".nyc_output",
    "_build",
    "site",
    ".cache",
    ".tmp",
    "tmp",
    ".temp",
    "temp",
]


def get_exclude_dirs(config_raw: dict[str, Any] | None = None) -> list[str]:
    """Get directories to exclude from quality checks.

    Combines:
      1. Git submodule paths (from .gitmodules)
      2. ci/ and ai/ (always)
      3. Common directories (.venv, node_modules, target, etc.)
      4. Custom paths from quality.exclude_paths config
    """
    excludes: list[str] = []

    gitmodules = Path(".gitmodules")
    if gitmodules.exists():
        for line in gitmodules.read_text().splitlines():
            if "path" in line and "=" in line:
                path = line.split("=", 1)[1].strip()
                if path and Path(path).is_dir():
                    excludes.append(path)

    for submod in ("ci", "ai"):
        if Path(submod).is_dir() and submod not in excludes:
            excludes.append(submod)

    for dirname in _COMMON_EXCLUDES:
        if Path(dirname).exists() and dirname not in excludes:
            excludes.append(dirname)

    if config_raw:
        custom = config_raw.get("quality", {}).get("exclude_paths", [])
        if isinstance(custom, list):
            for path in custom:
                if path and Path(path).is_dir() and path not in excludes:
                    excludes.append(path)

    return excludes
