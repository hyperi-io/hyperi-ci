# Project:   HyperI CI
# File:      src/hyperi_ci/common.py
# Purpose:   Shared utilities for CI scripts (output, subprocess, exclusions)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Shared utilities for HyperI CI.

Logging goes through the scalo logger, which detects GitHub Actions, CI and
terminal output. The rest wraps subprocess and curl.
"""

import codecs
import contextlib
import errno
import fnmatch
import functools
import http.client
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from scalo.logger import logger

# Initialise logger for CI use (auto-detects GH Actions, CI, terminal)
from scalo.logger import setup as _setup_logger
from scalo.logger.scrub import ScrubConfig, SecretsConfig

from hyperi_ci.repo_path import RepoPathError, confine

if TYPE_CHECKING:
    from hyperi_ci.config import CIConfig

# scalo's default scrubber minus gitleaks' generic-api-key rule, which matches
# log prose containing the word "keys" and ate our own config warnings (#255).
SCRUB_CONFIG = ScrubConfig(
    secrets=SecretsConfig(exclude_rules=frozenset({"generic-api-key"}))
)

_setup_logger(ci_mode=None, scrub_config=SCRUB_CONFIG)


def sanitize_ref_name(ref: str) -> str:
    """Replace '/' in a git ref name with '-' so it can be an artifact filename."""
    return ref.replace("/", "-")


class ReleaseVersionError(ValueError):
    """The version a run would carry is not a version, so no stage may use it."""


# Semver as semantic-release and stamp-version write it: X.Y.Z, then an
# optional prerelease and build metadata.
_RELEASE_VERSION_RE = re.compile(
    r"\d+\.\d+\.\d+"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)


def _checked_version(raw: str, source: str) -> str:
    version = raw.removeprefix("v")
    if not _RELEASE_VERSION_RE.fullmatch(version):
        raise ReleaseVersionError(
            f"{source} holds {raw[:40]!r}, which is not a semver version"
        )
    return version


def resolve_release_version() -> str | None:
    """Resolve the version being released, the SSoT for every stage.

    Precedence (issue #27): ``HYPERCI_VERSION``, the Plan job's predicted
    ``next-version``, so every job in a run agrees. Then the ``VERSION`` file,
    for local runs only, as it is stale in CI. Then the latest tag. A leading
    ``v`` is stripped. Returns None when none is set (the caller decides whether
    that is fatal).

    Container, binary and registry publish all call this. Re-implementing the
    read per stage drifts (the GH release once shipped a stale tag that way).

    The value reaches image tags, labels and build args in a job holding
    registry logins, so it must be semver, and ``VERSION`` must be a regular
    file inside the checkout, or a symlink to ``~/.docker/config.json`` would
    reach a ``{version}`` build arg.

    Raises:
        ReleaseVersionError: The value is not semver, or ``VERSION`` is a
            symlink or resolves outside the working directory.

    """
    explicit = os.environ.get("HYPERCI_VERSION", "").strip()
    if explicit:
        return _checked_version(explicit, "HYPERCI_VERSION")
    version_file = Path("VERSION")
    if version_file.is_symlink():
        raise ReleaseVersionError("VERSION is a symlink, so it is not read")
    if version_file.exists():
        try:
            path = confine(version_file, Path.cwd(), key="VERSION")
        except RepoPathError as exc:
            raise ReleaseVersionError(str(exc)) from exc
        value = path.read_text(encoding="utf-8", errors="replace").strip()
        if value:
            return _checked_version(value, "VERSION")
    return latest_version_tag()


def latest_version_tag() -> str | None:
    """Highest final-release ``vX.Y.Z`` tag as a bare version, or None outside a repo.

    Last-resort fallback for a checkout with no ``VERSION`` file (issue #85).
    It is one behind mid-release, so callers that need the version being
    released read ``HYPERCI_VERSION``.
    """
    result = run_cmd(
        ["git", "tag", "--list", "v[0-9]*", "--sort=-v:refname"],
        capture=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    # Plain vX.Y.Z only: a prerelease sorts above its own release.
    for line in result.stdout.splitlines():
        candidate = line.strip().removeprefix("v")
        if _SEMVER_RE.match(candidate):
            return candidate
    return None


_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def newer_release_than(version: str | None) -> str | None:
    """Return the highest stable release when it is above ``version``, else None.

    Reads the local ``v*`` tags, so the checkout must carry them. None also
    covers a prerelease or unparseable ``version`` and a repo with no stable tag.
    """
    if not version:
        return None
    candidate = version.strip().removeprefix("v")
    if not _SEMVER_RE.match(candidate):
        return None
    highest = latest_version_tag()
    if highest is None:
        return None

    def key(value: str) -> tuple[int, ...]:
        return tuple(int(part) for part in value.split("."))

    return highest if key(highest) > key(candidate) else None


def holds_latest(version: str | None, pointer: str) -> bool:
    """Report whether ``pointer`` must stay on a newer release, and say why.

    Re-publishing an older tag (``hyperi-ci release v1.2.3``) republishes that
    version's own artefacts, but every ``latest`` pointer belongs to the newest
    stable release, or ``downloads.hyperi.io/<project>/latest/`` and ``:latest``
    go backwards.

    Args:
        version: Version being published, with or without a leading ``v``.
        pointer: What would move, for the log line (``"R2 latest/"``).

    Returns:
        True when a higher stable ``v*`` tag exists, so ``pointer`` stays put.

    """
    newer = newer_release_than(version)
    if newer is None:
        return False
    shown = (version or "").strip().removeprefix("v")
    warn(
        f"Leaving {pointer} on v{newer}: v{shown} is older than the newest "
        f"stable release. Only its versioned artefacts publish."
    )
    return True


def explicit_version(value: str | None) -> str | None:
    """Bare ``X.Y.Z`` if ``value`` is an explicit version, else None.

    The from-head ``bump`` input doubles as an override
    (``hyperi-ci publish --version X.Y.Z``): ``auto``/``patch``/``minor`` are
    bump levels, and a bare semver is tagged at HEAD verbatim. A leading ``v``
    is tolerated. Only plain ``X.Y.Z`` is accepted, as the tag is ``v${version}``.
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


def sudo_prefix() -> list[str]:
    """Return ``["sudo"]`` when non-root on Linux, else ``[]``.

    A Dockerfile ``RUN`` runs as root with no sudoers entry, so sudo there
    fails with 'root is not in the sudoers file'.
    """
    if not is_linux():
        return []
    return [] if os.geteuid() == 0 else ["sudo"]


_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def truthy(value: object) -> bool:
    """Return True when a config value reads as on (1/true/yes/on, any case)."""
    return str(value).strip().lower() in _TRUTHY


def env_true(name: str) -> bool:
    """Return True when env var ``name`` holds an opt-in value (1/true/yes/on)."""
    return truthy(os.environ.get(name, ""))


def optimize_tier() -> str:
    """The optimisation tier this run asked for, or ``""`` when it asked for none.

    Read from ``HYPERCI_OPTIMIZE_TIER``, which the reusable workflows set from
    their per-run ``optimize-tier`` input only. With no repo variable or config
    key the ask cannot outlive the run, as the release tier adds PGO and BOLT.
    A value other than ``release`` comes back as given, for the build stage to
    refuse.
    """
    return os.environ.get("HYPERCI_OPTIMIZE_TIER", "").strip().lower()


def skip_optimize(config: CIConfig | None = None) -> bool:
    """Whether this run drops the optimisation stage.

    Rust reads it as no PGO and no BOLT with Tier 1 (allocator + LTO) still
    applied, and a language with no optimisation stage ignores it.
    ``HYPERCI_SKIP_OPTIMIZE``, set by the reusable workflows from their
    ``skip-optimize`` input, beats ``build.skip_optimize``. It is read here
    because the ``HYPERCI_*`` config mapping splits on underscores and would
    land it at ``skip.optimize``.

    A run that asks for the release tier never skips, since it named PGO and
    BOLT for this run. Answering here keeps the build, the image label and the
    release notes in agreement.
    """
    if optimize_tier() == "release":
        if _skip_requested(config):
            warn(
                "optimize-tier=release overrides skip-optimize for this run -- "
                "PGO and BOLT run."
            )
        return False
    return _skip_requested(config)


def _skip_requested(config: CIConfig | None) -> bool:
    """Whether the skip-optimize input, variable or config key asks to skip."""
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

    A separate consent from ``skip_optimize``. Read from
    ``HYPERCI_RELEASE_UNOPTIMIZED`` only, which the reusable workflows set from
    their per-run ``release-unoptimized`` input, so the consent never outlives
    the run.
    """
    return env_true("HYPERCI_RELEASE_UNOPTIMIZED")


def is_prerelease_build() -> bool:
    """Whether this run ships a prerelease version rather than a stable one.

    ``HYPERCI_PRERELEASE``, set from the plan job's ``prerelease`` output,
    answers first, else the version being released does. Identity is separate
    from the optimisation tier (``HYPERCI_CHANNEL``), so a full release can be
    rehearsed on a prerelease version (issue #144).
    """
    from hyperi_ci.release_branches import is_prerelease_version

    raw = os.environ.get("HYPERCI_PRERELEASE", "").strip().lower()
    if raw in _TRUTHY or raw in _FALSY:
        return raw in _TRUTHY
    return is_prerelease_version(resolve_release_version())


# Prefixes the Actions runner reads as a workflow command, leading whitespace
# ignored.
_COMMAND_PREFIXES = ("::", "##[")

# CSI sequences (colour, cursor) and OSC sequences (hyperlinks) ended by BEL or ST.
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def strip_ansi(text: str) -> str:
    """Return ``text`` without terminal escape sequences.

    A tool forced into colour (``CARGO_TERM_COLOR=always``, ``PY_COLORS=1``,
    ``FORCE_COLOR``) writes them into captured output too, which breaks a parser
    matching line starts.
    """
    return _ANSI_ESCAPE.sub("", text)


def _inert(msg: str) -> str:
    """Return ``msg`` with no line the Actions runner would run as a command.

    Under GitHub Actions the logger writes every line after the first raw, and
    log text carries repo config and tool output. A line that would start a
    command gets a ``| `` prefix. Elsewhere the text is returned unchanged.
    """
    if not is_github_actions():
        return msg
    lines = msg.splitlines()
    if not any(line.lstrip().startswith(_COMMAND_PREFIXES) for line in lines):
        return msg
    return "\n".join(
        f"| {line}" if line.lstrip().startswith(_COMMAND_PREFIXES) else line
        for line in lines
    )


def info(msg: str) -> None:
    """Info message -- delegates to scalo logger."""
    logger.info(_inert(msg))


def success(msg: str) -> None:
    """Success message -- delegates to scalo logger."""
    logger.success(_inert(msg))


def warn(msg: str) -> None:
    """Warning -- delegates to scalo logger."""
    logger.warning(_inert(msg))


def error(msg: str) -> None:
    """Error -- delegates to scalo logger."""
    logger.error(_inert(msg))


@contextmanager
def group(title: str) -> Iterator[None]:
    """Collapsible group in GH Actions logs. No-op elsewhere."""
    # Flush: stdout is a pipe under CI, so an unflushed marker lands after a
    # child's output.
    if is_github_actions():
        print(f"::group::{title}", flush=True)
    try:
        yield
    finally:
        if is_github_actions():
            print("::endgroup::", flush=True)


def escape_command_data(value: str) -> str:
    """Percent-encode a value for the data half of a workflow command.

    The runner decodes whatever follows the ``::`` (``UnescapeData``), so
    anything not encoded arrives as a DIFFERENT string. ``%`` goes first or it
    would re-encode its own output, as in ``@actions/core``'s ``escapeData``.
    Encoding the line breaks also stops a newline closing the command and the
    remainder parsing as another.
    """
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def announce(
    msg: str,
    title: str,
    *,
    level: Literal["notice", "warning", "error"] = "warning",
) -> None:
    """Report once: an annotation under GitHub Actions, a log line elsewhere.

    The annotation reaches the run summary, where a folded log group cannot hide
    it, and is escaped because a raw newline ends a workflow command. It is the
    only output under GitHub Actions, as the logger would add a second
    annotation. Any other CI gets the log line, at info for a notice.
    """
    if is_github_actions():
        print(f"::{level} title={title}::{escape_command_data(msg)}", flush=True)
        return
    if level == "error":
        error(msg)
    elif level == "warning":
        warn(msg)
    else:
        info(msg)


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

    The stage-gate shape: ``false`` never runs, ``true`` always runs (and fails
    loudly when it can't), ``auto`` runs iff detection finds a signal. YAML
    gives a bool for ``true`` / ``false`` and a string for ``auto``.

    An unrecognised value warns, naming the key, and falls back to ``auto``, so
    a typo does not turn a build red.

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
        return "auto"
    # A bare `1`, float or list is a mistake: `producer: 1` reads as "on" to a
    # human.
    warn(f"Unknown {key} value {raw!r} -- falling back to 'auto'")
    return "auto"


@contextmanager
def scratch_dir(path: Path) -> Iterator[Path]:
    """Yield ``path`` as a directory, removed with everything in it afterwards."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def stage_tree(
    start: Path,
    root: Path,
    stage: Path,
    refs: Callable[[Path], list[Path]],
    ignore: Callable[[str, list[str]], Iterable[str]],
) -> Path:
    """Copy ``start`` and every path ``refs`` reaches from it under ``stage``.

    Each copy keeps its path relative to ``root``, so relative references
    resolve in the copy as they do in the tree. Returns the copy of ``start``.
    """
    base = root.resolve()

    def place(path: Path) -> Path:
        if path.is_relative_to(base):
            return stage / path.relative_to(base)
        return stage / "outside" / path.name

    queue, seen = [start.resolve()], set()
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        if current.is_dir():
            shutil.copytree(
                current,
                place(current),
                symlinks=True,
                dirs_exist_ok=True,
                ignore=ignore,
            )
            queue += refs(current)
        elif current.is_file():
            place(current).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(current, place(current))
    return place(start.resolve())


def run_cmd(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    stdin_text: str | None = None,
    merge_stderr: bool = False,
    memory_limit_bytes: int | None = None,
    own_group: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess with consistent error handling.

    Args:
        cmd: Command as list of strings.
        check: Raise CalledProcessError on non-zero exit.
        capture: Capture stdout/stderr instead of passing through.
        merge_stderr: With ``capture``, send stderr down the stdout pipe in
            write order, leaving ``stderr`` None.
        cwd: Working directory.
        env: Additional env vars (merged with os.environ).
        timeout: Seconds before the child is killed and
            ``subprocess.TimeoutExpired`` raised. None waits for it to exit.
        own_group: With ``timeout``, on POSIX, kill the child's whole process
            group on timeout or interrupt, so a wrapper's children
            (``uvx checkov``) cannot outlive it.
        stdin_text: Text written to the child's stdin, which is then closed.
            The way to hand a child a secret, as any process can read argv from
            ``/proc/<pid>/cmdline``. None leaves stdin inherited.
        memory_limit_bytes: ``RLIMIT_AS`` for the child, Linux only, and
            ``MALLOC_ARENA_MAX=2`` unless ``env`` names it. For Python tools
            only, as a Go runtime reserves far more address space than it uses.

    Returns:
        CompletedProcess with text output.

    Raises:
        ValueError: ``merge_stderr`` without ``capture``.

    """
    if merge_stderr and not capture:
        raise ValueError("run_cmd: merge_stderr needs capture=True")

    preexec = None
    if memory_limit_bytes is not None and sys.platform == "linux":
        import resource

        limit = (memory_limit_bytes, memory_limit_bytes)
        preexec = functools.partial(resource.setrlimit, resource.RLIMIT_AS, limit)
        env = {"MALLOC_ARENA_MAX": "2", **(env or {})}

    run_env = None
    if env:
        run_env = {**os.environ, **env}

    stdout = stderr = None
    if capture:
        stdout = subprocess.PIPE
        stderr = subprocess.STDOUT if merge_stderr else subprocess.PIPE

    if own_group and timeout is not None and os.name == "posix":
        with subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if stdin_text is not None else None,
            stdout=stdout,
            stderr=stderr,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            env=run_env,
            preexec_fn=preexec,
            start_new_session=True,
        ) as proc:
            try:
                out, err = proc.communicate(stdin_text, timeout=timeout)
            except BaseException:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
                raise
        if check and proc.returncode:
            raise subprocess.CalledProcessError(proc.returncode, cmd, out, err)
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)

    return subprocess.run(
        cmd,
        check=check,
        stdout=stdout,
        stderr=stderr,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        env=run_env,
        timeout=timeout,
        input=stdin_text,
        preexec_fn=preexec,
    )


# Any 5xx is retried, and of the 4xx only these, which mean "ask again later".
_RETRY_STATUSES = frozenset({408, 429})


def _status_is_final(code: int) -> bool:
    """Whether asking again cannot change an HTTP error status."""
    return code < 500 and code not in _RETRY_STATUSES


def backoff(retry: int) -> float:
    """Seconds to wait before retry number ``retry``.

    About 1, 2, 4 and so on, each cut by up to half at random so parallel jobs
    do not retry in step.
    """
    return random.uniform(0.5, 1.0) * 2 ** (retry - 1)  # noqa: S311 -- retry jitter, not a cryptographic use


# With backoff() this spans about 1+2+...+64 seconds, long enough to ride out a
# GitHub release-download 504 burst.
_CURL_RETRIES = 7
# No retry starts once this many seconds have passed since the first attempt.
_CURL_RETRY_MAX_TIME = 600
_CURL_CONNECT_TIMEOUT = 10
_CURL_MAX_TIME = 180
# Seconds past --max-time before the process is killed.
_CURL_BACKSTOP = 20
# curl exit codes: an HTTP status of 400 or more under -f, and a transfer past
# --max-time.
_CURL_HTTP_ERROR = 22
_CURL_TIMED_OUT = 28


def _redact_url(url: str) -> str:
    """Return ``url`` without its ``user:password@``, for a log line.

    A URL too malformed to split is replaced whole.
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "<unparseable URL>"
    if "@" not in parts.netloc:
        return url
    host = parts.netloc.rpartition("@")[2]
    return urllib.parse.urlunsplit(parts._replace(netloc=host))


def _curl_attempt(cmd: list[str], max_time: int) -> subprocess.CompletedProcess[str]:
    """Run one curl attempt, reporting a backstop kill as curl's own timeout."""
    try:
        return run_cmd(
            cmd, check=False, capture=True, timeout=max_time + _CURL_BACKSTOP
        )
    except subprocess.TimeoutExpired:
        reason = (
            f"curl outlived --max-time {max_time} "
            f"and was killed {_CURL_BACKSTOP}s later"
        )
        return subprocess.CompletedProcess(
            cmd, _CURL_TIMED_OUT, stdout="", stderr=reason
        )


def _curl_retryable(result: subprocess.CompletedProcess[str]) -> bool:
    """Whether a failed curl attempt is worth making again.

    Every failure is, except an HTTP status that asking again cannot change.
    An HTTP error with no readable status is retried.
    """
    status = (result.stdout or "").strip()
    if result.returncode != _CURL_HTTP_ERROR or not status.isdigit():
        return True
    return not _status_is_final(int(status))


def curl_fetch(
    url: str,
    dest: Path,
    *,
    extra: Sequence[str] = (),
    follow_redirects: bool = True,
    max_time: int = _CURL_MAX_TIME,
) -> subprocess.CompletedProcess[str]:
    """Download ``url`` to ``dest`` with curl, retrying a transient failure.

    Every Python-side fetch goes through here, and a test fails on a fetching
    curl argv anywhere else. Each attempt is one curl process, with the body in
    the ``-o`` file and only the HTTP status on stdout.

    A 5xx, 408 or 429, a transfer cut off mid-body, a timeout or any other curl
    failure is retried up to 7 times with :func:`backoff` waits. No retry starts
    more than 600s after the first attempt. Any other HTTP status is final,
    because asking again gets the same answer and spends rate limit.

    A curl that outlives its own ``--max-time`` is killed 20 seconds later and
    counts as a timeout (exit 28). ``-f`` makes an HTTP error a non-zero exit
    rather than a saved error page. Each failed attempt logs curl's error line
    after the URL with any ``user:password@`` removed.

    Args:
        url: What to fetch.
        dest: File the body is written to.
        extra: More curl options, placed before the URL.
        follow_redirects: Pass ``-L``.
        max_time: Seconds one attempt may take in all, the connect included.
            The curl process is killed 20 seconds after that.

    Returns:
        The last curl attempt. Check its return code.

    Raises:
        OSError: curl could not be started.

    """
    cmd = [
        "curl",
        "-fsS",
        *(["-L"] if follow_redirects else []),
        "--connect-timeout",
        str(_CURL_CONNECT_TIMEOUT),
        "--max-time",
        str(max_time),
        "-w",
        "%{http_code}",
        *extra,
        "-o",
        str(dest),
        url,
    ]
    shown = _redact_url(url)
    started = time.monotonic()
    retry = 0
    while True:
        result = _curl_attempt(cmd, max_time)
        if result.returncode == 0:
            return result
        reason = (result.stderr or "").strip() or f"curl exit {result.returncode}"
        delay = backoff(retry + 1)
        # The wait before a retry counts toward the window.
        retry_starts = time.monotonic() - started + delay
        if (
            retry == _CURL_RETRIES
            or retry_starts >= _CURL_RETRY_MAX_TIME
            or not _curl_retryable(result)
        ):
            info(f"{shown}: {reason}")
            return result
        retry += 1
        info(
            f"{shown}: {reason}, retrying in {delay:.1f}s "
            f"(retry {retry} of {_CURL_RETRIES})"
        )
        time.sleep(delay)


def curl_read(
    url: str,
    *,
    extra: Sequence[str] = (),
    follow_redirects: bool = True,
    max_time: int = _CURL_MAX_TIME,
) -> tuple[int, bytes]:
    """Fetch ``url`` into memory through a temp file, removed before returning.

    For a caller that needs the bytes. The arguments are those of
    :func:`curl_fetch`.

    Args:
        url: What to fetch.
        extra: More curl options, placed before the URL.
        follow_redirects: Pass ``-L``.
        max_time: Seconds one attempt may take in all.

    Returns:
        curl's exit code and the body. The body is empty unless curl exited 0.

    Raises:
        OSError: curl could not be started, or the temp file failed.

    """
    with tempfile.TemporaryDirectory(prefix="hyperi-ci-fetch-") as scratch:
        dest = Path(scratch) / "body"
        result = curl_fetch(
            url,
            dest,
            extra=extra,
            follow_redirects=follow_redirects,
            max_time=max_time,
        )
        # A curl that exits 0 without writing the file reads as an empty body.
        ok = result.returncode == 0 and dest.is_file()
        body = dest.read_bytes() if ok else b""
    return result.returncode, body


def download_artefact(name: str, url: str) -> bytes | None:
    """Fetch a release artefact into memory, or log why not and return None.

    Args:
        name: What is being fetched, for the error line.
        url: Where from.

    Returns:
        The body, or None on any failure, an empty body included.

    """
    try:
        rc, body = curl_read(url)
    except OSError as exc:
        error(f"Failed to download {name} ({exc})")
        return None
    if rc != 0 or not body:
        error(f"Failed to download {name} (curl exit {rc})")
        return None
    return body


URL_ATTEMPTS = 4

# HTTPError and URLError are both OSError, and a truncated response raises an
# HTTPException.
URL_ERRORS = (OSError, http.client.HTTPException)


def _url_read_once(request: urllib.request.Request, timeout: float) -> bytes:
    with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310  # nosec B310  # nosemgrep: dynamic-urllib-use-detected -- callers pass fixed https URLs
        return resp.read()


def url_read(
    request: urllib.request.Request,
    *,
    timeout: float,
    attempts: int = URL_ATTEMPTS,
) -> bytes:
    """Open ``request`` with urllib and return the body, retrying a transient failure.

    A 5xx, 408 or 429, a timeout, a reset connection or any other socket error
    is retried with :func:`backoff` waits. Any other HTTP status raises at once.

    Nothing bounds the whole call: ``timeout`` limits each socket operation, so
    a server that trickles its reply can hold an attempt far longer, and a hung
    DNS lookup is not limited at all.

    Args:
        request: What to open. The caller vouches for the URL.
        timeout: Seconds any one socket operation may block, such as the
            connect or a single read.
        attempts: Tries in total. 1 turns retrying off.

    Returns:
        The response body, empty for a HEAD request.

    Raises:
        urllib.error.HTTPError: A status that retrying cannot change.
        OSError: The last failure, once every attempt is spent.
        http.client.HTTPException: The same, for a malformed or truncated reply.

    """
    for attempt in range(1, attempts):
        try:
            return _url_read_once(request, timeout)
        except urllib.error.HTTPError as exc:
            if _status_is_final(exc.code):
                raise
            exc.close()
            reason = f"HTTP {exc.code}"
        except URL_ERRORS as exc:
            reason = str(exc) or type(exc).__name__
        delay = backoff(attempt)
        info(
            f"{request.full_url}: {reason}, retrying in {delay:.1f}s "
            f"(retry {attempt} of {attempts - 1})"
        )
        time.sleep(delay)
    return _url_read_once(request, timeout)


def _log_line(line: str) -> None:
    info(f"  {line}")


def echo_chunk(text: str) -> None:
    """Pass a piece of a child's output through unchanged, as it arrives.

    For a ``stream_cmd`` whose child's output is the report, such as a test
    runner's: the logger would prefix every line, and waiting for a newline would
    hold back the id of a test that hangs mid-line.
    """
    sys.stdout.write(text)
    sys.stdout.flush()


# Bounded so a test that logs without end cannot exhaust a 4 GB runner.
STREAM_TAIL_CHARS = 64 * 1024

_STREAM_READ_BYTES = 64 * 1024

# A grandchild that inherited the pipe can hold it open indefinitely.
_STREAM_GIVE_UP_SECONDS = 10.0


class _StreamReader:
    """Drain a child's output pipe, feeding the sinks and keeping a bounded tail.

    A sink that raises (a closed stdout under ``| head``) must not stop the
    draining, since an undrained pipe fills and blocks the child. The first such
    exception is kept for the caller to raise once the child has exited.

    It reads its own duplicate of ``fd`` and closes it only at EOF, so it can
    never read a descriptor number the process has since reused.

    A failed read ends the drain. EIO is how a pty reports its writer has gone,
    so it reads as EOF, and any other error is kept in ``read_error``.
    """

    def __init__(
        self,
        fd: int,
        on_line: Callable[[str], None] | None,
        on_chunk: Callable[[str], None] | None,
        tail_chars: int,
    ) -> None:
        self._fd = os.dup(fd)
        self._on_line = on_line
        self._on_chunk = on_chunk
        self._tail_chars = tail_chars
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._partial = ""
        self._tail = ""
        self._lock = threading.Lock()
        self.sink_error: BaseException | None = None
        self.read_error: OSError | None = None

    def _call(self, sink: Callable[[str], None] | None, text: str) -> None:
        if sink is None or self.sink_error is not None:
            return
        try:
            sink(text)
        except Exception as exc:  # noqa: BLE001 - kept and re-raised by stream_cmd
            self.sink_error = exc

    def _feed(self, text: str) -> None:
        if not text:
            return
        self._call(self._on_chunk, text)
        with self._lock:
            self._tail = (self._tail + text)[-self._tail_chars :]
        # Only the last piece can be an unfinished line.
        pieces = (self._partial + text).split("\n")
        self._partial = pieces.pop()
        if len(self._partial) > self._tail_chars:
            self._partial = self._partial[-self._tail_chars :]
        for line in pieces:
            self._call(self._on_line, line.removesuffix("\r"))

    def _read(self) -> bytes:
        """Return the next chunk of output, or empty bytes at EOF."""
        return os.read(self._fd, _STREAM_READ_BYTES)

    def run(self) -> None:
        """Read until EOF or a failed read, then hand over any unterminated line."""
        try:
            while True:
                try:
                    chunk = self._read()
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        self.read_error = exc
                    break
                if not chunk:
                    break
                self._feed(self._decoder.decode(chunk))
        finally:
            os.close(self._fd)
        self._feed(self._decoder.decode(b"", final=True))
        if self._partial:
            self._call(self._on_line, self._partial.removesuffix("\r"))
            self._partial = ""

    def tail(self) -> str:
        """Return the retained end of the output, without its final newline."""
        with self._lock:
            return self._tail.removesuffix("\n").removesuffix("\r")


def stream_cmd(
    cmd: list[str],
    *,
    on_line: Callable[[str], None] | None = _log_line,
    on_chunk: Callable[[str], None] | None = None,
    on_heartbeat: Callable[[float, int], None] | None = None,
    heartbeat_seconds: float = 30.0,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Run a subprocess, handing over its output as it arrives.

    For a step whose silence is the symptom: a hung process leaves everything it
    printed, and ``on_heartbeat`` fires on an interval so the log says how long
    it has been going (issue #261).

    Args:
        cmd: Command as list of strings.
        on_line: Called with each complete line of combined stdout and stderr,
            and with a final unterminated one at exit. None skips it.
        on_chunk: Called with each piece of output as it is read, before any
            newline arrives, for a caller that passes the output through.
        on_heartbeat: Called with elapsed seconds and the child's pid every
            ``heartbeat_seconds`` until the process exits.
        heartbeat_seconds: Interval between heartbeats.
        cwd: Working directory.
        env: Additional env vars for the child (merged with os.environ).

    Returns:
        The exit code and the last :data:`STREAM_TAIL_CHARS` characters of the
        combined output. A caller that needs something from earlier collects
        it in ``on_line``.

    Raises:
        OSError: The command could not be started (``FileNotFoundError`` or
            ``PermissionError``).
        Exception: The first exception ``on_line`` or ``on_chunk`` raised,
            re-raised once the child has exited. The pipe is drained regardless.

    """
    run_env = {**os.environ, **env} if env else None
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=cwd,
        env=run_env,
    )
    if proc.stdout is None:
        raise OSError(f"no output pipe for {cmd[0]}")
    drain = _StreamReader(proc.stdout.fileno(), on_line, on_chunk, STREAM_TAIL_CHARS)
    proc.stdout.close()
    reader = threading.Thread(target=drain.run, daemon=True)
    reader.start()
    started = time.monotonic()
    while True:
        try:
            returncode = proc.wait(timeout=heartbeat_seconds)
            break
        except subprocess.TimeoutExpired:
            if on_heartbeat is not None:
                on_heartbeat(time.monotonic() - started, proc.pid)

    reader.join(timeout=_STREAM_GIVE_UP_SECONDS)
    if drain.sink_error is not None:
        raise drain.sink_error
    if drain.read_error is not None:
        warn(f"  output of {cmd[0]} was cut short: {drain.read_error}")
    return returncode, drain.tail()


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


# Searching these for a bare exclude name would cost more than the lookup is worth.
_NAME_SEARCH_SKIP = frozenset(
    {".git", ".worktrees", "node_modules", ".venv", "venv", "target"}
)


def _unmatched_names(root: Path, names: set[str]) -> set[str]:
    """Return the names (or name globs) that match no directory under ``root``."""
    missing = set(names)
    for _dirpath, dirnames, _filenames in root.walk():
        for dirname in dirnames:
            missing = {n for n in missing if not fnmatch.fnmatchcase(dirname, n)}
        if not missing:
            break
        dirnames[:] = [d for d in dirnames if d not in _NAME_SEARCH_SKIP]
    return missing


@functools.cache
def _note_unmatched_excludes(entries: tuple[str, ...], root: Path) -> None:
    """Report each ``quality.exclude_paths`` entry that excludes nothing.

    Info, not a warning, because an entry may guard a directory that exists on
    some checkouts only. Cached so it appears once per process.
    """
    prefix = "quality.exclude_paths:"
    names = {e for e in entries if "/" not in e and not (root / e).is_dir()}
    for name in sorted(_unmatched_names(root, names)):
        info(f"{prefix} no directory named '{name}', so it excludes nothing")
    for entry in entries:
        if "/" in entry and not (root / entry).is_dir():
            info(f"{prefix} '{entry}' is not a directory, so it excludes nothing")


def get_exclude_dirs(config_raw: dict[str, Any] | None = None) -> list[str]:
    """Get directories to exclude from quality checks.

    Combines git submodule paths (.gitmodules), ci/ and ai/, the common
    directories (.venv, node_modules, target, etc.) and the
    ``quality.exclude_paths`` entries with any trailing ``/`` stripped. An entry
    with a ``/`` is a path from the repo root, kept only if it is a directory.
    A bare name is kept regardless, as consumers match it against a directory
    name at any depth.

    A custom entry that excludes nothing is reported once per process.
    """
    excludes: list[str] = []

    gitmodules = Path(".gitmodules")
    if gitmodules.exists():
        for line in gitmodules.read_text(encoding="utf-8").splitlines():
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
            stripped = (str(entry).rstrip("/") for entry in custom if entry)
            entries = tuple(entry for entry in stripped if entry)
            _note_unmatched_excludes(entries, Path.cwd())
            for entry in entries:
                keep = "/" not in entry or Path(entry).is_dir()
                if keep and entry not in excludes:
                    excludes.append(entry)

    return excludes
