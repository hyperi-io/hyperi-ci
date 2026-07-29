# Project:   HyperI CI
# File:      src/hyperi_ci/deps/ecosystems.py
# Purpose:   Declared floor vs locked version, per dependency group
# Origin:    Derek's deps automation scripts, merged into hyperi-ci now they are
#            mature enough for people (and hyperi-ai's /deps) to use directly
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Floor-vs-lock drift, per ecosystem, per dependency group.

Renovate has no equivalent. Its ``rangeStrategy: bump`` only rewrites a floor
when a NEW upstream release triggers a PR, so a repo whose declared floor is
already years behind its own lock is never told. This is that standing audit.

MULTI-LANGUAGE BY CONSTRUCTION. ``detect_language()`` returns ONE primary
language, which is the assumption we cannot make -- ours are Python and Rust
and TypeScript and OpenTofu at once. Every manifest in the tree is parsed in
the same pass, and each ecosystem is reported separately, so a stale Rust dev
dependency cannot hide behind a current Python runtime pin.

Two layers, and only the first is load-bearing:

1. CORE -- pure Python parsing (tomllib, json). Zero external tools, always
   works, sufficient on its own.
2. ENRICHMENT -- if a language toolchain happens to be installed, shell out to
   it for what hand-parsing cannot get: a workspace member whose lock lives
   above the scan root, a crate rename, a vendored graph. Probed with
   ``shutil.which`` every time (the house idiom, same as
   ``languages/rust/quality.py``), run offline, and it may only ADD rows the
   parse missed -- never replace one, never change an exit code, never warn
   when absent. A box without cargo is normal, not a finding. Every locked
   version carries the ``source`` it came from (``parse``, or the tool's name).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from hyperi_ci.deps import versions as ver
from hyperi_ci.deps.surfaces import Surface, load, repo_files

# Manifest filename -> ecosystem name. A repo may carry every one of these at
# once; this is a lookup, never a first-match-wins detection.
MANIFESTS: dict[str, str] = {
    "pyproject.toml": "python",
    "Cargo.toml": "rust",
    "package.json": "node",
}


@dataclass
class Ecosystem:
    """One manifest and the lock that resolves it."""

    name: str
    manifest: str
    lock: str
    groups: list[dict] = field(default_factory=list)
    declared: int = 0
    compared: int = 0


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
    """Resolve a dotted group path, ``*`` wildcarding every key at that level.

    ``project.optional-dependencies.*`` over a pyproject yields one
    ``(concrete_path, value)`` per extra, so the report keeps dev separate from
    runtime instead of merging them into one number.
    """
    parts = path.split(".")

    def walk(node: object, index: int, trail: list[str]) -> Iterator[tuple[str, object]]:
        if index == len(parts):
            yield ".".join(trail), node
            return
        if not isinstance(node, dict):
            return
        part = parts[index]
        if part == "*":
            for key, child in node.items():
                yield from walk(child, index + 1, [*trail, str(key)])
        elif part in node:
            yield from walk(node[part], index + 1, [*trail, part])

    yield from walk(data, 0, [])


def group_entries(value: object) -> list[tuple[str, str]]:
    """A dependency group -> ``[(name, constraint)]``, whatever shape it is in.

    Covers the three that occur: a list of PEP 508 strings (PEP 621/735), a map
    of name to constraint string (npm, poetry, simple cargo), and a map of name
    to table (cargo with features, a cargo rename, a poetry table).
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
                    # `package = "..."` is cargo's rename: the crate actually
                    # locked, not the name it is imported under.
                    real = raw.get("package")
                    out.append(
                        (str(real) if isinstance(real, str) else str(key), version)
                    )
    return out


def packages_from_toml_lock(path: Path) -> dict[str, str]:
    """``[[package]]`` name/version pairs -- uv.lock, poetry.lock, Cargo.lock.

    Where one name resolves several times (different markers, or two majors of
    a transitive crate), keep the HIGHEST: that is the one a floor has to
    cover, so it is the one worth warning about.
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
    """package-lock.json v2/v3 ``packages`` map -> name -> version.

    Keys are ``node_modules/<name>``, nested when hoisting could not collapse
    them. The shallowest key wins, since that is the copy the manifest's own
    constraint is about. A v1 lockfile (a flat ``dependencies`` map, no
    ``packages``) yields nothing here -- npm 7 shipped v2 in 2020, and the
    enrichment path covers it when npm is installed.
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


def find_lock(start: Path, root: Path, names: tuple[str, ...]) -> Path | None:
    """First lockfile found walking from a manifest's directory up to ``root``.

    Workspace layouts put the lock at the workspace root, not beside the member
    manifest, so a beside-only lookup reports every member as unlocked.
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
# Enrichment -- optional, additive, never load-bearing
# ---------------------------------------------------------------------------


def _run_tool(cmd: list[str], cwd: Path) -> str | None:
    """Run an optional toolchain command. None when absent or unhappy.

    Probed with ``shutil.which`` on every call: a box without cargo is normal,
    not a finding, so a miss is silent -- no warn, no non-zero exit. Nothing
    here is required for a surface to be reported; the parsing path already
    produced that.
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
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def enrich_cargo(cwd: Path) -> dict[str, str]:
    """Resolved crate versions from ``cargo metadata``, offline.

    Buys the workspace case: a member whose Cargo.lock sits above the scan
    root, and renames the manifest declares but the lock records differently.
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
    """Resolved distributions from ``uv export --frozen``, offline.

    Buys the uv-workspace case, where the lock belongs to a parent member.
    ``--frozen`` reads the existing lock and never resolves, so this never
    reaches the network and never rewrites the lock.
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
        for name, meta in (node.get("dependencies") or {}).items():
            if isinstance(meta, dict) and isinstance(meta.get("version"), str):
                resolved.setdefault(str(name), meta["version"])
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
    """Record one group: every declared entry, with the lock beside it.

    Entries with no resolvable floor (``*``, a git or path dependency) or no
    lock hit are still reported, with empty columns -- "declared but never
    locked" is itself worth seeing. Only entries with both count as compared.
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
    # pep621 AND poetry: one file belongs to both surfaces, so both group sets
    # are walked rather than guessing which build backend won.
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
    """package.json against package-lock.json (plus npm ls, when installed)."""
    manifest = root / rel
    data = _load_json(manifest)
    lock = find_lock(manifest.parent, root, ("package-lock.json",))
    parsed = packages_from_npm_lock(lock) if lock is not None else {}
    locked = _locked_map(parsed, enrich_npm(manifest.parent), "npm", ver.norm_npm)

    eco = Ecosystem(
        name="node",
        manifest=rel,
        lock=lock.relative_to(root).as_posix() if lock is not None else "",
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

    The check that started the whole thing: no update bot raises this, because
    an open ``>=`` range is already satisfied by every future release, so there
    is never a manifest edit to propose. Grown out of Derek's deps automation
    scripts and generalised here from one repo to any.

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

    notes: list[str] = []
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
