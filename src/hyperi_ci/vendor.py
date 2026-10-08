# Project:   HyperI CI
# File:      src/hyperi_ci/vendor.py
# Purpose:   Mirror files one way from another repository at a pinned ref
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Mirror files one way from another repository at a pinned ref.

``vendor sync`` fetches every file the ``vendor:`` block names at its ref,
writes it to its destination, and records the source, ref and sha256 of each
destination in the lock file. ``vendor check`` fails when a destination no
longer matches the lock, which is a hand edit, or the lock and the config
disagree on where a file comes from, which is a pin bumped without a sync.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import yaml

from hyperi_ci.common import curl_read, error, info, success
from hyperi_ci.config import CIConfig
from hyperi_ci.repo_path import RepoPathError, confine

LOCK_FILE = ".hyperi-ci-vendor.lock"
RAW_URL = "https://raw.githubusercontent.com/{source}/{ref}/{path}"
LOCK_HEADER = (
    "# Written by hyperi-ci vendor sync. Change the vendor: block, not this.\n"
)

type Fetch = Callable[[str, str, str], bytes]


class VendorError(ValueError):
    """The ``vendor:`` block is malformed, or a file cannot be fetched."""


@dataclass(frozen=True, slots=True)
class Pin:
    """Where one destination file comes from."""

    source: str
    ref: str
    path: str

    def __str__(self) -> str:
        """Render as ``owner/repo@ref:path``."""
        return f"{self.source}@{self.ref}:{self.path}"


def pins(config: CIConfig) -> dict[str, Pin]:
    """Return every destination the ``vendor:`` block names, with its pin.

    Raises:
        VendorError: An entry lacks an ``owner/repo`` source, a ref or files,
            or two entries write the same destination.

    """
    found: dict[str, Pin] = {}
    for entry in config.get("vendor") or []:
        source = entry.get("source") if isinstance(entry, dict) else None
        ref = entry.get("ref") if isinstance(entry, dict) else None
        files = entry.get("files") if isinstance(entry, dict) else None
        if not isinstance(source, str) or source.count("/") != 1:
            raise VendorError(f"vendor: source must be owner/repo, got {source!r}")
        if not isinstance(ref, str) or not ref or not isinstance(files, dict):
            raise VendorError(f"vendor: {source} needs a ref and a files mapping")
        for src, dst in files.items():
            if str(dst) in found:
                raise VendorError(f"vendor: {dst} is written by two entries")
            found[str(dst)] = Pin(source, ref, str(src))
    return dict(sorted(found.items()))


def fetch_raw(source: str, ref: str, path: str) -> bytes:
    """Fetch ``path`` from GitHub repo ``source`` at ``ref``, byte for byte.

    Raises:
        VendorError: curl failed.

    """
    url = RAW_URL.format(source=source, ref=quote(ref), path=quote(path))
    rc, body = curl_read(url)
    if rc != 0:
        raise VendorError(f"cannot fetch {Pin(source, ref, path)} (curl exit {rc})")
    return body


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _target(root: Path, dst: str) -> Path:
    try:
        return confine(dst, root, key="vendor files")
    except RepoPathError as exc:
        raise VendorError(str(exc)) from exc


def read_lock(root: Path) -> dict[str, dict]:
    """Return the lock's per-destination records, empty when there is no lock.

    Raises:
        VendorError: The lock file is not a mapping of mappings.

    """
    path = root / LOCK_FILE
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise VendorError(f"{LOCK_FILE}: {exc}") from exc
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        raise VendorError(f"{LOCK_FILE} has no files mapping")
    return files


def sync(config: CIConfig, root: Path, fetch: Fetch = fetch_raw) -> int:
    """Fetch every pinned file, write it, and rewrite the lock.

    Returns:
        The number of files written.

    Raises:
        VendorError: The config is malformed, a destination leaves the repo,
            or a fetch failed. Nothing is written in that case.

    """
    wanted = pins(config)
    if not wanted:
        return 0
    targets = {dst: _target(root, dst) for dst in wanted}
    bodies = {dst: fetch(p.source, p.ref, p.path) for dst, p in wanted.items()}
    records = {}
    for dst, body in bodies.items():
        targets[dst].parent.mkdir(parents=True, exist_ok=True)
        targets[dst].write_bytes(body)
        pin = wanted[dst]
        records[dst] = {
            "path": pin.path,
            "ref": pin.ref,
            "sha256": _sha256(body),
            "source": pin.source,
        }
    lock = LOCK_HEADER + yaml.safe_dump({"files": records}, sort_keys=True)
    (root / LOCK_FILE).write_text(lock, encoding="utf-8", newline="\n")
    return len(records)


def check(config: CIConfig, root: Path) -> list[str]:
    """Return one line per vendored file that has drifted from the lock.

    Raises:
        VendorError: The config or the lock is malformed.

    """
    wanted = pins(config)
    lock = read_lock(root)
    problems: list[str] = []
    for dst, pin in wanted.items():
        record = lock.get(dst)
        record = record if isinstance(record, dict) else {}
        fields = [str(record.get(key, "")) for key in ("source", "ref", "path")]
        locked = Pin(*fields) if record else None
        target = _target(root, dst)
        if locked != pin:
            problems.append(
                f"{dst}: config pins {pin}, the lock has {locked or 'nothing'} "
                "-- run hyperi-ci vendor sync"
            )
        elif not target.is_file():
            problems.append(f"{dst}: missing -- run hyperi-ci vendor sync")
        elif _sha256(target.read_bytes()) != record.get("sha256"):
            problems.append(
                f"{dst}: edited by hand -- change it in {pin.source} and re-sync"
            )
    for dst in sorted(set(lock) - set(wanted)):
        problems.append(f"{dst}: in {LOCK_FILE} but not in vendor: -- re-sync")
    return problems


def run(config: CIConfig, root: Path) -> int:
    """Run ``vendor check`` as a quality gate.

    Returns:
        0 when every vendored file matches its lock, else 1.

    """
    try:
        problems = check(config, root)
    except VendorError as exc:
        error(str(exc))
        return 1
    for problem in problems:
        error(f"  vendor: {problem}")
    if problems:
        return 1
    success(f"  vendor: {len(pins(config))} file(s) match {LOCK_FILE}")
    return 0


def run_sync(config: CIConfig, root: Path) -> int:
    """Run ``vendor sync`` and log the outcome.

    Returns:
        0 on success, else 1.

    """
    try:
        written = sync(config, root)
    except VendorError as exc:
        error(str(exc))
        return 1
    if not written:
        info("No vendor: block -- nothing to sync")
        return 0
    success(f"Vendored {written} file(s); {LOCK_FILE} rewritten")
    return 0
