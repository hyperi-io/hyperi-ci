# Project:   HyperI CI
# File:      src/hyperi_ci/quality/upstream_licences.py
# Purpose:   Licence text for a dependency whose published package ships none
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Licence text from a dependency's source repository, else the SPDX standard text.

Many crates and some PyPI packages publish without their licence file, which
sits at the repository root. The text comes from the repository at the tag for
the package's version, so it is the same on every run. With no such tag, or no
licence file there, the SPDX standard text for the declared licence is used and
says so. Each text opens with a line naming where it came from.

git reads the repository over https with no API token or rate limit:
``ls-remote`` picks the tag, and a blobless depth-1 clone lists the real
filenames and fetches only the ones read.
"""

import re
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from hyperi_ci.common import run_cmd

SPDX_LIST_VERSION = "3.29.0"
SPDX_TEXTS = (
    Path(__file__).resolve().parent.parent / "config" / "licence-texts" / "spdx"
)

_LICENCE_PREFIXES = ("LICENSE", "LICENCE", "COPYING", "UNLICENSE")
_NOTICE_PREFIXES = ("NOTICE",)
# Hosts whose repository is the first two path segments of a URL into it.
_FORGES = frozenset({"github.com", "gitlab.com", "codeberg.org", "bitbucket.org"})
_TREE = re.compile(r"/(?:-/)?(?:tree|blob|src)/[^/]+/?(?P<sub>.*)$")
# Opens the paragraph that says where fetched text came from.
_SOURCE_PREFIX = "Licence text from "
# git's stderr for a failure that may not happen next time.
_NETWORK = (
    "could not resolve host",
    "failed to connect",
    "timed out",
    "connection reset",
    "connection refused",
    "early eof",
)
_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0"}
_GIT_TIMEOUT = 120


@dataclass(frozen=True, slots=True)
class Upstream:
    """What a repository gave for one package.

    Attributes:
        licence: The licence text with its source line, or None.
        notice: The NOTICE text at the same tag, or None.
        reachable: False when a network failure stopped the lookup, so a later
            run may find text this one did not.

    """

    licence: str | None = None
    notice: str | None = None
    reachable: bool = True


def licence_body(text: str | None) -> str | None:
    """Return licence text without the provenance paragraph :func:`fetch_from` adds."""
    if text and text.startswith(_SOURCE_PREFIX):
        return text.partition("\n\n")[2]
    return text


def repository_source(repository: str | None) -> tuple[str, str] | None:
    """Return the clone URL and the package's subdirectory from its repository link.

    Args:
        repository: The package's declared repository URL.

    Returns:
        ``(clone_url, subdirectory)``, or None for anything but an https URL.

    """
    if not repository:
        return None
    parts = urlsplit(repository.strip())
    if parts.scheme != "https" or not parts.netloc:
        return None
    path = parts.path.rstrip("/")
    subdir = ""
    if tree := _TREE.search(path):
        subdir = tree["sub"].strip("/")
        path = path[: tree.start()]
    if parts.netloc.lower() in _FORGES:
        path = "/".join(path.split("/")[:3])
    path = path.removesuffix(".git")
    return f"https://{parts.netloc}{path}", subdir


def pick_repository(urls: Iterable[str | None]) -> str | None:
    """Return the first URL on a known forge, else the first URL given.

    A package's homepage is often a docs site git cannot clone, while one of
    its other links is the repository.

    Args:
        urls: Candidate links, most likely first.

    Returns:
        The link to read licence text from, or None when there is none.

    """
    candidates = [url.strip() for url in urls if url and url.strip()]
    for url in candidates:
        if urlsplit(url).netloc.lower() in _FORGES and repository_source(url):
            return url
    return candidates[0] if candidates else None


def tag_candidates(name: str, version: str) -> list[str]:
    """Return the tags a release of ``name`` ``version`` is usually cut as, in order."""
    versions = list(dict.fromkeys([version, version.split("+", 1)[0]]))
    names = list(dict.fromkeys([name, name.replace("_", "-"), name.replace("-", "_")]))
    tags: list[str] = []
    for ver in versions:
        tags += [f"v{ver}", ver]
        for each in names:
            tags += [f"{each}-v{ver}", f"{each}-{ver}", f"{each}@{ver}"]
            tags += [f"{each}/v{ver}", f"{each}/{ver}"]
    return list(dict.fromkeys(tags))


def pick_tag(tags: set[str], name: str, version: str) -> str | None:
    """Return the first candidate tag the repository has, or None."""
    return next((t for t in tag_candidates(name, version) if t in tags), None)


def _network_failure(stderr: str | None) -> bool:
    text = (stderr or "").lower()
    return any(marker in text for marker in _NETWORK)


def _git(args: list[str]) -> tuple[int, str, str]:
    try:
        result = run_cmd(
            ["git", *args],
            check=False,
            capture=True,
            env=_GIT_ENV,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", f"timed out: {exc}"
    return result.returncode, result.stdout or "", result.stderr or ""


def _remote_tags(clone_url: str) -> tuple[set[str] | None, bool]:
    code, out, err = _git(["ls-remote", "--tags", "--refs", clone_url])
    if code != 0:
        return None, not _network_failure(err)
    tags = {
        line.split("refs/tags/", 1)[1]
        for line in out.splitlines()
        if "refs/tags/" in line
    }
    return tags, True


def _matching(names: list[str], prefixes: tuple[str, ...]) -> list[str]:
    return sorted(n for n in names if n.upper().startswith(prefixes))


def _read_files(work: Path, directory: str) -> tuple[list[str], list[str]]:
    """Return the licence and NOTICE paths at ``directory`` of the cloned tag."""
    tree = f"HEAD:{directory}" if directory else "HEAD"
    code, out, _ = _git(["-C", str(work), "ls-tree", "--name-only", tree])
    if code != 0:
        return [], []
    names = out.splitlines()
    prefix = f"{directory}/" if directory else ""
    licences = [prefix + n for n in _matching(names, _LICENCE_PREFIXES)]
    notices = [prefix + n for n in _matching(names, _NOTICE_PREFIXES)]
    return licences, notices


def _show(work: Path, paths: list[str]) -> str | None:
    texts = []
    for path in paths:
        code, out, _ = _git(["-C", str(work), "show", f"HEAD:{path}"])
        if code == 0 and out.strip():
            texts.append(
                out.strip() if len(paths) == 1 else f"{path}:\n\n{out.strip()}"
            )
    return "\n\n".join(texts) or None


def fetch_from(clone_url: str, subdir: str, name: str, version: str) -> Upstream:
    """Read a package's licence and NOTICE files at its version tag.

    Args:
        clone_url: Where git clones the repository from.
        subdir: The package's directory in it, tried before the root.
        name: The package name.
        version: The package version.

    Returns:
        What the tag holds. No tag or no licence file is an empty, reachable
        answer.

    """
    tags, reachable = _remote_tags(clone_url)
    if tags is None:
        return Upstream(reachable=reachable)
    tag = pick_tag(tags, name, version)
    if tag is None:
        return Upstream()
    with tempfile.TemporaryDirectory(prefix="hyperi-ci-licence-") as scratch:
        work = Path(scratch) / "repo"
        code, _, err = _git(
            [
                "clone",
                "--quiet",
                "--depth",
                "1",
                "--branch",
                tag,
                "--filter=blob:none",
                "--no-checkout",
                clone_url,
                str(work),
            ]
        )
        if code != 0:
            return Upstream(reachable=not _network_failure(err))
        for directory in dict.fromkeys([subdir, ""]):
            licences, notices = _read_files(work, directory)
            if licences:
                text = _show(work, licences)
                if text is None:
                    continue
                source = (
                    f"{_SOURCE_PREFIX}{clone_url} at tag {tag}: {', '.join(licences)}."
                    " The published package ships none."
                )
                return Upstream(f"{source}\n\n{text}", _show(work, notices))
    return Upstream()


def fetch(name: str, version: str, repository: str | None) -> Upstream:
    """Read a package's licence text from the repository it declares.

    Args:
        name: The package name.
        version: The package version.
        repository: The repository URL its metadata declares.

    Returns:
        What the repository gave, empty when it declares none git can read.

    """
    source = repository_source(repository)
    if source is None:
        return Upstream()
    return fetch_from(source[0], source[1], name, version)


def spdx_ids(expression: str | None) -> list[str]:
    """Return the licence ids an SPDX expression names, exceptions left out."""
    ids: list[str] = []
    after_with = False
    for token in re.split(r"[\s()]+", expression or ""):
        if not token or token in ("AND", "OR"):
            continue
        if token == "WITH":
            after_with = True
            continue
        if not after_with:
            ids.append(token.removesuffix("+"))
        after_with = False
    return list(dict.fromkeys(ids))


def standard_text(expression: str | None) -> str | None:
    """Return the SPDX standard text for every licence ``expression`` names.

    Returns:
        The texts with a line saying where they came from, or None when the
        expression names a licence with no bundled text.

    """
    ids = spdx_ids(expression)
    paths = [SPDX_TEXTS / f"{spdx_id}.txt" for spdx_id in ids]
    if not ids or not all(path.is_file() for path in paths):
        return None
    source = (
        f"SPDX License List {SPDX_LIST_VERSION} standard text for {expression}."
        " Neither the published package nor its repository at this version ships"
        " a licence file, so any copyright line is the template's."
    )
    texts = [path.read_text(encoding="utf-8").strip() for path in paths]
    return "\n\n".join([source, *texts])
