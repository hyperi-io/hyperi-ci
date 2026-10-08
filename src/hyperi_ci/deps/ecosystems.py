# Project:   HyperI CI
# File:      src/hyperi_ci/deps/ecosystems.py
# Purpose:   Declared floor vs locked version, per dependency group
# Origin:    Derek's deps automation scripts, merged into hyperi-ci now they are
#            mature enough for people (and hyperi-ai's /deps) to use directly
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Floor-vs-lock drift, per ecosystem, per dependency group.

Renovate's ``rangeStrategy: bump`` only moves a floor when a new release opens
a PR, so a floor years behind its own lock is never reported there.

Every manifest in the tree is parsed in one pass, not just the primary
language's, and each ecosystem is reported separately. Locked versions come
from parsing the lockfile. An installed toolchain, run offline, may only ADD
rows the parse missed, and its absence is silent. Each locked version records
its ``source``: ``parse`` or the tool's name.
"""

import json
import shutil
import subprocess
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from scalo.logger import logger

from hyperi_ci.deps import versions as ver
from hyperi_ci.deps.surfaces import Surface, load, repo_files


@dataclass
class Ecosystem:
    """One manifest and the lock that resolves it."""

    name: str
    manifest: str
    lock: str
    groups: list[dict] = field(default_factory=list)
    declared: int = 0
    compared: int = 0
    # Set when a lock was found but not read; surfaced in the report's notes.
    note: str = ""


# ---------------------------------------------------------------------------
# File loading
# ---------------------------------------------------------------------------


def _load_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def walk_groups(data: dict, path: str) -> Iterator[tuple[str, object]]:
    """Yield ``(concrete_path, value)`` for a dotted path, ``*`` matching any key.

    A wildcard yields each extra or group separately, so dev and runtime are
    never merged.
    """
    parts = path.split(".")

    def walk(
        node: object, index: int, trail: list[str]
    ) -> Iterator[tuple[str, object]]:
        if index == len(parts):
            yield ".".join(trail), node
            return
        if not isinstance(node, dict):
            return
        part = parts[index]
        if part == "*":
            for key, child in node.items():
                yield from walk(child, index + 1, [*trail, str(key)])
            return
        for key, child in node.items():
            if key == part:
                yield from walk(child, index + 1, [*trail, part])
                return

    yield from walk(data, 0, [])


def group_entries(value: object) -> list[tuple[str, str]]:
    """Flatten a dependency group to ``[(name, constraint)]``.

    Accepts a list of PEP 508 strings, a map of name to constraint, or a map of
    name to table carrying ``version``.
    """
    out: list[tuple[str, str]] = []
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, str):
                continue  # a PEP 735 include-group table carries no version
            name, spec = ver.split_requirement(item)
            if name:
                out.append((name, spec))
        return out
    if isinstance(value, dict):
        for key, raw in value.items():
            if isinstance(raw, str):
                out.append((str(key), raw))
            elif isinstance(raw, dict):
                version = raw.get("version")
                if isinstance(version, str):
                    # Cargo's `package` rename names the crate the lock records.
                    real = raw.get("package")
                    out.append(
                        (str(real) if isinstance(real, str) else str(key), version)
                    )
    return out


def packages_from_toml_lock(path: Path) -> dict[str, str]:
    """Return ``[[package]]`` name to version from uv.lock, poetry.lock or Cargo.lock.

    A name locked more than once keeps its highest version, the one a floor
    has to cover.
    """
    out: dict[str, str] = {}
    for package in _load_toml(path).get("package") or []:
        if not isinstance(package, dict):
            continue
        name, version = package.get("name"), package.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            continue
        current = out.get(name)
        if current is None or (ver.parse(version) or ()) > (ver.parse(current) or ()):
            out[name] = version
    return out


def packages_from_npm_lock(path: Path) -> dict[str, str]:
    """Return name to version from a package-lock.json v2/v3 ``packages`` map.

    The shallowest ``node_modules/`` key wins, as the copy the manifest's
    constraint governs. A v1 lockfile yields nothing; ``npm ls`` enrichment
    covers it when npm is installed.
    """
    packages = _load_json(path).get("packages")
    if not isinstance(packages, dict):
        return {}
    best: dict[str, tuple[int, str]] = {}
    marker = "node_modules/"
    for key, meta in packages.items():
        if not key or not isinstance(meta, dict):
            continue
        cut = key.rfind(marker)
        if cut < 0:
            continue
        name = key[cut + len(marker) :]
        version = meta.get("version")
        if not name or not isinstance(version, str):
            continue
        depth = key.count(marker)
        current = best.get(name)
        if current is None or depth < current[0]:
            best[name] = (depth, version)
    return {name: version for name, (_, version) in best.items()}


def _yarn_descriptor_name(descriptor: str) -> str:
    """``@scope/pkg@npm:^1.2.3`` -> ``@scope/pkg``.

    Splits at the last ``@`` past index 0, which a scoped name starts with.
    """
    descriptor = descriptor.strip().strip('"')
    cut = descriptor.rfind("@")
    return descriptor[:cut] if cut > 0 else descriptor


def packages_from_yarn_lock(path: Path) -> dict[str, str]:
    """Return name to version from a Yarn Berry ``yarn.lock``.

    Each YAML key is a comma-joined descriptor list resolving to one version,
    so the first descriptor names it. A name under several keys keeps its
    highest version. Yarn Classic (v1) is not YAML and yields nothing.
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}

    out: dict[str, str] = {}
    for key, meta in data.items():
        if key == "__metadata" or not isinstance(meta, dict):
            continue
        version = meta.get("version")
        if not isinstance(version, str):
            continue
        name = _yarn_descriptor_name(str(key).split(",")[0])
        if not name:
            continue
        current = out.get(name)
        if current is None or (ver.parse(version) or ()) > (ver.parse(current) or ()):
            out[name] = version
    return out


# A lock missing here is reported as unparsed, since an empty read looks like
# "no drift".
NODE_LOCK_PARSERS: dict[str, Callable[[Path], dict[str, str]]] = {
    "package-lock.json": packages_from_npm_lock,
    "yarn.lock": packages_from_yarn_lock,
}


def find_lock(start: Path, root: Path, names: tuple[str, ...]) -> Path | None:
    """Return the first lockfile walking from ``start`` up to ``root``, or None.

    A workspace member's lock sits at the workspace root, not beside it.
    """
    current = start.resolve()
    root = root.resolve()
    while True:
        for name in names:
            candidate = current / name
            if candidate.is_file():
                return candidate
        if current == root or current.parent == current:
            return None
        current = current.parent


# ---------------------------------------------------------------------------
# Enrichment -- optional and additive
# ---------------------------------------------------------------------------


def _run_tool(cmd: list[str], cwd: Path) -> str | None:
    """Run an optional toolchain command and return its stdout, or None.

    A missing tool returns None silently. A timeout, launch failure or
    non-zero exit returns None and logs why at debug level.
    """
    if shutil.which(cmd[0]) is None:
        return None
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        logger.debug(f"{cmd[0]} timed out after {exc.timeout}s: {' '.join(cmd)}")
        return None
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug(f"{cmd[0]} failed to start: {exc}")
        return None
    if proc.returncode != 0:
        logger.debug(
            f"{cmd[0]} exited {proc.returncode}: {proc.stderr.strip()[-2000:]}"
        )
        return None
    return proc.stdout


def enrich_cargo(cwd: Path) -> dict[str, str]:
    """Read resolved crate versions from ``cargo metadata``, offline.

    Covers a member whose Cargo.lock sits above the scan root.
    """
    out = _run_tool(["cargo", "metadata", "--format-version", "1", "--offline"], cwd)
    if out is None:
        return {}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {}
    return {
        pkg["name"]: pkg["version"]
        for pkg in data.get("packages") or []
        if isinstance(pkg.get("name"), str) and isinstance(pkg.get("version"), str)
    }


def enrich_uv(cwd: Path) -> dict[str, str]:
    """Read resolved distributions from ``uv export --frozen``.

    ``--frozen`` reads the existing lock without resolving, so it never
    reaches the network or rewrites the lock.
    """
    out = _run_tool(
        [
            "uv",
            "export",
            "--frozen",
            "--no-hashes",
            "--all-extras",
            "--all-groups",
            "--no-emit-project",
            "--quiet",
        ],
        cwd,
    )
    if out is None:
        return {}
    resolved: dict[str, str] = {}
    for line in out.splitlines():
        head = line.split(";", 1)[0].strip()
        if not head or head[0] in "-#":
            continue
        name, _, version = head.partition("==")
        if name and version:
            resolved[name.strip()] = version.strip()
    return resolved


def enrich_npm(cwd: Path) -> dict[str, str]:
    """Installed tree from ``npm ls --package-lock-only``, no network."""
    out = _run_tool(["npm", "ls", "--json", "--all", "--package-lock-only"], cwd)
    if out is None:
        return {}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {}
    resolved: dict[str, str] = {}

    def collect(node: object) -> None:
        if not isinstance(node, dict):
            return
        children = node.get("dependencies")
        if not isinstance(children, dict):
            return
        for name, meta in children.items():
            if not isinstance(meta, dict):
                continue
            version = meta.get("version")
            # An unmet peer dep has no version and no resolved children.
            if isinstance(version, str):
                resolved.setdefault(str(name), version)
                collect(meta)

    collect(data)
    return resolved


def _locked_map(
    parsed: dict[str, str],
    enriched: dict[str, str],
    tool: str,
    norm: Callable[[str], str],
) -> dict[str, tuple[str, str]]:
    """Merge parse and tool results. The parse always wins; the tool only adds."""
    merged: dict[str, tuple[str, str]] = {
        norm(name): (version, "parse") for name, version in parsed.items()
    }
    for name, version in enriched.items():
        merged.setdefault(norm(name), (version, tool))
    return merged


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def _compare(
    eco: Ecosystem,
    group_path: str,
    entries: list[tuple[str, str]],
    locked: dict[str, tuple[str, str]],
    norm: Callable[[str], str],
) -> None:
    """Append one group's entries to ``eco``, each with its locked version.

    An entry with no floor or no lock hit is kept with empty columns; only
    entries with both count as compared.
    """
    rows: list[dict] = []
    comparable = 0
    for name, constraint in entries:
        floor = ver.floor_of(constraint)
        version, source = locked.get(norm(name), ("", ""))
        kind = ver.drift_kind(floor, version) if floor and version else None
        if floor and version:
            comparable += 1
        rows.append(
            {
                "dep": name,
                "constraint": constraint,
                "floor": floor or "",
                "locked": version,
                "source": source,
                "drift": kind,
            }
        )
    if not rows:
        return
    eco.declared += len(rows)
    eco.compared += comparable
    eco.groups.append({"group": group_path, "entries": rows})


def python_ecosystem(root: Path, rel: str, by_id: dict[str, Surface]) -> Ecosystem:
    """pyproject.toml against uv.lock / poetry.lock (plus uv, when installed)."""
    manifest = root / rel
    data = _load_toml(manifest)
    lock_names = tuple(dict.fromkeys(by_id["pep621"].lock + by_id["poetry"].lock))
    lock = find_lock(manifest.parent, root, lock_names)
    parsed = packages_from_toml_lock(lock) if lock is not None else {}
    locked = _locked_map(parsed, enrich_uv(manifest.parent), "uv", ver.norm_python)

    eco = Ecosystem(
        name="python",
        manifest=rel,
        lock=lock.relative_to(root).as_posix() if lock is not None else "",
    )
    # One pyproject belongs to both pep621 and poetry, so both group sets run.
    for group_path in list(by_id["pep621"].groups) + list(by_id["poetry"].groups):
        for concrete, value in walk_groups(data, group_path):
            _compare(eco, concrete, group_entries(value), locked, ver.norm_python)
    return eco


def rust_ecosystem(root: Path, rel: str, by_id: dict[str, Surface]) -> Ecosystem:
    """Cargo.toml against Cargo.lock (plus cargo metadata, when installed)."""
    manifest = root / rel
    data = _load_toml(manifest)
    lock = find_lock(manifest.parent, root, by_id["cargo"].lock)
    parsed = packages_from_toml_lock(lock) if lock is not None else {}
    locked = _locked_map(parsed, enrich_cargo(manifest.parent), "cargo", ver.norm_cargo)

    eco = Ecosystem(
        name="rust",
        manifest=rel,
        lock=lock.relative_to(root).as_posix() if lock is not None else "",
    )
    for group_path in by_id["cargo"].groups:
        for concrete, value in walk_groups(data, group_path):
            _compare(eco, concrete, group_entries(value), locked, ver.norm_cargo)
    return eco


def node_ecosystem(root: Path, rel: str, by_id: dict[str, Surface]) -> Ecosystem:
    """package.json against whichever lock the npm surface names.

    A lock with no entry in :data:`NODE_LOCK_PARSERS` sets ``note`` rather
    than reading as clean.
    """
    manifest = root / rel
    data = _load_json(manifest)
    lock = find_lock(manifest.parent, root, tuple(by_id["npm"].lock))
    parser = NODE_LOCK_PARSERS.get(lock.name) if lock is not None else None
    parsed = parser(lock) if (parser is not None and lock is not None) else {}
    locked = _locked_map(parsed, enrich_npm(manifest.parent), "npm", ver.norm_npm)

    eco = Ecosystem(
        name="node",
        manifest=rel,
        lock=lock.relative_to(root).as_posix() if lock is not None else "",
    )
    if lock is not None and parser is None:
        eco.note = (
            f"{lock.name} has no parser, so {rel} was compared against an "
            "empty lock. Reported rather than passed silently."
        )
    for group_path in by_id["npm"].groups:
        for concrete, value in walk_groups(data, group_path):
            _compare(eco, concrete, group_entries(value), locked, ver.norm_npm)
    return eco


_BUILDERS: dict[str, Callable[[Path, str, dict[str, Surface]], Ecosystem]] = {
    "pyproject.toml": python_ecosystem,
    "Cargo.toml": rust_ecosystem,
    "package.json": node_ecosystem,
}


def drift(
    root: Path,
    surfaces: tuple[Surface, ...] | None = None,
    files: list[str] | None = None,
) -> dict:
    """Audit every declared floor against the version actually locked.

    No update bot raises this: an open ``>=`` range admits every future
    release, so there is no manifest edit to propose.

    Args:
        root: Repository root.
        surfaces: Override catalogue (tests). Defaults to the shipped one.
        files: Pre-enumerated repo-relative paths.

    Returns:
        A report dict: per-ecosystem group breakdowns (every declared entry,
        drifted or not), the flat drift list, and notes for what was skipped.

    """
    root = Path(root).resolve()
    catalogue = surfaces if surfaces is not None else load()
    by_id = {surface.id: surface for surface in catalogue}
    if files is None:
        files, _ = repo_files(root)

    ecosystems = [
        _BUILDERS[Path(rel).name](root, rel, by_id)
        for rel in files
        if Path(rel).name in _BUILDERS
    ]

    notes: list[str] = [eco.note for eco in ecosystems if eco.note]
    if any(Path(rel).name == "go.mod" for rel in files):
        notes.append(
            "go.mod records an exact version per module, so there is no "
            "declared floor to drift from the lock -- Go is skipped here, not "
            "silently omitted."
        )

    flat = [
        {
            "ecosystem": eco.name,
            "manifest": eco.manifest,
            "group": group["group"],
            **entry,
        }
        for eco in ecosystems
        for group in eco.groups
        for entry in group["entries"]
        if entry["drift"]
    ]
    return {
        "root": str(root),
        "ecosystems": [
            {
                "name": eco.name,
                "manifest": eco.manifest,
                "lock": eco.lock,
                "declared": eco.declared,
                "compared": eco.compared,
                "groups": eco.groups,
            }
            for eco in ecosystems
        ],
        "drift": flat,
        "declared": sum(eco.declared for eco in ecosystems),
        "compared": sum(eco.compared for eco in ecosystems),
        "notes": notes,
    }
