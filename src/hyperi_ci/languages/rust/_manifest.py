# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/_manifest.py
# Purpose:   Facts about the root Cargo.toml
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Read the root Cargo.toml for the facts the Rust stages act on."""

import tomllib
from pathlib import Path
from typing import Any


def _root_manifest(project_dir: Path | None) -> dict[str, Any] | None:
    """Return the parsed root Cargo.toml, or None when missing or unparseable."""
    manifest = (project_dir or Path.cwd()) / "Cargo.toml"
    try:
        return tomllib.loads(manifest.read_text(encoding="utf-8", errors="replace"))
    except (OSError, tomllib.TOMLDecodeError):
        return None


def split_feature_sets(features: str) -> list[str]:
    """Split pipe-separated feature sets, each run on its own.

    cargo's ``--features`` is additive, so mutually exclusive sets such as
    jemalloc and mimalloc need separate invocations.
    """
    if features in ("all", "default"):
        return [features]
    return [f.strip() for f in features.split("|") if f.strip()]


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def is_root_package_workspace(project_dir: Path | None = None) -> bool:
    """Return True when a bare cargo command covers only the root package.

    That is a root manifest with both ``[package]`` and ``[workspace]`` and no
    ``default-members``, which is the repo's own choice and must not be
    overridden. A virtual workspace, or a missing or unparseable manifest,
    answers False.

    Args:
        project_dir: Directory holding the root Cargo.toml; the cwd if None.

    Returns:
        Whether cargo needs ``--workspace`` to reach every member.

    """
    data = _root_manifest(project_dir)
    if data is None:
        return False
    workspace = data.get("workspace")
    return (
        isinstance(data.get("package"), dict)
        and isinstance(workspace, dict)
        and "default-members" not in workspace
    )


def feature_resolver(project_dir: Path | None = None) -> int | None:
    """Return the cargo feature resolver version the root Cargo.toml selects.

    A ``resolver`` key under ``[workspace]`` or ``[package]`` wins. Without
    one, a root package takes it from its edition (2021 selects 2, 2024 and
    later 3, anything older 1), and a virtual workspace gets 1, as cargo does.

    Args:
        project_dir: Directory holding the root Cargo.toml; the cwd if None.

    Returns:
        The resolver version, or None when the manifest is missing or
        unparseable, or declares a resolver that is not a number.

    """
    data = _root_manifest(project_dir)
    if data is None:
        return None
    workspace = _table(data, "workspace")
    package = _table(data, "package")
    declared = workspace.get("resolver", package.get("resolver"))
    if declared is not None:
        try:
            return int(str(declared))
        except ValueError:
            return None
    if not package:
        return 1 if workspace else None
    edition = package.get("edition", "2015")
    if isinstance(edition, dict) and edition.get("workspace") is True:
        edition = _table(workspace, "package").get("edition", "2015")
    try:
        year = int(str(edition))
    except ValueError:
        return None
    if year >= 2024:
        return 3
    return 2 if year >= 2021 else 1
