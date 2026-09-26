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

import fnmatch
import functools
import http.client
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

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
        value = version_file.read_text(encoding="utf-8").strip()
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


def optimize_tier() -> str:
    """The optimisation tier this run asked for, or ``""`` when it asked for none.

    Read from ``HYPERCI_OPTIMIZE_TIER``, which the reusable workflows set from
    their per-run ``optimize-tier`` input only. There is no repo variable and
    no config key: the release tier adds PGO and BOLT to every build it
    reaches, so the ask must not outlive the run. A value other than
    ``release`` comes back as given, for the build stage to refuse.
    """
    return os.environ.get("HYPERCI_OPTIMIZE_TIER", "").strip().lower()


def skip_optimize(config: CIConfig | None = None) -> bool:
    """Whether this run drops the optimisation stage.

    Language-agnostic: Rust reads it as no PGO and no BOLT with Tier 1
    (allocator + LTO) still applied, and a language with no optimisation
    stage ignores it. ``HYPERCI_SKIP_OPTIMIZE``, set by the reusable
    workflows from their ``skip-optimize`` input, beats ``build.skip_optimize``.
    It is read here rather than through the ``HYPERCI_*`` config mapping,
    which splits on underscores and would land it at ``skip.optimize``.

    A run that asks for the release tier never skips. It named PGO and BOLT
    for this run, which beats a repo-wide skip, and answering here keeps the
    build, the image label and the release notes in agreement.
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


# A line the Actions runner reads as a workflow command starts with one of
# these, leading whitespace ignored.
_COMMAND_PREFIXES = ("::", "##[")


def _inert(msg: str) -> str:
    """Return ``msg`` with no line the Actions runner would run as a command.

    Log text carries repo config and tool output, and under GitHub Actions the
    logger writes every line after the first raw. A line that would start a
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


def fatal(msg: str) -> None:
    """Fatal error -- log and exit with code 1."""
    logger.critical(_inert(msg))
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


def announce(
    msg: str, title: str, *, level: Literal["warning", "error"] = "warning"
) -> None:
    """Report once: an annotation under GitHub Actions, a log line elsewhere.

    The annotation reaches the run summary, where a folded log group cannot
    hide it, and it is escaped because a raw newline ends a workflow command.
    Under GitHub Actions it is the only output, because the logger would add a
    second annotation. Any other CI reads no workflow commands, so it gets the
    log line.
    """
    if is_github_actions():
        print(f"::{level} title={title}::{escape_command_data(msg)}", flush=True)
        return
    if level == "error":
        error(msg)
    else:
        warn(msg)


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
    stdin_text: str | None = None,
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
        stdin_text: Text written to the child's stdin, which is then closed.
            The way to hand a child a secret: argv is readable by any process
            on the host through ``/proc/<pid>/cmdline``, stdin is not. None
            leaves stdin inherited.

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
        input=stdin_text,
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
    return random.uniform(0.5, 1.0) * 2 ** (retry - 1)


_CURL_RETRIES = 5
# No retry starts once this many seconds have passed since the first attempt.
_CURL_RETRY_MAX_TIME = 600
_CURL_CONNECT_TIMEOUT = 10
_CURL_MAX_TIME = 180
# Seconds past --max-time before the process is killed, so it only fires on a
# curl that has stopped honouring its own limit.
_CURL_BACKSTOP = 20
# curl's exit code for an HTTP status of 400 or more under -f.
_CURL_HTTP_ERROR = 22
# curl's exit code for a transfer that ran past --max-time.
_CURL_TIMED_OUT = 28


def _redact_url(url: str) -> str:
    """Return ``url`` without its ``user:password@``, for a log line.

    A URL too malformed to split is replaced whole, because where its
    credentials end cannot be told.
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
    An HTTP error with no readable status is retried rather than guessed at.
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
    curl argv anywhere else. Each attempt is one curl process. The body goes
    to the ``-o`` file and stdout carries only the HTTP status, which decides
    whether the attempt is made again.

    A 5xx, 408 or 429, a transfer cut off mid-body, a timeout or any other
    curl failure is retried up to 5 times, after about 1s, 2s, 4s, 8s and
    16s, each wait cut by up to half at random. No retry starts more than
    600s after the first attempt did. Any other HTTP status is final, because
    asking again gets the same answer and spends another request against a
    rate limit.

    A curl that outlives its own ``--max-time`` is killed 20 seconds later,
    and the attempt counts as a timeout, curl's exit 28.

    ``-f`` turns an HTTP error into a non-zero exit rather than a saved error
    page. curl's error line is logged for each failed attempt, after the URL
    with any ``user:password@`` removed.

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
        # The window bounds when a retry starts, so the wait before it counts.
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

    For a caller that needs the bytes, or pipes them into a shell. The
    arguments are those of :func:`curl_fetch`.

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

# What a failed urllib request raises: HTTPError and URLError are both OSError,
# and a response cut short raises an HTTPException.
URL_ERRORS = (OSError, http.client.HTTPException)


def _url_read_once(request: urllib.request.Request, timeout: float) -> bytes:
    with urllib.request.urlopen(request, timeout=timeout) as resp:  # nosec B310  # nosemgrep: dynamic-urllib-use-detected -- callers pass fixed https URLs
        return resp.read()


def url_read(
    request: urllib.request.Request,
    *,
    timeout: float,
    attempts: int = URL_ATTEMPTS,
) -> bytes:
    """Open ``request`` with urllib and return the body, retrying a transient failure.

    A 5xx, 408 or 429, a timeout, a reset connection or any other socket error
    is tried again after about 1s, 2s, then 4s. Each wait is cut by up to half
    at random so parallel jobs do not retry in step. Any other HTTP status
    raises at once, because asking again gets the same answer.

    Nothing here bounds how long a whole call takes. ``timeout`` limits each
    socket operation, so a server that trickles its reply can hold an attempt
    far longer, and a hung DNS lookup is not limited at all.

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
def _warn_unmatched_excludes(entries: tuple[str, ...], root: Path) -> None:
    """Warn about each ``quality.exclude_paths`` entry that excludes nothing.

    Cached so the warning appears once per process, however many stages ask
    for the exclude list.
    """
    prefix = "quality.exclude_paths:"
    names = {e for e in entries if "/" not in e and not (root / e).is_dir()}
    for name in sorted(_unmatched_names(root, names)):
        warn(f"{prefix} no directory named '{name}', so it excludes nothing")
    for entry in entries:
        if "/" in entry and not (root / entry).is_dir():
            warn(f"{prefix} '{entry}' is not a directory, so it excludes nothing")


def get_exclude_dirs(config_raw: dict[str, Any] | None = None) -> list[str]:
    """Get directories to exclude from quality checks.

    Combines:
      1. Git submodule paths (from .gitmodules)
      2. ci/ and ai/ (always)
      3. Common directories (.venv, node_modules, target, etc.)
      4. Custom entries from quality.exclude_paths config. An entry with a
         ``/`` is a path from the repo root, kept only if it is a directory.
         A bare name is kept regardless, since the consumers match it
         against a directory name at any depth.

    A custom entry that excludes nothing is warned about once per process.
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
            entries = tuple(str(entry) for entry in custom if entry)
            _warn_unmatched_excludes(entries, Path.cwd())
            for entry in entries:
                keep = "/" not in entry or Path(entry).is_dir()
                if keep and entry not in excludes:
                    excludes.append(entry)

    return excludes
