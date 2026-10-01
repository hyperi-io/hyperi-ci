# Project:   HyperI CI
# File:      src/hyperi_ci/quality/osv_scanner.py
# Purpose:   Malicious-package scanning via osv-scanner (Rust + TS gap)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""osv-scanner helper: the malicious-package (``MAL-*``) detection layer.

cargo-audit (RustSec) and npm/pnpm audit (GitHub Advisory DB) cover
*known vulnerabilities* but NOT the OSSF malicious-packages feed.
osv-scanner reads that feed (the one ossf/malicious-packages amends),
so it closes the typosquat / compromised-maintainer gap for Rust and
TypeScript. Python is already covered (pip-audit queries OSV directly).

It is defence-in-depth behind the Renovate 7-day cooldown, so it runs
at ``warn`` by default: the same OSV feed periodically ships false-
positive waves (see ossf/malicious-packages#1276), and a blocking gate
on a feed that misfires would red the build on legitimate packages.
True positives are acted on; known false positives are suppressed via
``quality.ignore`` (which generates this scanner's native
``[[IgnoredVulns]]`` config, with optional auto-expiry).

A repo can also carry its own ``osv-scanner.toml`` beside the lockfile.
osv-scanner reads that file only when no ``--config`` is given, so the
generated ignores are appended to a copy of it rather than replacing it.
"""

import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Iterable
from pathlib import Path

from hyperi_ci.common import error, info, is_ci, run_cmd, success, warn
from hyperi_ci.quality.ignores import IgnoreEntry
from hyperi_ci.tools import missing_tool_notice, warn_on_pin_drift

SLUG = "osv-scanner"
_BINARY = "osv-scanner"
_CONFIG_NAME = "osv-scanner.toml"

# osv-scanner v2 exit codes, from cmd/osv-scanner/internal/cmd/run.go.
_FINDINGS_EXIT = 1
_ERRORED_EXIT = 127
_NO_PACKAGES_EXIT = 128
_API_FAILED_EXIT = 129

# v2.6.0 never returns ErrAPIFailed, so a failed osv.dev query exits 127 with
# this plugin named in the logged error.
_OSV_DEV_MATCHER = "vulnmatch/osvdev"


class ConfigMergeError(ValueError):
    """A repo's own osv-scanner.toml cannot carry the generated ignores."""


def available() -> bool:
    """Return True if the osv-scanner binary is on PATH."""
    return shutil.which(_BINARY) is not None


def _toml_escape(value: str) -> str:
    """Escape a string for a TOML basic (double-quoted) string."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def render_ignore_config(entries: Iterable[IgnoreEntry]) -> str:
    """Render an ``osv-scanner.toml`` body from ignore entries.

    Each entry becomes an ``[[IgnoredVulns]]`` block. ``expires`` maps
    to osv-scanner's native ``ignoreUntil`` (RFC3339), so a suppression
    self-clears once the date passes - belt-and-braces with the
    framework-level drop in ``load_ignores``.

    Returns:
        TOML text (empty string when there are no entries).

    """
    blocks: list[str] = []
    for e in entries:
        lines = ["[[IgnoredVulns]]", f'id = "{_toml_escape(e.id)}"']
        if e.expires is not None:
            lines.append(f"ignoreUntil = {e.expires.isoformat()}T00:00:00Z")
        lines.append(f'reason = "{_toml_escape(e.reason)}"')
        blocks.append("\n".join(lines))
    if not blocks:
        return ""
    return "\n\n".join(blocks) + "\n"


def repo_config_path(lockfile: Path) -> Path:
    """Return the config osv-scanner reads for ``lockfile`` when not given one.

    osv-scanner v2 looks only in the lockfile's own directory, never a parent
    (``normalizeConfigLoadPath`` in ``internal/config/manager.go``).
    """
    return lockfile.parent / _CONFIG_NAME


def merge_config(
    repo_text: str, entries: Iterable[IgnoreEntry]
) -> tuple[str, list[str]]:
    """Append generated ``[[IgnoredVulns]]`` blocks to a repo's own config.

    The repo text is kept verbatim, so every setting it carries survives:
    ``PackageOverrides``, ``GoVersionOverride``, comments and all. On a
    duplicate id the repo's entry wins and the generated one is dropped.
    osv-scanner would honour only the first of two entries anyway and warn
    about the second, and the repo file is what a hand-run osv-scanner reads.

    Args:
        repo_text: The repo's ``osv-scanner.toml``, or ``""`` when it has none.
        entries: Ignores from ``quality.ignore`` and ``deny.toml``.

    Returns:
        The merged TOML text, and the generated ids the repo already ignores.

    Raises:
        ConfigMergeError: The repo file is not valid TOML, or it declares
            ``IgnoredVulns`` in a form an appended block cannot extend.

    """
    try:
        repo = tomllib.loads(repo_text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigMergeError(f"not valid TOML: {exc}") from exc

    repo_ignores = repo.get("IgnoredVulns", [])
    if not isinstance(repo_ignores, list):
        raise ConfigMergeError("IgnoredVulns is not an array of tables")
    repo_ids = {v.get("id") for v in repo_ignores if isinstance(v, dict)}

    added: list[IgnoreEntry] = []
    shadowed: list[str] = []
    for entry in entries:
        if entry.id in repo_ids:
            shadowed.append(entry.id)
        else:
            added.append(entry)

    head = repo_text
    if head and not head.endswith("\n"):
        head += "\n"
    generated = render_ignore_config(added)
    merged = head + ("\n" if head and generated else "") + generated

    # An inline `IgnoredVulns = [...]` array cannot be extended by a table header.
    try:
        tomllib.loads(merged)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigMergeError(
            f"cannot append [[IgnoredVulns]] blocks to it: {exc}"
        ) from exc
    return merged, shadowed


def build_command(lockfile: Path, config_path: Path | None = None) -> list[str]:
    """Compose the osv-scanner CLI invocation for a single lockfile.

    osv-scanner v2 scans a named lockfile via ``scan source --lockfile``;
    ``--config`` points at the generated ignore config.
    """
    cmd = [_BINARY, "scan", "source", "--lockfile", str(lockfile)]
    if config_path is not None:
        cmd += ["--config", str(config_path)]
    return cmd


def _not_scanned(why: str, mode: str) -> None:
    """Report a scan that checked no package, which is not a clean result."""
    warn(
        f"  {SLUG}: NOT SCANNED - {why}, so no package was checked. "
        f"This is not a clean result."
    )
    if is_ci() and mode == "blocking":
        print(
            f"::warning title=osv-scanner scanned nothing::{SLUG} is "
            f"{mode} but {why}, so it gated nothing here."
        )


def _osv_dev_unreachable(result: subprocess.CompletedProcess[str]) -> bool:
    """Whether the scan read the lockfile but could not query osv.dev."""
    if result.returncode == _API_FAILED_EXIT:
        return True
    return result.returncode == _ERRORED_EXIT and _OSV_DEV_MATCHER in (
        result.stderr or ""
    )


def _scan(lockfile: Path, config_path: Path | None, mode: str) -> bool:
    """Run one scan and apply the warn / blocking semantics to its exit code.

    Only exit 1 is a finding. An osv.dev outage passes in both modes, reported
    as NOT SCANNED, the same policy as cargo-audit's unreachable advisory
    database: the repo cannot fix it, and the Renovate cooldown still stands.
    Any other scanner error keeps the mode's outcome but is not called a finding.
    """
    result = run_cmd(build_command(lockfile, config_path), check=False, capture=True)
    code = result.returncode
    if code == 0:
        success(f"  {SLUG}: passed")
        return True

    if code == _NO_PACKAGES_EXIT:
        _not_scanned(f"{lockfile.name} lists no packages", mode)
        return True

    if _osv_dev_unreachable(result):
        _not_scanned(f"osv.dev could not be queried (exit {code})", mode)
        if result.stderr:
            info(result.stderr)
        return True

    if code == _FINDINGS_EXIT:
        outcome = "issues found"
    else:
        outcome = f"scanner error (exit {code}), not a finding"

    if mode == "warn":
        warn(f"  {SLUG}: {outcome} (non-blocking)")
    else:
        error(f"  {SLUG}: failed - {outcome}")
    for stream in (result.stdout, result.stderr):
        if stream:
            info(stream)
    return mode == "warn"


def run(lockfile: Path, entries: Iterable[IgnoreEntry], mode: str) -> bool:
    """Run osv-scanner against ``lockfile``.

    A missing binary fails a ``blocking`` scan in CI and warn-skips
    everywhere else, like every other quality tool. A missing lockfile, one
    that lists no packages, or an osv.dev that cannot be queried is reported
    as NOT SCANNED and passes.

    When ignore entries are present, the repo's own ``osv-scanner.toml``
    (if any) is merged with them into a file in a temporary directory, the
    scanner is pointed at it, and it is removed after the scan.

    Returns:
        True on pass, skip, NOT SCANNED or a ``warn``-mode finding; False
        when a ``blocking`` scan found something or errored, could not run
        in CI, or the repo's config could not be merged.

    """
    if mode == "disabled":
        info(f"  {SLUG}: disabled")
        return True

    if not available():
        notice = missing_tool_notice(_BINARY)
        if mode == "blocking" and is_ci():
            error(notice)
            return False
        warn(notice)
        return True

    warn_on_pin_drift(_BINARY)

    if not lockfile.exists():
        # A library legitimately ships no lockfile, so this is not a failure,
        # but it is not coverage either (issue #223).
        _not_scanned(f"there is no {lockfile.name} in this repo", mode)
        return True

    entries = list(entries)
    if not entries:
        return _scan(lockfile, None, mode)

    repo_path = repo_config_path(lockfile)
    repo_text = repo_path.read_text(encoding="utf-8") if repo_path.is_file() else ""
    try:
        merged, shadowed = merge_config(repo_text, entries)
    except ConfigMergeError as exc:
        refusal = (
            f"  {SLUG}: NOT SCANNED - cannot add the quality.ignore / deny.toml "
            f"ignores to {repo_path}: {exc}. Scanning without it would drop "
            f"the repo's own ignores."
        )
        if mode == "blocking":
            error(refusal)
            return False
        warn(refusal)
        return True

    for vuln_id in shadowed:
        info(f"  {SLUG}: {vuln_id} is already ignored in {repo_path}, which wins")

    # Outside the checkout, so a local run leaves nothing untracked to commit
    # and a repo's own osv-scanner.toml is never overwritten.
    with tempfile.TemporaryDirectory(prefix="hyperi-ci-osv-") as scratch:
        config_path = Path(scratch) / _CONFIG_NAME
        config_path.write_text(merged, encoding="utf-8", newline="\n")
        return _scan(lockfile, config_path, mode)
