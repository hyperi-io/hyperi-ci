# Project:   HyperI CI
# File:      src/hyperi_ci/labels.py
# Purpose:   The single reader for the org issue label manifest
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Read the org issue label manifest shipped in ``config/labels.yaml``.

Other repos import this module rather than copying names: hyperi-infra's label
reconcile creates what :func:`expected_labels` returns, and dfe-sweep reads the
gate through :func:`gate_of`. It imports stdlib and ``yaml`` only.

Label names compare case-insensitively, as GitHub treats them.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

MANIFEST_FILE = Path(__file__).resolve().parent / "config" / "labels.yaml"

# GitHub refuses a longer label description.
MAX_DESCRIPTION = 100

# Version of the `labels --json` shape, raised when a key is removed or retyped.
JSON_SCHEMA = 1

_AREA_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_COLOR = re.compile(r"^[0-9a-fA-F]{6}$")


class ManifestError(ValueError):
    """The label manifest is malformed."""


class AreasError(ValueError):
    """A repo's ``issues.areas`` block is malformed."""


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
class Manifest:
    """The parsed manifest.

    Attributes:
        types: Org issue types.
        gate: Gate labels, strictest first.
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

    """

    types: tuple[IssueType, ...]
    gate: tuple[Label, ...]
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


def _parse(raw: object) -> Manifest:
    if not isinstance(raw, Mapping):
        raise ManifestError("the manifest must be a mapping")
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
    manifest = Manifest(
        types=_types(raw),
        gate=_labels(raw, "gate"),
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


def load_manifest(path: Path | None = None) -> Manifest:
    """Load and validate a label manifest.

    Args:
        path: Manifest file. Defaults to the one shipped in the package, which
            is parsed once per process.

    Returns:
        The parsed :class:`Manifest`.

    Raises:
        ManifestError: The file is unreadable, not YAML, or malformed.

    """
    if path is None or path == MANIFEST_FILE:
        return _shipped()
    return _read(path)


@lru_cache(maxsize=1)
def _shipped() -> Manifest:
    return _read(MANIFEST_FILE)


def _read(path: Path) -> Manifest:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ManifestError(f"{path}: {exc}") from exc
    try:
        return _parse(raw)
    except ManifestError as exc:
        raise ManifestError(f"{path}: {exc}") from exc


def gate_labels() -> tuple[str, ...]:
    """Return the gate label names, strictest first."""
    return tuple(label.name for label in load_manifest().gate)


def _gates_present(labels: Iterable[str]) -> tuple[str, ...]:
    present = {name.casefold() for name in labels}
    return tuple(name for name in gate_labels() if name.casefold() in present)


def gate_of(labels: Iterable[str]) -> str | None:
    """Return the issue's gate: the strictest gate label it carries.

    Args:
        labels: The issue's label names.

    Returns:
        The manifest's spelling of the strictest gate label, or None when the
        issue carries none, which means untriaged.

    """
    found = _gates_present(labels)
    return found[0] if found else None


def conflicts(labels: Iterable[str]) -> tuple[str, ...]:
    """Return the gate labels an issue carries when it carries more than one.

    Args:
        labels: The issue's label names.

    Returns:
        Every gate label present, strictest first, or an empty tuple when
        there is one or none.

    """
    found = _gates_present(labels)
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


def area_label(name: str) -> str:
    """Return the label name for area ``name``, e.g. ``area/auth``."""
    return f"{load_manifest().area_prefix}{name}"


def area_description(paths: Iterable[str]) -> str:
    """Return an area label's description: its paths, cut to GitHub's limit."""
    text = ", ".join(paths)
    if len(text) <= MAX_DESCRIPTION:
        return text
    return text[: MAX_DESCRIPTION - 3].rstrip(", ") + "..."


def expected_labels(
    areas: Mapping[str, Iterable[str]] | None = None,
) -> tuple[Label, ...]:
    """Return every label a repo must carry.

    Gate, waiting and flag labels, then one ``area/<name>`` per declared area.
    Community labels are optional and bot-owned ones belong to the bot, so
    neither is included.

    Args:
        areas: The repo's validated areas, as :func:`parse_areas` returns.

    Returns:
        The labels, each with its exact colour and description.

    """
    manifest = load_manifest()
    labels = [*manifest.gate, *manifest.waiting, *manifest.flags]
    for name, paths in (areas or {}).items():
        labels.append(
            Label(area_label(name), manifest.area_color, area_description(paths))
        )
    return tuple(labels)


def is_allowed(name: str) -> bool:
    """Return True when another standard declares label ``name``.

    Such a label is accepted on a repo but never created by the reconcile.
    """
    manifest = load_manifest()
    if name.casefold() in {lit.casefold() for lit in manifest.allowed_literals}:
        return True
    return any(re.fullmatch(p, name) for p in manifest.allowed_patterns)


def manifest_json(areas: Mapping[str, Iterable[str]] | None = None) -> dict[str, Any]:
    """Return the manifest, plus a repo's areas, as a JSON-ready dict.

    The shape is a contract for consumers that cannot import this module.
    ``schema`` changes whenever a key is removed or changes type.

    Args:
        areas: The repo's validated areas, empty when it declares none.

    Returns:
        A dict of plain lists, dicts and strings.

    """
    manifest = load_manifest()

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
    }


def retired_labels() -> dict[str, str]:
    """Return retired label name -> what replaces it."""
    return dict(load_manifest().retired)


def retired_description(replacement: str) -> str:
    """Return the description a retired label carries, ``RETIRED: use <new>``."""
    return load_manifest().retired_template.format(replacement=replacement)
