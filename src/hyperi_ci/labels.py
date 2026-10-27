# Project:   HyperI CI
# File:      src/hyperi_ci/labels.py
# Purpose:   The single reader for the org issue label manifest
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Read the org issue label manifest.

The manifest comes from the first of these that exists: a file the caller
names, the file at ``issues.labels.path`` on the default branch of the org's
``.github`` repo, or the neutral default shipped in ``config/labels.yaml``.
:attr:`Manifest.source` records which, and the ``.github`` commit it was read
at.

Other repos import this module rather than copying names: hyperi-infra's label
reconcile creates what :func:`expected_labels` returns, and dfe-sweep reads the
gate through :func:`gate_of`. Importing it loads stdlib and ``yaml`` only. The
functions that read GitHub or the config load :mod:`hyperi_ci.gh` and
:mod:`hyperi_ci.config` when called.

Every function that reads the manifest takes an optional ``manifest``. Without
one it loads the manifest for the org of the environment or the cwd's repo.

Label names compare case-insensitively, as GitHub treats them.

Release horizons. The gate labels are the one horizon vocabulary, strictest
first: ``release-blocker``, ``next-patch``, ``next-minor``, ``next-major``,
``backlog``. A program, a release train spanning repos, takes the ``next-*``
horizons as ``<program>:<horizon>`` labels. Read horizons through this module
or ``hyperi-ci labels --json``, never from GitHub's label list.

What counts as a release, for counting how many an item has waited:

- per repo, a ``v*`` git tag
- per program, a change of the value at its ``released`` source's key, for
  dfe ``latest`` in dfe-infra ``versions.yaml``, the last GA stack cut. A
  prerelease never changes it, and a patch GA does. An empty value means no
  GA yet, which :class:`NoReleaseYetError` reports.
"""

import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from functools import cache, lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import quote

import yaml

MANIFEST_FILE = Path(__file__).resolve().parent / "config" / "labels.yaml"

# The packaged default as the wheel lays it out, for provenance.
_PACKAGED_PATH = MANIFEST_FILE.relative_to(MANIFEST_FILE.parents[2]).as_posix()

# Major version of the manifest file format this reader understands.
MANIFEST_SCHEMA = 1

# Config key naming the manifest's path in an org's `.github` repo. Empty
# turns the org lookup off.
PATH_KEY = "issues.labels.path"

# Values of ManifestSource.kind.
SOURCE_ORG = "org-dotgithub"
SOURCE_FILE = "file"
SOURCE_PACKAGED = "packaged-default"

# GitHub refuses a longer label description.
MAX_DESCRIPTION = 100

# Version of the `labels --json` shape, raised when a key is removed or retyped.
JSON_SCHEMA = 1

# Who may place a horizon label: the values of a gate entry's `set_by`.
SET_BY = ("anyone", "release-owner", "hand")

# The horizons a program may carry, the ones that resolve to a version line.
PROGRAM_HORIZONS = ("next-patch", "next-minor", "next-major")

_AREA_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_PROGRAM_NAME = _AREA_NAME
_COLOR = re.compile(r"^[0-9a-fA-F]{6}$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_VERSION = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)


class ManifestError(ValueError):
    """The label manifest is malformed."""


class AreasError(ValueError):
    """A repo's ``issues.areas`` block is malformed."""


class ProgramError(ValueError):
    """A program is unknown, or its membership or version source is unusable."""


class NoReleaseYetError(ProgramError):
    """A program's ``released`` value is empty: it has had no GA release."""


@dataclass(frozen=True, slots=True)
class Label:
    """One label as it should exist on a repo.

    Attributes:
        name: Label name, exactly as created.
        color: Six hex digits, no ``#``, lower case.
        description: Exact description text.

    """

    name: str
    color: str
    description: str


@dataclass(frozen=True, slots=True)
class IssueType:
    """One org issue type and when to use it.

    Attributes:
        name: Type name as GitHub shows it.
        use_for: What the type covers.
        not_for: What it does not, empty where nothing is excluded.

    """

    name: str
    use_for: str
    not_for: str


@dataclass(frozen=True, slots=True)
class Horizon:
    """One release horizon, which is one gate label.

    Attributes:
        name: The gate label name.
        rank: Position in the vocabulary, 1 for the strictest.
        set_by: ``anyone``, ``release-owner`` or ``hand``.
        description: The gate label's description.

    """

    name: str
    rank: int
    set_by: str
    description: str


@dataclass(frozen=True, slots=True)
class Source:
    """One file on another repo's default branch, and the key read from it.

    Attributes:
        repo: ``owner/name``.
        path: Path in that repo.
        key: Top-level key, dotted for a nested one.

    """

    repo: str
    path: str
    key: str


@dataclass(frozen=True, slots=True)
class Program:
    """A release train spanning repos, and its label family.

    Attributes:
        name: Program name, the label prefix before ``:``.
        labels: One label per horizon, in vocabulary order.
        horizons: The horizon each label stands for, same order.
        members: Where membership is read: a mapping whose entries each carry
            ``repo: owner/name``.
        version: Where the train version the lines resolve from is read: a
            semver string.
        released: Where the last released version is read: a semver string.
            A change of it is one program release.

    """

    name: str
    labels: tuple[Label, ...]
    horizons: tuple[str, ...]
    members: Source
    version: Source
    released: Source


@dataclass(frozen=True, slots=True)
class ProgramRelease:
    """One program release: the commit that moved the ``released`` value.

    Attributes:
        version: The GA version it moved to.
        at: When the commit landed, ISO 8601 UTC.
        commit: The commit SHA.

    """

    version: str
    at: str
    commit: str


@dataclass(frozen=True, slots=True)
class ManifestSource:
    """Where a manifest was read from.

    Attributes:
        kind: ``org-dotgithub``, ``file`` or ``packaged-default``.
        org: The org whose ``.github`` repo held it, empty for the other kinds.
        path: The file's path, in that repo for ``org-dotgithub``.
        sha: The ``<org>/.github`` commit the file was read at, empty for the
            other kinds.

    """

    kind: str
    org: str = ""
    path: str = ""
    sha: str = ""

    def describe(self) -> str:
        """Return a one-line description for a log line."""
        if self.kind == SOURCE_ORG:
            return f"{self.org}/.github:{self.path} at {self.sha}"
        if self.kind == SOURCE_FILE:
            return f"file {self.path}"
        return f"the packaged default {self.path}"

    def as_json(self) -> dict[str, str]:
        """Return the ``source`` object of ``labels --json``."""
        return {"kind": self.kind, "org": self.org, "path": self.path, "sha": self.sha}


@dataclass(frozen=True, slots=True)
class Manifest:
    """The parsed manifest.

    Attributes:
        types: Org issue types.
        gate: Gate labels, strictest first.
        horizons: The gate as horizons, same order.
        programs: Program name -> :class:`Program`, in manifest order.
        waiting: Waiting labels, at most one per issue.
        flags: Flag labels, zero or more per issue.
        community: GitHub's community labels, optional per repo.
        bot_owned: Names of labels only bots create and apply.
        area_prefix: Prefix of every area label, ``area/``.
        area_color: Colour of every area label.
        retired: Retired label name -> what replaces it.
        retired_template: Description of a retired label, with
            ``{replacement}`` in it.
        allowed_patterns: Regexes, matched against the whole name, of labels
            other standards declare. Accepted, never created.
        allowed_literals: Exact names of the same kind.
        source: Where it was read from. Two manifests with the same content
            compare equal whatever their source.

    """

    types: tuple[IssueType, ...]
    gate: tuple[Label, ...]
    horizons: tuple[Horizon, ...]
    programs: Mapping[str, Program]
    waiting: tuple[Label, ...]
    flags: tuple[Label, ...]
    community: tuple[Label, ...]
    bot_owned: tuple[str, ...]
    area_prefix: str
    area_color: str
    retired: Mapping[str, str]
    retired_template: str
    allowed_patterns: tuple[str, ...]
    allowed_literals: tuple[str, ...]
    source: ManifestSource = field(compare=False)


def _text(entry: Mapping[str, Any], key: str, where: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str):
        raise ManifestError(f"{where}: `{key}` must be a string")
    return value


def _labels(raw: Mapping[str, Any], section: str) -> tuple[Label, ...]:
    entries = raw.get(section)
    if not isinstance(entries, list) or not entries:
        raise ManifestError(f"`{section}` must be a non-empty list of labels")
    labels: list[Label] = []
    for index, entry in enumerate(entries):
        where = f"{section}[{index}]"
        if not isinstance(entry, Mapping):
            raise ManifestError(f"{where}: must be a mapping")
        name = _text(entry, "name", where)
        color = _text(entry, "color", where)
        description = _text(entry, "description", where)
        if not name:
            raise ManifestError(f"{where}: `name` is empty")
        if not _COLOR.match(color):
            raise ManifestError(f"{where}: `color` {color!r} is not six hex digits")
        if len(description) > MAX_DESCRIPTION:
            raise ManifestError(
                f"{where}: description is over {MAX_DESCRIPTION} characters"
            )
        labels.append(Label(name, color.lower(), description))
    return tuple(labels)


def _types(raw: Mapping[str, Any]) -> tuple[IssueType, ...]:
    entries = raw.get("types")
    if not isinstance(entries, list) or not entries:
        raise ManifestError("`types` must be a non-empty list")
    types: list[IssueType] = []
    for index, entry in enumerate(entries):
        where = f"types[{index}]"
        if not isinstance(entry, Mapping):
            raise ManifestError(f"{where}: must be a mapping")
        types.append(
            IssueType(
                _text(entry, "name", where),
                _text(entry, "use_for", where),
                _text(entry, "not_for", where),
            )
        )
    return tuple(types)


def _allowed(raw: Mapping[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    allowed = raw.get("allowed")
    if not isinstance(allowed, Mapping):
        raise ManifestError("`allowed` must be a mapping of patterns and literals")
    found: list[tuple[str, ...]] = []
    for key in ("patterns", "literals"):
        entries = allowed.get(key)
        if not isinstance(entries, list) or not all(
            isinstance(entry, str) and entry for entry in entries
        ):
            raise ManifestError(f"`allowed.{key}` must be a list of strings")
        found.append(tuple(entries))
    for pattern in found[0]:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ManifestError(f"`allowed.patterns`: {pattern!r}: {exc}") from exc
    return found[0], found[1]


def _horizons(raw: Mapping[str, Any], gate: tuple[Label, ...]) -> tuple[Horizon, ...]:
    horizons: list[Horizon] = []
    for index, label in enumerate(gate):
        set_by = raw["gate"][index].get("set_by")
        if set_by not in SET_BY:
            raise ManifestError(
                f"gate[{index}]: `set_by` must be one of {', '.join(SET_BY)}"
            )
        horizons.append(Horizon(label.name, index + 1, set_by, label.description))
    return tuple(horizons)


def _source(entry: object, where: str) -> Source:
    if not isinstance(entry, Mapping):
        raise ManifestError(f"{where}: must be a mapping of repo, path and key")
    repo = _text(entry, "repo", where)
    path = _text(entry, "path", where)
    key = _text(entry, "key", where)
    if not _REPO.match(repo) or not path or not key:
        raise ManifestError(f"{where}: needs repo as owner/name, a path and a key")
    return Source(repo, path, key)


def _programs(raw: Mapping[str, Any], gate: tuple[Label, ...]) -> Mapping[str, Program]:
    entries = raw.get("programs")
    if not isinstance(entries, Mapping):
        raise ManifestError("`programs` must be a mapping of program name to entry")
    colors = {label.name: label.color for label in gate}
    programs: dict[str, Program] = {}
    for name, entry in entries.items():
        where = f"programs.{name}"
        if not isinstance(name, str) or not _PROGRAM_NAME.match(name):
            raise ManifestError(f"{where}: a program name is a lower-case slug")
        if not isinstance(entry, Mapping):
            raise ManifestError(f"{where}: must be a mapping")
        descriptions = entry.get("labels")
        if not isinstance(descriptions, Mapping) or not descriptions:
            raise ManifestError(f"{where}.labels: must map horizon to description")
        unknown = [h for h in descriptions if h not in PROGRAM_HORIZONS]
        if unknown:
            raise ManifestError(
                f"{where}.labels: {unknown[0]!r} is not one of "
                f"{', '.join(PROGRAM_HORIZONS)}"
            )
        horizons = tuple(h for h in PROGRAM_HORIZONS if h in descriptions)
        family: list[Label] = []
        for horizon in horizons:
            text = descriptions[horizon]
            if not isinstance(text, str) or not text:
                raise ManifestError(f"{where}.labels.{horizon}: must be a string")
            if len(text) > MAX_DESCRIPTION:
                raise ManifestError(
                    f"{where}.labels.{horizon}: description is over "
                    f"{MAX_DESCRIPTION} characters"
                )
            family.append(Label(f"{name}:{horizon}", colors[horizon], text))
        programs[name] = Program(
            name=name,
            labels=tuple(family),
            horizons=horizons,
            members=_source(entry.get("members"), f"{where}.members"),
            version=_source(entry.get("version"), f"{where}.version"),
            released=_source(entry.get("released"), f"{where}.released"),
        )
    return MappingProxyType(programs)


def _parse(raw: object, source: ManifestSource) -> Manifest:
    if not isinstance(raw, Mapping):
        raise ManifestError("the manifest must be a mapping")
    schema = raw.get("schema", MANIFEST_SCHEMA)
    if isinstance(schema, bool) or schema != MANIFEST_SCHEMA:
        raise ManifestError(
            f"`schema` is {schema!r}, and this reader understands {MANIFEST_SCHEMA}"
        )
    bot_owned = raw.get("bot_owned")
    if not isinstance(bot_owned, list) or not all(
        isinstance(name, str) and name for name in bot_owned
    ):
        raise ManifestError("`bot_owned` must be a list of label names")
    area = raw.get("area")
    if not isinstance(area, Mapping):
        raise ManifestError("`area` must be a mapping")
    prefix = _text(area, "prefix", "area")
    area_color = _text(area, "color", "area")
    if not prefix or not _COLOR.match(area_color):
        raise ManifestError("`area` needs a prefix and a six-hex-digit colour")
    retired = raw.get("retired")
    if not isinstance(retired, Mapping) or not all(
        isinstance(k, str) and isinstance(v, str) and v for k, v in retired.items()
    ):
        raise ManifestError("`retired` must map label names to replacement text")
    template = raw.get("retired_description")
    if not isinstance(template, str) or "{replacement}" not in template:
        raise ManifestError("`retired_description` must contain `{replacement}`")
    patterns, literals = _allowed(raw)
    gate = _labels(raw, "gate")
    missing = [h for h in PROGRAM_HORIZONS if h not in {g.name for g in gate}]
    if missing:
        raise ManifestError(f"`gate` lacks the horizon {missing[0]!r}")
    manifest = Manifest(
        types=_types(raw),
        gate=gate,
        horizons=_horizons(raw, gate),
        programs=_programs(raw, gate),
        waiting=_labels(raw, "waiting"),
        flags=_labels(raw, "flags"),
        community=_labels(raw, "community"),
        bot_owned=tuple(bot_owned),
        area_prefix=prefix,
        area_color=area_color.lower(),
        retired=MappingProxyType(dict(retired)),
        retired_template=template,
        allowed_patterns=patterns,
        allowed_literals=literals,
        source=source,
    )
    _check_unique(manifest)
    return manifest


def _check_unique(manifest: Manifest) -> None:
    """Refuse a name that sits in two sections, which would give it two rules."""
    names = [
        label.name
        for section in (
            manifest.gate,
            manifest.waiting,
            manifest.flags,
            manifest.community,
            *(program.labels for program in manifest.programs.values()),
        )
        for label in section
    ]
    names += [*manifest.bot_owned, *manifest.retired, *manifest.allowed_literals]
    seen: set[str] = set()
    for name in names:
        key = name.casefold()
        if key in seen:
            raise ManifestError(f"label {name!r} is declared more than once")
        seen.add(key)


def resolve_org(repo: str | None = None, cwd: Path | None = None) -> str:
    """Return the GitHub org whose ``.github`` repo supplies the manifest.

    Args:
        repo: ``owner/name`` the caller is working on. Its owner wins.
        cwd: Directory whose ``origin`` remote names the repo, read when
            neither ``repo`` nor ``GITHUB_REPOSITORY_OWNER`` is set.

    Returns:
        The org, or ``""`` when nothing names one.

    """
    if repo:
        return repo.partition("/")[0]
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "").strip()
    if owner:
        return owner
    from hyperi_ci.description_source import repo_slug

    try:
        slug = repo_slug(cwd)
    except OSError:
        return ""
    return slug.partition("/")[0] if slug else ""


def load_manifest(
    path: Path | None = None, *, org: str | None = None, strict: bool = False
) -> Manifest:
    """Load and validate the label manifest.

    Resolution order: ``path`` when given, else the file at config
    ``issues.labels.path`` on the default branch of ``<org>/.github``, else
    the neutral default shipped in the package. The org file is read once per
    process. Its absence falls to the default silently. Any other failure to
    read or validate it falls to the default with one warning, or raises under
    ``strict``.

    Args:
        path: Manifest file, read as given.
        org: Org whose ``.github`` repo to read. None resolves it with
            :func:`resolve_org` from the environment and the cwd's repo.
        strict: Raise rather than fall back when the org file exists but is
            unusable, for a check that must not pass against the wrong set.

    Returns:
        The parsed :class:`Manifest`, its ``source`` saying which it was.

    Raises:
        ManifestError: ``path`` is unreadable, not YAML, or malformed, the
            project config cannot be read for ``issues.labels.path``, or
            ``strict`` and the org file is unusable.

    """
    if path is not None:
        if path == MANIFEST_FILE:
            return _packaged()
        return _read(path, ManifestSource(SOURCE_FILE, path=str(path)))
    where = _org_path()
    owner = _default_org() if org is None else org
    if where and owner:
        found = _from_org(owner, where, strict=strict)
        if found is not None:
            return found
    return _packaged()


def _given(manifest: Manifest | None) -> Manifest:
    return load_manifest() if manifest is None else manifest


def _org_path() -> str:
    """Return ``issues.labels.path`` from the config cascade, empty when off."""
    from hyperi_ci.config import ConfigError, load_config

    try:
        value = load_config(report_removed=False).setting(PATH_KEY)
    except ConfigError as exc:
        raise ManifestError(f"cannot read `{PATH_KEY}`: {exc}") from exc
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ManifestError(f"`{PATH_KEY}` must be a path, not {value!r}")
    return value.strip()


@lru_cache(maxsize=1)
def _default_org() -> str:
    return resolve_org()


@lru_cache(maxsize=1)
def _packaged() -> Manifest:
    return _read(MANIFEST_FILE, ManifestSource(SOURCE_PACKAGED, path=_PACKAGED_PATH))


def _read(path: Path, source: ManifestSource) -> Manifest:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ManifestError(f"{path}: {exc}") from exc
    try:
        return _parse(raw, source)
    except ManifestError as exc:
        raise ManifestError(f"{path}: {exc}") from exc


def _org_commit(repo: str, path: str) -> str | None:
    """Return the last default-branch commit touching ``path``, None for none.

    Raises:
        ManifestError: The commit list could not be read, other than a 404.

    """
    from hyperi_ci import gh

    result = gh.gh_api([f"repos/{repo}/commits?path={quote(path)}&per_page=1"])
    if result.data is None:
        if "HTTP 404" in (result.stderr or ""):
            return None
        raise ManifestError(result.reason)
    if not isinstance(result.data, list):
        raise ManifestError(f"the commit list of {repo}:{path} was not a JSON array")
    if not result.data:
        return None
    head = result.data[0]
    sha = head.get("sha") if isinstance(head, Mapping) else None
    if not isinstance(sha, str) or not sha:
        raise ManifestError(f"the commit list of {repo}:{path} carries no sha")
    return sha


@dataclass(frozen=True, slots=True)
class _OrgRead:
    """One read of the org manifest: the manifest, or why there is none.

    Attributes:
        manifest: The parsed manifest, None when absent or unusable.
        failure: Why it is unusable, empty when it is absent or was read.

    """

    manifest: Manifest | None
    failure: str = ""


@cache
def _read_org(org: str, path: str) -> _OrgRead:
    """Read ``path`` from ``<org>/.github`` at the commit that last touched it.

    The file is read at the commit the sha names, so the two always agree.
    """
    from hyperi_ci import gh

    repo = f"{org}/.github"
    try:
        sha = _org_commit(repo, path)
        text = None if sha is None else gh.read_repo_file(repo, path, ref=sha)
        if sha is None or text is None:
            return _OrgRead(None)
        source = ManifestSource(SOURCE_ORG, org, path, sha)
        return _OrgRead(_parse(yaml.safe_load(text), source))
    except (ManifestError, yaml.YAMLError, gh.RepoFileError, OSError) as exc:
        return _OrgRead(None, " ".join(str(exc).split()))


@cache
def _warn_unusable(repo: str, path: str, reason: str) -> None:
    from hyperi_ci.common import warn

    warn(
        f"labels: {repo}:{path} is unusable, so the packaged default applies: {reason}"
    )


def _from_org(org: str, path: str, *, strict: bool) -> Manifest | None:
    """Return the org manifest, None when the packaged default applies.

    Raises:
        ManifestError: ``strict`` and the file exists but could not be read
            or validated.

    """
    read = _read_org(org, path)
    if read.failure:
        repo = f"{org}/.github"
        if strict:
            raise ManifestError(f"labels: {repo}:{path} is unusable: {read.failure}")
        _warn_unusable(repo, path, read.failure)
    return read.manifest


def gate_labels(*, manifest: Manifest | None = None) -> tuple[str, ...]:
    """Return the gate label names, strictest first."""
    return tuple(label.name for label in _given(manifest).gate)


def _gates_present(labels: Iterable[str], manifest: Manifest | None) -> tuple[str, ...]:
    present = {name.casefold() for name in labels}
    names = gate_labels(manifest=manifest)
    return tuple(name for name in names if name.casefold() in present)


def gate_of(labels: Iterable[str], *, manifest: Manifest | None = None) -> str | None:
    """Return the issue's gate: the strictest gate label it carries.

    Args:
        labels: The issue's label names.
        manifest: The manifest to read, :func:`load_manifest` when None.

    Returns:
        The manifest's spelling of the strictest gate label, or None when the
        issue carries none, which means untriaged.

    """
    found = _gates_present(labels, manifest)
    return found[0] if found else None


def conflicts(
    labels: Iterable[str], *, manifest: Manifest | None = None
) -> tuple[str, ...]:
    """Return the gate labels an issue carries when it carries more than one.

    Args:
        labels: The issue's label names.
        manifest: The manifest to read, :func:`load_manifest` when None.

    Returns:
        Every gate label present, strictest first, or an empty tuple when
        there is one or none.

    """
    found = _gates_present(labels, manifest)
    return found if len(found) > 1 else ()


def parse_areas(raw: object) -> dict[str, tuple[str, ...]]:
    """Validate a repo's ``issues.areas`` block.

    Args:
        raw: The configured value: a mapping of area name to a list of paths.
            None or an empty mapping means the repo declares no areas.

    Returns:
        Area name -> the paths it covers, in configured order.

    Raises:
        AreasError: The block is not a mapping, a name is not a lower-case
            slug, or a value is not a non-empty list of path strings.

    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise AreasError("`issues.areas` must be a mapping of area name to paths")
    areas: dict[str, tuple[str, ...]] = {}
    for name, paths in raw.items():
        if not isinstance(name, str) or not _AREA_NAME.match(name):
            raise AreasError(
                f"`issues.areas.{name}`: an area name is a lower-case slug "
                "(a-z, 0-9, single hyphens)"
            )
        if (
            not isinstance(paths, list)
            or not paths
            or not all(isinstance(p, str) and p.strip() for p in paths)
        ):
            raise AreasError(
                f"`issues.areas.{name}` must be a non-empty list of path strings"
            )
        areas[name] = tuple(paths)
    return areas


def area_label(name: str, *, manifest: Manifest | None = None) -> str:
    """Return the label name for area ``name``, e.g. ``area/auth``."""
    return f"{_given(manifest).area_prefix}{name}"


def area_description(paths: Iterable[str]) -> str:
    """Return an area label's description: its paths, cut to GitHub's limit."""
    text = ", ".join(paths)
    if len(text) <= MAX_DESCRIPTION:
        return text
    return text[: MAX_DESCRIPTION - 3].rstrip(", ") + "..."


def expected_labels(
    areas: Mapping[str, Iterable[str]] | None = None,
    programs: Iterable[str] = (),
    *,
    manifest: Manifest | None = None,
) -> tuple[Label, ...]:
    """Return every label a repo must carry.

    Gate, waiting and flag labels, then each listed program's family, then one
    ``area/<name>`` per declared area. Community labels are optional and
    bot-owned ones belong to the bot, so neither is included.

    Args:
        areas: The repo's validated areas, as :func:`parse_areas` returns.
        programs: Programs the repo belongs to, as :func:`program_members`
            answers. A repo that left one is simply not listed: its labels
            stay, since nothing deletes them.
        manifest: The manifest to read, :func:`load_manifest` when None.

    Returns:
        The labels, each with its exact colour and description.

    Raises:
        ProgramError: A listed program is not in the manifest.

    """
    manifest = _given(manifest)
    labels = [*manifest.gate, *manifest.waiting, *manifest.flags]
    for program in programs:
        labels.extend(program_info(program, manifest=manifest).labels)
    for name, paths in (areas or {}).items():
        label = area_label(name, manifest=manifest)
        labels.append(Label(label, manifest.area_color, area_description(paths)))
    return tuple(labels)


def is_allowed(name: str, *, manifest: Manifest | None = None) -> bool:
    """Return True when another standard declares label ``name``.

    Such a label is accepted on a repo but never created by the reconcile.
    """
    manifest = _given(manifest)
    if name.casefold() in {lit.casefold() for lit in manifest.allowed_literals}:
        return True
    return any(re.fullmatch(p, name) for p in manifest.allowed_patterns)


def program_info(name: str, *, manifest: Manifest | None = None) -> Program:
    """Return program ``name`` from the manifest.

    Raises:
        ProgramError: The manifest declares no such program.

    """
    program = _given(manifest).programs.get(name)
    if program is None:
        raise ProgramError(f"no program {name!r} in the label manifest")
    return program


def _read_source(source: Source) -> object:
    """Read and parse ``source``'s file, raising on anything but YAML content."""
    # Deferred so importing this module never loads scalo through hyperi_ci.gh.
    from hyperi_ci import gh

    where = f"{source.repo}:{source.path}"
    try:
        text = gh.read_repo_file(source.repo, source.path)
    except gh.RepoFileError as exc:
        raise ProgramError(str(exc)) from exc
    except OSError as exc:
        raise ProgramError(f"could not read {where}: {exc}") from exc
    if text is None:
        raise ProgramError(f"{where} does not exist on the default branch")
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ProgramError(f"{where} is not valid YAML: {exc}") from exc


def _at_key(doc: object, source: Source) -> object:
    value = doc
    for part in source.key.split("."):
        if not isinstance(value, Mapping) or part not in value:
            raise ProgramError(f"{source.repo}:{source.path} has no `{source.key}`")
        value = value[part]
    return value


def program_members(name: str, *, manifest: Manifest | None = None) -> set[str]:
    """Return the repos in program ``name``, read from its membership source.

    One ``gh api`` read per call, so a caller labelling many repos reads it
    once and passes the result along.

    Args:
        name: Program name, e.g. ``dfe``.
        manifest: The manifest to read, :func:`load_manifest` when None.

    Returns:
        Every member as ``owner/name``, the ``repo`` of each entry under the
        source's key. For dfe, ``nodes.<node>.repo`` in dfe-infra
        ``suite.yaml``.

    Raises:
        ProgramError: The program is unknown, or the source is unreadable,
            missing, not YAML, or an entry carries no ``owner/name`` repo.

    """
    source = program_info(name, manifest=manifest).members
    entries = _at_key(_read_source(source), source)
    where = f"{source.repo}:{source.path} `{source.key}`"
    if not isinstance(entries, Mapping) or not entries:
        raise ProgramError(f"{where} must be a non-empty mapping")
    members: set[str] = set()
    for node, entry in entries.items():
        repo = entry.get("repo") if isinstance(entry, Mapping) else None
        if not isinstance(repo, str) or not _REPO.match(repo):
            raise ProgramError(f"{where}.{node}: `repo` must be owner/name")
        members.add(repo)
    return members


def _semver_at(source: Source, doc: object) -> str:
    value = _at_key(doc, source)
    if not isinstance(value, str) or not _VERSION.match(value):
        raise ProgramError(
            f"{source.repo}:{source.path} `{source.key}` is {value!r}, "
            "not a MAJOR.MINOR.PATCH version"
        )
    return value


def program_version(name: str, *, manifest: Manifest | None = None) -> str:
    """Return the train version program ``name``'s horizon lines resolve from.

    For dfe it is ``current`` in dfe-infra ``versions.yaml``, the stack being
    built, prereleases included.

    Raises:
        ProgramError: The program is unknown, or the source is unreadable,
            missing, not YAML, or holds no semver string at its key.

    """
    source = program_info(name, manifest=manifest).version
    return _semver_at(source, _read_source(source))


def _released_at(program: Program, doc: object) -> str:
    source = program.released
    if _at_key(doc, source) in (None, ""):
        raise NoReleaseYetError(
            f"{source.repo}:{source.path} `{source.key}` is empty, so "
            f"{program.name} has no GA release yet"
        )
    return _semver_at(source, doc)


def program_released(name: str, *, manifest: Manifest | None = None) -> str:
    """Return program ``name``'s last released version.

    A change of this value is one program release, for counting how many
    releases an item has waited. For dfe it is ``latest`` in dfe-infra
    ``versions.yaml``, the last GA stack cut, which no prerelease moves.

    Raises:
        NoReleaseYetError: The value is empty or null: no GA release yet.
        ProgramError: The program is unknown, or the source is unreadable,
            missing, not YAML, or holds no semver string at its key.

    """
    program = program_info(name, manifest=manifest)
    return _released_at(program, _read_source(program.released))


def ga_version(text: object) -> tuple[int, int, int] | None:
    """Return ``(major, minor, patch)`` of a GA semver, None for anything else.

    A ``v`` prefix and build metadata are accepted. A prerelease is not GA.
    """
    if not isinstance(text, str):
        return None
    match = _VERSION.match(text)
    if match is None or match.group(4) is not None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _commits(source: Source, query: str, *, paginate: bool) -> list[tuple[str, str]]:
    """Return ``(sha, committer date)`` of commits touching ``source``'s file."""
    from hyperi_ci import gh

    args = [f"repos/{source.repo}/commits?path={source.path}&{query}"]
    result = gh.gh_api([*args, "--paginate"] if paginate else args)
    if not isinstance(result.data, list):
        raise ProgramError(result.reason or f"{args[0]} was not a JSON array")
    found: list[tuple[str, str]] = []
    for entry in result.data:
        commit = entry.get("commit") if isinstance(entry, Mapping) else None
        committer = commit.get("committer") if isinstance(commit, Mapping) else None
        date = committer.get("date") if isinstance(committer, Mapping) else None
        sha = entry.get("sha") if isinstance(entry, Mapping) else None
        if isinstance(sha, str) and isinstance(date, str):
            found.append((sha, date))
    return found


def program_releases_since(
    name: str, since: str, *, manifest: Manifest | None = None
) -> tuple[ProgramRelease, ...]:
    """Return program ``name``'s releases after ``since``, oldest first.

    A release is a commit on the ``released`` source's default branch that
    moves the value to a GA version higher than every earlier one. A
    prerelease and a reverted cut never count.

    Reads the file's commit list from ``since`` on, plus the last commit
    before it for the starting value. The file is read only at the commits a
    bisection needs, at most once each: a commit range whose two ends hold the
    same value is taken to hold no change.

    Args:
        name: Program name, e.g. ``dfe``.
        since: ISO 8601 timestamp. Only commits after it count.
        manifest: The manifest to read, :func:`load_manifest` when None.

    Returns:
        The releases, each with the version, commit date and SHA.

    Raises:
        ProgramError: The program is unknown, or the commit list or the file
            at a commit could not be read.

    """
    from hyperi_ci import gh

    source = program_info(name, manifest=manifest).released
    before = _commits(source, f"until={since}&per_page=1", paginate=False)[:1]
    after = _commits(source, f"since={since}&per_page=100", paginate=True)
    seen = {sha for sha, _ in before}
    history = [*before, *(c for c in reversed(after) if c[0] not in seen)]
    if not history:
        return ()

    values: dict[str, object] = {}

    def value(index: int) -> object:
        sha = history[index][0]
        if sha not in values:
            try:
                text = gh.read_repo_file(source.repo, source.path, ref=sha)
            except (gh.RepoFileError, OSError) as exc:
                raise ProgramError(
                    f"could not read {source.repo}:{source.path} at {sha}: {exc}"
                ) from exc
            try:
                values[sha] = _at_key(yaml.safe_load(text or ""), source)
            except (yaml.YAMLError, ProgramError):
                values[sha] = None
        return values[sha]

    def changes(low: int, high: int) -> list[int]:
        if value(low) == value(high):
            return []
        if high - low == 1:
            return [high]
        middle = (low + high) // 2
        return changes(low, middle) + changes(middle, high)

    moved = changes(0, len(history) - 1)
    if before:
        top = ga_version(value(0))
    else:
        top = None
        moved.insert(0, 0)
    releases: list[ProgramRelease] = []
    for index in moved:
        version = ga_version(value(index))
        if version is not None and (top is None or version > top):
            sha, date = history[index]
            releases.append(ProgramRelease(str(value(index)), date, sha))
            top = version
    return tuple(releases)


def horizon_line(version: str, horizon: str) -> str:
    """Return the release line ``horizon`` names, relative to ``version``.

    Each horizon is the next unreleased version of its kind. A prerelease's
    own version is unreleased, so it counts for the kind it is: a patch when
    PATCH is non-zero, a minor when only MINOR is, a major otherwise.

    Formats: next-patch is ``MAJOR.MINOR.PATCH``, next-minor ``MAJOR.MINOR``,
    next-major ``MAJOR.0``. Build metadata is ignored.

    Args:
        version: Semver, ``v`` prefix optional, e.g. ``2.2.1-rc.17``.
        horizon: ``next-patch``, ``next-minor`` or ``next-major``.

    Returns:
        The line. From ``2.2.1-rc.17``: ``2.2.1``, ``2.3``, ``3.0``. From
        ``2.2.0``: ``2.2.1``, ``2.3``, ``3.0``. From ``3.0.0-rc.1``: ``3.0.1``,
        ``3.1``, ``3.0``.

    Raises:
        ProgramError: ``version`` is not semver, or ``horizon`` does not
            resolve to a line.

    """
    match = _VERSION.match(version)
    if match is None:
        raise ProgramError(f"{version!r} is not a MAJOR.MINOR.PATCH version")
    major, minor, patch = (int(part) for part in match.group(1, 2, 3))
    prerelease = match.group(4) is not None
    if horizon == "next-patch":
        if prerelease and patch:
            return f"{major}.{minor}.{patch}"
        return f"{major}.{minor}.{patch + 1}"
    if horizon == "next-minor":
        if prerelease and minor and not patch:
            return f"{major}.{minor}"
        return f"{major}.{minor + 1}"
    if horizon == "next-major":
        if prerelease and not minor and not patch:
            return f"{major}.0"
        return f"{major + 1}.0"
    raise ProgramError(
        f"{horizon!r} has no release line, only {', '.join(PROGRAM_HORIZONS)} do"
    )


def resolve_program_horizon(
    name: str, horizon: str, *, manifest: Manifest | None = None
) -> str:
    """Return the concrete release line ``<name>:<horizon>`` means right now.

    Resolve once per program and horizon, not once per item: every item with
    the label shares the answer. Format and rules are :func:`horizon_line`'s.

    Args:
        name: Program name, e.g. ``dfe``.
        horizon: One of the program's horizons.
        manifest: The manifest to read, :func:`load_manifest` when None.

    Returns:
        The line, e.g. ``2.2.1`` for dfe next-patch while ``current`` is
        ``2.2.1-rc.17``.

    Raises:
        ProgramError: The program is unknown, does not carry ``horizon``, or
            its version source is unusable.

    """
    manifest = _given(manifest)
    if horizon not in program_info(name, manifest=manifest).horizons:
        raise ProgramError(f"program {name!r} has no {horizon!r} horizon")
    return horizon_line(program_version(name, manifest=manifest), horizon)


def _program_json(program: Program) -> dict[str, Any]:
    docs: dict[tuple[str, str], object] = {}
    reasons: list[str] = []

    def semver(source: Source, parse: Callable[[object], str]) -> str:
        """Read ``source``, once per file, recording why on failure."""
        try:
            file = (source.repo, source.path)
            if file not in docs:
                docs[file] = _read_source(source)
            return parse(docs[file])
        except NoReleaseYetError:
            raise
        except ProgramError as exc:
            if str(exc) not in reasons:
                reasons.append(str(exc))
            return ""

    version = semver(program.version, lambda doc: _semver_at(program.version, doc))
    try:
        released = semver(program.released, lambda doc: _released_at(program, doc))
        released_state = "released" if released else "error"
    except NoReleaseYetError:
        released, released_state = "", "none"
    lines = {h: horizon_line(version, h) for h in program.horizons} if version else {}
    return {
        "labels": [
            {
                "name": label.name,
                "horizon": horizon,
                "color": label.color,
                "description": label.description,
            }
            for label, horizon in zip(program.labels, program.horizons, strict=True)
        ],
        "membership_source": _source_json(program.members),
        "version_source": _source_json(program.version),
        "version": version,
        "lines": lines,
        "released_source": _source_json(program.released),
        "released": released,
        "released_state": released_state,
        "error": "; ".join(reasons),
    }


def _source_json(source: Source) -> dict[str, str]:
    return {"repo": source.repo, "path": source.path, "key": source.key}


def manifest_json(
    areas: Mapping[str, Iterable[str]] | None = None,
    *,
    manifest: Manifest | None = None,
) -> dict[str, Any]:
    """Return the manifest, plus a repo's areas, as a JSON-ready dict.

    The shape is a contract for consumers that cannot import this module.
    ``schema`` changes whenever a key is removed or changes type.

    ``programs`` carries each program's resolved ``lines``, so a consumer
    reads the concrete release line here rather than resolving per item. It
    reads each source file once through ``gh api``. A source that cannot be
    read leaves its value empty, ``lines`` too for the version source, and
    says why in ``error``. ``released_state`` is ``released``, ``none`` where
    the released value is empty (no GA yet, not an error), or ``error``.

    ``source`` is :meth:`ManifestSource.as_json`: which manifest this is, and
    the ``.github`` commit it was read at.

    Args:
        areas: The repo's validated areas, empty when it declares none.
        manifest: The manifest to render, :func:`load_manifest` when None.

    Returns:
        A dict of plain lists, dicts and strings.

    """
    manifest = _given(manifest)

    def labels(section: tuple[Label, ...]) -> list[dict[str, str]]:
        return [
            {"name": x.name, "color": x.color, "description": x.description}
            for x in section
        ]

    return {
        "schema": JSON_SCHEMA,
        "types": [
            {"name": t.name, "use_for": t.use_for, "not_for": t.not_for}
            for t in manifest.types
        ],
        "gate": labels(manifest.gate),
        "waiting": labels(manifest.waiting),
        "flags": labels(manifest.flags),
        "community": labels(manifest.community),
        "bot_owned": list(manifest.bot_owned),
        "allowed": {
            "patterns": list(manifest.allowed_patterns),
            "literals": list(manifest.allowed_literals),
        },
        "area": {"prefix": manifest.area_prefix, "color": manifest.area_color},
        "retired": dict(manifest.retired),
        "retired_description": manifest.retired_template,
        "areas": {name: list(paths) for name, paths in (areas or {}).items()},
        "horizons": [
            {
                "name": h.name,
                "rank": h.rank,
                "set_by": h.set_by,
                "description": h.description,
            }
            for h in manifest.horizons
        ],
        "programs": {
            name: _program_json(program) for name, program in manifest.programs.items()
        },
        "source": manifest.source.as_json(),
    }


def retired_labels(*, manifest: Manifest | None = None) -> dict[str, str]:
    """Return retired label name -> what replaces it."""
    return dict(_given(manifest).retired)


def retired_description(replacement: str, *, manifest: Manifest | None = None) -> str:
    """Return the description a retired label carries, ``RETIRED: use <new>``."""
    return _given(manifest).retired_template.format(replacement=replacement)
