# Project:   HyperI CI
# File:      src/hyperi_ci/licence_migrate.py
# Purpose:   Bring a repo to the licence its declared classification ships under
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Bring a repo to the licence its declared classification ships under.

The classification is the human's choice, so this only conforms a repo to it:
the bundle's licence files, the ``License:`` and ``SPDX-License-Identifier``
header lines, and the root manifest licence fields. A header is changed only
where its copyright names HYPERI PTY LIMITED, and a copied-in directory
(``quality.dep_licenses.vendored``) never is.
"""

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import tomlkit

from hyperi_ci.common import run_cmd

# A header block is the top of the file.
_HEADER_LINES = 15

_OURS = re.compile(r"copyright.*HYPERI PTY LIMITED", re.IGNORECASE)
_LICENSE_LINE = re.compile(
    r"^(?P<lead>[#/*!;<\-\s]*License:\s+)(?P<value>.+?)"
    r"(?P<tail>\s+-{1,2}\s+HYPERI PTY LIMITED.*)?$"
)
_SPDX_LINE = re.compile(
    r"^(?P<lead>[#/*!;<\-\s]*SPDX-License-Identifier:\s*)(?P<value>\S+)(?P<tail>.*)$"
)
_BUNDLE_FILE = re.compile(r"(LICEN[CS]E|COPYING|COMMERCIAL\.md|AI-TRAINING-POLICY\.md)")

# The year placeholder in a bundle template.
_YEAR = b"<YEAR>"

# Manifest key prefix -> the root file it names and the table the key sits in.
_MANIFESTS = (
    ("workspace.package.", "Cargo.toml", ("workspace", "package")),
    ("package.", "Cargo.toml", ("package",)),
    ("project.", "pyproject.toml", ("project",)),
    (".", "package.json", ()),
)


class MigrateError(RuntimeError):
    """The repo cannot be brought to its licence."""


@dataclass(frozen=True, slots=True)
class Change:
    """One value the migration changes."""

    path: str
    before: str
    after: str


@dataclass(slots=True)
class Migration:
    """Everything a migration would write, and what it leaves for a human."""

    licence: str
    changes: list[Change] = field(default_factory=list)
    writes: dict[str, bytes] = field(default_factory=dict)
    leftovers: list[str] = field(default_factory=list)


def rewrite_header(
    text: str, licence: str, own: frozenset[str]
) -> tuple[str, list[tuple[str, str]]]:
    """Return ``text`` with its header's licence lines set to ``licence``.

    Only a value in ``own`` is replaced: another project's licence on a file
    we copied in stays.

    Returns:
        The new text, and each ``(before, after)`` value changed. Unchanged
        when the header's copyright does not name HYPERI PTY LIMITED.

    """
    lines = text.splitlines(keepends=True)
    head = lines[:_HEADER_LINES]
    if not any(_OURS.search(line) for line in head):
        return text, []
    changed: list[tuple[str, str]] = []
    for i, line in enumerate(head):
        body = line.rstrip("\r\n")
        ending = line[len(body) :]
        match = _SPDX_LINE.match(body) or _LICENSE_LINE.match(body)
        if match is None:
            continue
        value = match["value"].strip()
        if value == licence or value not in own:
            continue
        changed.append((value, licence))
        lines[i] = f"{match['lead']}{licence}{match['tail'] or ''}{ending}"
    return "".join(lines), changed


def _fields_for(
    name: str, fields: Mapping[str, str]
) -> list[tuple[tuple[str, ...], str, Any]]:
    """Return ``(table path, key, value)`` for the fields manifest ``name`` takes."""
    wanted = []
    for key, raw in fields.items():
        value = json.loads(raw) if raw.startswith("[") else raw
        for prefix, file_name, path in _MANIFESTS:
            if file_name == name and key.startswith(prefix):
                wanted.append((path, key[len(prefix) :], value))
                break
    return wanted


def rewrite_manifest(
    name: str, text: str, fields: Mapping[str, str]
) -> tuple[str, list[tuple[str, str]]]:
    """Return a root manifest with ``fields`` set, comments and layout kept.

    Only tables the manifest already has are touched. Setting ``license-file``
    drops a ``license`` beside it, which cargo would otherwise read first.

    Returns:
        The new text, and each ``(before, after)`` value changed.

    """
    wanted = _fields_for(name, fields)
    changed: list[tuple[str, str]] = []
    if name == "package.json":
        document = json.loads(text)
        for _, key, value in wanted:
            if document.get(key) != value:
                changed.append((str(document.get(key)), str(value)))
                document[key] = value
        return (json.dumps(document, indent=2) + "\n" if changed else text), changed
    document = tomlkit.parse(text)
    keys = {(path, key) for path, key, _ in wanted}
    for path, key, value in wanted:
        table: Any = document
        for part in path:
            table = table.get(part) if table is not None else None
        if table is None:
            continue
        if (
            key == "license-file"
            and "license" in table
            and (path, "license") not in keys
        ):
            changed.append((str(table["license"]), "(removed)"))
            del table["license"]
        current = table.get(key)
        if current is None or current.unwrap() != value:
            changed.append((str(current), str(value)))
            table[key] = value
    return (tomlkit.dumps(document) if changed else text), changed


def _gh(*args: str) -> bytes:
    result = run_cmd(["gh", "api", *args], check=False, capture=True)
    if result.returncode != 0:
        raise MigrateError(f"gh api {args[-1]}: {result.stderr.strip()}")
    return result.stdout.encode("utf-8")


def fetch_bundle(source: str) -> dict[str, bytes]:
    """Return the licence files of bundle ``source`` (``owner/repo[/dir]``) at HEAD.

    Raises:
        MigrateError: gh failed, or the bundle holds no licence file.

    """
    owner, repo, *rest = source.strip("/").split("/")
    where = f"repos/{owner}/{repo}/contents" + "".join(f"/{part}" for part in rest)
    listing = json.loads(_gh(where))
    names = [
        str(entry["name"])
        for entry in listing
        if entry.get("type") == "file" and _BUNDLE_FILE.match(str(entry["name"]))
    ]
    if not names:
        raise MigrateError(f"{source} holds no licence file")
    raw = "Accept: application/vnd.github.raw+json"
    return {name: _gh("-H", raw, f"{where}/{name}") for name in names}


def _with_year(template: bytes, current: bytes | None) -> bytes:
    """Fill ``<YEAR>``, keeping the year a matching copy already carries."""
    if _YEAR not in template:
        return template
    pattern = rb"(\d{4})".join(re.escape(part) for part in template.split(_YEAR))
    found = re.fullmatch(pattern, current or b"", re.DOTALL)
    year = found.group(1) if found else str(datetime.now(UTC).year).encode()
    return template.replace(_YEAR, year)


def _read(path: Path, written: bytes | None) -> str | None:
    try:
        return written.decode("utf-8") if written else path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def plan(
    root: Path,
    files: Iterable[str],
    entry: Mapping[str, Any],
    own: Iterable[str],
    vendored: Iterable[str] = (),
    fetch: Callable[[str], dict[str, bytes]] = fetch_bundle,
) -> Migration:
    """Work out what bringing ``root`` to its outbound ``entry`` would write.

    Args:
        root: The repo root.
        files: The repo's tracked files, relative to ``root``.
        entry: The classification's outbound entry from licensing.yaml.
        own: HyperI's own licences, the only header values replaced.
        vendored: Copied-in directories, never touched.
        fetch: Returns a bundle's licence files by name.

    Returns:
        The migration. Nothing is written.

    Raises:
        MigrateError: The classification has no licence to migrate to.

    """
    licences = list(entry.get("licences") or ())
    if not licences or licences[0] == "inherit":
        raise MigrateError(
            "a fork keeps its upstream licence, so there is nothing to do"
        )
    migration = Migration(licences[0])
    ours = frozenset(own)
    skip = tuple(f"{path.rstrip('/')}/" for path in vendored)
    names = sorted(name for name in files if not name.startswith(skip))

    def record(name: str, content: bytes, changed: Iterable[tuple[str, str]]) -> None:
        migration.writes[name] = content
        migration.changes += [Change(name, before, after) for before, after in changed]

    if bundle := entry.get("bundle"):
        for name, template in sorted(fetch(str(bundle)).items()):
            current = (root / name).read_bytes() if (root / name).is_file() else None
            content = _with_year(template, current)
            if content != current:
                record(
                    name,
                    content,
                    [("differs" if current else "missing", "bundle copy")],
                )

    for name in ("Cargo.toml", "pyproject.toml", "package.json"):
        text = _read(root / name, None) if (root / name).is_file() else None
        if text is not None:
            new, changed = rewrite_manifest(name, text, entry.get("manifest") or {})
            if changed:
                record(name, new.encode("utf-8"), changed)

    for name in names:
        text = _read(root / name, migration.writes.get(name))
        if text is None:
            continue
        new, changed = rewrite_header(text, migration.licence, ours)
        if changed:
            record(name, new.encode("utf-8"), changed)

    replaced = sorted({c.before for c in migration.changes} & ours)
    if replaced:
        alternatives = "|".join(re.escape(licence) for licence in replaced)
        mention = re.compile(rf"(?<![\w.-])(?:{alternatives})(?![\w.-])")
        for name in names:
            text = _read(root / name, migration.writes.get(name)) or ""
            for number, line in enumerate(text.splitlines(), 1):
                if mention.search(line):
                    migration.leftovers.append(f"{name}:{number}")
    return migration


def apply(root: Path, migration: Migration) -> None:
    """Write every file the migration changes."""
    for name, content in migration.writes.items():
        (root / name).write_bytes(content)


def tracked_files(root: Path) -> list[str]:
    """Return the repo's tracked files, relative to ``root``.

    Raises:
        MigrateError: ``root`` is not a git checkout.

    """
    result = run_cmd(
        ["git", "-C", str(root), "ls-files", "-z"], check=False, capture=True
    )
    if result.returncode != 0:
        raise MigrateError(f"{root} is not a git checkout")
    return [name for name in result.stdout.split("\0") if name]
