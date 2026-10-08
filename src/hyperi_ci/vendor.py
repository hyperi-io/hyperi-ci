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
import posixpath
import re
import shutil
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
# GitHub's own owner and repository name charsets.
SOURCE_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+")
# A branch, tag or commit name; the URL path is built from it unescaped.
REF_RE = re.compile(r"[A-Za-z0-9._/-]+")
STAGED_SUFFIX = ".vendor-sync"

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


def _segments_ok(value: str) -> bool:
    """Return whether ``value`` is relative with no empty, ``.`` or ``..`` segment.

    curl collapses ``.`` and ``..`` in a URL path, so either one would let a
    ref or path fetch from another repository.
    """
    return all(part not in {"", ".", ".."} for part in value.split("/"))


def check_pin(source: str, ref: str, path: str) -> None:
    """Refuse a pin whose URL could resolve outside ``source`` at ``ref``.

    Raises:
        VendorError: ``source`` is not ``owner/repo``, ``ref`` is not a plain
            git ref, or ``path`` is not relative to the repository root.

    """
    _check_repo(source, ref)
    _check_path(Pin(source, ref, path))


def _check_repo(source: str, ref: str) -> None:
    if not SOURCE_RE.fullmatch(source) or source.split("/")[1] in {".", ".."}:
        raise VendorError(f"vendor: source must be owner/repo, got {source!r}")
    if not REF_RE.fullmatch(ref) or not _segments_ok(ref):
        raise VendorError(f"vendor: {source}: ref {ref!r} is not a plain git ref")


def _check_path(pin: Pin) -> None:
    if not pin.path or not _segments_ok(pin.path):
        raise VendorError(
            f"vendor: {pin}: path {pin.path!r} must be relative to the repository "
            "root with no empty, . or .. segment"
        )


def pins(config: CIConfig) -> dict[str, Pin]:
    """Return every destination the ``vendor:`` block names, with its pin.

    Destinations are compared normalised, so ``./x`` and ``x`` are one.

    Raises:
        VendorError: An entry lacks a valid ``owner/repo`` source, ref or
            files, a source path leaves the repository, or two entries write
            the same destination.

    """
    found: dict[str, Pin] = {}
    for entry in config.get("vendor") or []:
        source = entry.get("source") if isinstance(entry, dict) else None
        ref = entry.get("ref") if isinstance(entry, dict) else None
        files = entry.get("files") if isinstance(entry, dict) else None
        if not isinstance(source, str):
            raise VendorError(f"vendor: source must be owner/repo, got {source!r}")
        if not isinstance(ref, str) or not ref or not isinstance(files, dict):
            raise VendorError(f"vendor: {source} needs a ref and a files mapping")
        _check_repo(source, ref)
        for src, dst in files.items():
            _check_path(Pin(source, ref, str(src)))
            key = posixpath.normpath(str(dst))
            if key in found:
                raise VendorError(f"vendor: {dst} is written by two entries")
            found[key] = Pin(source, ref, str(src))
    return dict(sorted(found.items()))


def fetch_raw(source: str, ref: str, path: str) -> bytes:
    """Fetch ``path`` from GitHub repo ``source`` at ``ref``, byte for byte.

    Raises:
        VendorError: The pin could leave ``source``, or curl failed.

    """
    check_pin(source, ref, path)
    url = RAW_URL.format(source=source, ref=quote(ref), path=quote(path))
    rc, body = curl_read(url)
    if rc != 0:
        raise VendorError(f"cannot fetch {Pin(source, ref, path)} (curl exit {rc})")
    return body


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _target(root: Path, dst: str) -> Path:
    """Resolve ``dst`` to a file path inside ``root`` that sync may write.

    Raises:
        VendorError: ``dst`` leaves ``root``, is ``root`` or another
            directory, lies under ``.git/``, or is the lock file.

    """
    try:
        target = confine(dst, root, key="vendor files")
    except RepoPathError as exc:
        raise VendorError(str(exc)) from exc
    parts = target.relative_to(root.resolve()).parts
    if not parts or target.is_dir():
        raise VendorError(f"vendor: {dst} is a directory, not a file")
    if parts[0].casefold() == ".git":
        raise VendorError(f"vendor: {dst} is under .git/")
    if parts == (LOCK_FILE,):
        raise VendorError(f"vendor: {dst} is {LOCK_FILE}, which sync rewrites")
    return target


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


def _write_all(targets: dict[str, Path], bodies: dict[str, bytes]) -> None:
    """Write every body beside its target, then rename each into place.

    Raises:
        VendorError: A body could not be written. Every staged file, and
            every directory made for one, is removed first.

    """
    made: list[Path] = []
    staged: list[tuple[Path, Path]] = []
    for dst, body in bodies.items():
        target = targets[dst]
        ancestors = (target.parent, *target.parent.parents)
        missing = [p for p in ancestors if not p.exists()]
        if missing:
            made.append(missing[-1])
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}{STAGED_SUFFIX}")
            staged.append((tmp, target))
            tmp.write_bytes(body)
        except OSError as exc:
            for tmp, _ in staged:
                tmp.unlink(missing_ok=True)
            for directory in reversed(made):
                shutil.rmtree(directory, ignore_errors=True)
            raise VendorError(f"vendor: cannot write {dst}: {exc}") from exc
    for tmp, target in staged:
        tmp.replace(target)


def sync(config: CIConfig, root: Path, fetch: Fetch = fetch_raw) -> int:
    """Fetch every pinned file, write it, and rewrite the lock.

    Every file is fetched before any is written, and each is staged beside
    its destination before the first one is renamed into place.

    Returns:
        The number of files written.

    Raises:
        VendorError: The config is malformed, a destination is not a file
            path inside the repo, a fetch failed, or a file could not be
            staged. No destination and no lock is written in that case.

    """
    wanted = pins(config)
    if not wanted:
        return 0
    targets = {dst: _target(root, dst) for dst in wanted}
    bodies = {dst: fetch(p.source, p.ref, p.path) for dst, p in wanted.items()}
    _write_all(targets, bodies)
    records = {}
    for dst, body in bodies.items():
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
