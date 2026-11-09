# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/licenses.py
# Purpose:   Rust dependency-licence gate (cargo metadata -> licence policy)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Rust dependency-licence gate.

A crate is shipped when ``cargo tree`` lists it through normal, non-proc-macro
edges for one of the build's targets, so dev and build dependencies, proc-macros
and crates for other platforms are not judged. ``cargo metadata`` supplies each
crate's exact SPDX ``license`` expression and its files. Every shipped crate is
linked into the binary, so it is judged as ``static``.
"""

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from hyperi_ci.build_targets import ci_targets
from hyperi_ci.common import error, info, run_cmd
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
    judge,
    load_settings,
    project_path,
    quote,
)
from hyperi_ci.quality.vendored_licenses import finish_with_copied_in

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
_SLASH = re.compile(r"\s*/\s*")

_LICENCE_PREFIXES = ("LICENSE", "LICENCE", "COPYING", "UNLICENSE")
_NOTICE_PREFIXES = ("NOTICE",)


def tree_command(target: str) -> list[str]:
    """Return the cargo tree call listing what a build for ``target`` links."""
    return [
        "cargo",
        "tree",
        "--locked",
        "--workspace",
        "--all-features",
        "--edges",
        "normal,no-proc-macro",
        "--target",
        target,
        "--prefix",
        "none",
        "--format",
        "{p}",
        "--color",
        "never",
    ]


def tree_packages(text: str) -> set[tuple[str, str]]:
    """Return the ``(name, version)`` of every package a cargo tree listing names.

    Args:
        text: ``cargo tree --prefix none --format {p}`` output, where each line
            starts ``name vX.Y.Z``.

    Returns:
        The packages, version without its ``v``.

    """
    found: set[tuple[str, str]] = set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1].startswith("v"):
            found.add((fields[0], fields[1][1:]))
    return found


def shipped_packages(
    metadata: Mapping[str, Any], linked: set[tuple[str, str]]
) -> list[Mapping[str, Any]]:
    """Return the third-party packages the build links.

    cargo tree resolves features and platforms the way a build does, which
    cargo metadata's graph does not: it merges dev-dependency features, keeps
    optional crates only a weak ``dep?/feature`` names, and covers every
    platform.

    Args:
        metadata: ``cargo metadata --format-version 1`` output, for each
            package's licence fields and files.
        linked: :func:`tree_packages` over every build target.

    Returns:
        The linked packages, workspace members excluded, in id order.

    """
    members = set(metadata.get("workspace_members", []))
    return sorted(
        (
            p
            for p in metadata.get("packages", [])
            if (p["name"], p["version"]) in linked and p["id"] not in members
        ),
        key=lambda p: p["id"],
    )


def _crate_files(package: Mapping[str, Any], prefixes: tuple[str, ...]) -> list[Path]:
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
            if raw:
                # crates.io reads the legacy `MIT/Apache-2.0` form as an OR.
                raw = _SLASH.sub(" OR ", str(raw))
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
    targets = ci_targets(config._raw)
    linked: set[tuple[str, str]] = set()
    for target in targets:
        tree = run_cmd(tree_command(target), check=False, capture=True)
        if tree.returncode != 0:
            emit_tool_output("cargo tree", tree.stderr)
            return fail(f"  dep licences: cargo tree for {target} failed", mode)
        linked |= tree_packages(tree.stdout)
    info(f"  dep licences: crates linked for {', '.join(targets)}")
    packages = shipped_packages(json.loads(result.stdout), linked)
    findings = findings_for(packages, policy, overrides)
    tools = {"cargo metadata": "format-version 1", "cargo tree": ",".join(targets)}
    entries = notice_entries(packages, findings)
    return finish_with_copied_in(
        config, findings, entries, policy, tools, mode, overrides
    )
