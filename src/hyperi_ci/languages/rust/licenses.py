# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/licenses.py
# Purpose:   Rust dependency-licence gate (cargo metadata -> licence policy)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Rust dependency-licence gate.

``cargo metadata`` carries each crate's exact SPDX ``license`` expression and
the dependency graph. A crate is shipped when a workspace member reaches it
through normal dependencies, so dev and build dependencies are not judged, and
neither is a proc-macro or anything only a proc-macro needs. Every shipped
crate is linked into the binary, so it is judged as ``static``.
"""

import json
from collections import deque
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from hyperi_ci.common import error, run_cmd
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import emit_tool_output, resolve_tool_mode
from hyperi_ci.quality.dep_licenses import (
    Finding,
    Mode,
    NoticeEntry,
    Override,
    Policy,
    PolicyError,
    fail,
    find_override,
    finish,
    judge,
    load_settings,
    project_path,
    quote,
)

# Every feature, so a crate only a release build turns on is judged too.
_METADATA = [
    "cargo",
    "metadata",
    "--format-version",
    "1",
    "--locked",
    "--all-features",
]

# Files a crate ships its licence or notice in, matched on the name's start.
_LICENCE_PREFIXES = ("LICENSE", "LICENCE", "COPYING", "UNLICENSE")
_NOTICE_PREFIXES = ("NOTICE",)


def shipped_packages(metadata: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return the packages a workspace member reaches through normal dependencies.

    Args:
        metadata: ``cargo metadata --format-version 1`` output.

    Returns:
        The shipped third-party packages, workspace members excluded.

    """
    packages = {p["id"]: p for p in metadata.get("packages", [])}
    nodes = {n["id"]: n for n in (metadata.get("resolve") or {}).get("nodes", [])}
    members = set(metadata.get("workspace_members", []))
    seen: set[str] = set()
    queue = deque(members)
    while queue:
        node = nodes.get(queue.popleft())
        for dep in (node or {}).get("deps", []):
            target = dep.get("pkg")
            normal = any(k.get("kind") is None for k in dep.get("dep_kinds", []))
            if not normal or target in seen or _is_proc_macro(packages.get(target)):
                continue
            seen.add(target)
            queue.append(target)
    return [packages[pid] for pid in sorted(seen - members) if pid in packages]


def _is_proc_macro(package: Mapping[str, Any] | None) -> bool:
    targets = (package or {}).get("targets", [])
    return any("proc-macro" in t.get("kind", []) for t in targets)


def _crate_files(package: Mapping[str, Any], prefixes: Sequence[str]) -> list[Path]:
    crate_dir = Path(package["manifest_path"]).parent
    if not crate_dir.is_dir():
        return []
    return sorted(
        path
        for path in crate_dir.iterdir()
        if path.is_file() and path.name.upper().startswith(prefixes)
    )


def _read(paths: Sequence[Path]) -> str | None:
    texts = [p.read_text(encoding="utf-8", errors="replace").strip() for p in paths]
    joined = "\n\n".join(text for text in texts if text)
    return joined or None


def licence_files(package: Mapping[str, Any]) -> list[Path]:
    """Return the crate's licence files: its ``license-file``, else LICENSE* and the like."""
    declared = package.get("license_file")
    if declared:
        path = Path(package["manifest_path"]).parent / declared
        return [path] if path.is_file() else []
    return _crate_files(package, _LICENCE_PREFIXES)


def findings_for(
    packages: Sequence[Mapping[str, Any]],
    policy: Policy,
    overrides: Sequence[Override],
) -> list[Finding]:
    """Judge each shipped crate as a static link.

    Args:
        packages: The shipped packages from ``cargo metadata``.
        policy: The effective policy.
        overrides: The repo's per-dependency answers.

    Returns:
        One finding per crate.

    """
    findings = []
    for package in packages:
        name, version = str(package["name"]), str(package["version"])
        override = find_override(overrides, "crates", name, version)
        if override is not None:
            raw, source, evidence = override.licence, "override", override.reason
        else:
            raw, source, evidence = package.get("license"), "metadata", "license"
        spdx, verdict, obligations, rule = judge(raw, Mode.STATIC, policy)
        if rule == "undeclared":
            files = licence_files(package)
            if files:
                source = "license-file"
                evidence = f"{project_path(files[0])}: {quote(_read(files))}"
        findings.append(
            Finding(
                "crates",
                name,
                version,
                spdx,
                source,
                evidence,
                Mode.STATIC,
                verdict,
                obligations,
                rule,
            )
        )
    return findings


def notice_entries(
    packages: Sequence[Mapping[str, Any]], findings: Sequence[Finding]
) -> list[NoticeEntry]:
    """Pair each finding with the licence and NOTICE text in its crate."""
    by_key = {(str(p["name"]), str(p["version"])): p for p in packages}
    entries = []
    for finding in findings:
        package = by_key.get((finding.name, finding.version))
        if package is None:
            entries.append(NoticeEntry(finding, None, None))
            continue
        notice = _read(_crate_files(package, _NOTICE_PREFIXES))
        entries.append(NoticeEntry(finding, _read(licence_files(package)), notice))
    return entries


def run_gate(config: CIConfig) -> bool:
    """Run the dependency-licence gate for a Rust project.

    Args:
        config: Merged CI configuration.

    Returns:
        True if the pipeline should continue, False on a blocking failure.

    """
    mode = resolve_tool_mode("dep_licenses", config, default="warn")
    if mode == "disabled":
        return True
    if not Path("Cargo.lock").is_file():
        return fail("  dep licences: no Cargo.lock, nothing checked", mode)
    try:
        policy, overrides = load_settings(config)
    except PolicyError as exc:
        error(f"  dep licences: {exc}")
        return False

    # The no-deps metadata in targets.py has no dependency graph.
    try:
        result = run_cmd(_METADATA, check=False, capture=True)
    except OSError:
        return fail("  dep licences: cargo is not installed, nothing checked", mode)
    if result.returncode != 0:
        emit_tool_output("cargo metadata", result.stderr)
        return fail("  dep licences: cargo metadata failed, nothing checked", mode)
    packages = shipped_packages(json.loads(result.stdout))
    findings = findings_for(packages, policy, overrides)
    tools = {"cargo metadata": "format-version 1"}
    return finish(findings, notice_entries(packages, findings), policy, tools, mode)
