# Project:   HyperI CI
# File:      src/hyperi_ci/quality/osv_scanner.py
# Purpose:   Malicious-package scanning via osv-scanner (Rust + TS gap)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""osv-scanner: malicious-package (``MAL-*``) detection for Rust and TypeScript.

cargo-audit and npm/pnpm audit do not read the OSSF malicious-packages feed;
pip-audit already covers Python. It defaults to ``warn`` because the feed
ships false-positive waves (ossf/malicious-packages#1276), and the Renovate
7-day cooldown stands in front of it. ``quality.ignore`` entries become
``[[IgnoredVulns]]``, appended to a copy of any repo ``osv-scanner.toml``
because osv-scanner reads that file only when no ``--config`` is given.
"""

import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Iterable
from pathlib import Path

from hyperi_ci.common import error, info, is_ci, run_cmd, success, warn
from hyperi_ci.quality.ignores import IgnoreEntry
from hyperi_ci.tools import missing_tool, warn_on_pin_drift

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

    Each entry becomes an ``[[IgnoredVulns]]`` block, with ``expires`` as
    osv-scanner's RFC3339 ``ignoreUntil``.

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

    The repo text is kept verbatim. On a duplicate id the repo's entry wins,
    because it is what a hand-run osv-scanner reads.

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
    """Return the osv-scanner v2 command scanning one lockfile."""
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
    """Return whether the scan read the lockfile but could not query osv.dev."""
    if result.returncode == _API_FAILED_EXIT:
        return True
    return result.returncode == _ERRORED_EXIT and _OSV_DEV_MATCHER in (
        result.stderr or ""
    )


def _scan(lockfile: Path, config_path: Path | None, mode: str) -> bool:
    """Run one scan and apply the warn / blocking semantics to its exit code.

    Only exit 1 is a finding. An osv.dev outage passes as NOT SCANNED in both
    modes, as cargo-audit treats an unreachable database. Any other scanner
    error takes the mode's outcome without being called a finding.
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

    A missing binary fails a ``blocking`` scan in CI and warn-skips elsewhere.
    A missing or empty lockfile, or an unreachable osv.dev, passes as NOT
    SCANNED. Ignore entries are merged with any repo ``osv-scanner.toml`` into
    a temporary config.

    Returns:
        True on pass, skip, NOT SCANNED or a ``warn``-mode finding; False
        when a ``blocking`` scan found something or errored, could not run
        in CI, or the repo's config could not be merged.

    """
    if mode == "disabled":
        info(f"  {SLUG}: disabled")
        return True

    if not available():
        return not missing_tool(_BINARY, mode)

    warn_on_pin_drift(_BINARY)

    if not lockfile.exists():
        # A library may ship no lockfile: not a failure, not coverage (issue #223).
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

    # Outside the checkout, so the repo's own osv-scanner.toml is never overwritten.
    with tempfile.TemporaryDirectory(prefix="hyperi-ci-osv-") as scratch:
        config_path = Path(scratch) / _CONFIG_NAME
        config_path.write_text(merged, encoding="utf-8", newline="\n")
        return _scan(lockfile, config_path, mode)
