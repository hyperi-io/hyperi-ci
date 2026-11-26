# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/release.py
# Purpose:   Rust release handler (crates.io)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Rust release handler -- checks and packages a crate, then publishes it.

``prepare`` runs everything that executes the crate's own code
(cargo-semver-checks, ``cargo package``) before any tag is cut. ``run``
uploads, and classifies the crate itself even when prepared, so a forged
``prepared.json`` cannot put a binary application on crates.io.

cargo cannot publish a ``.crate`` it did not pack, so the upload repackages
with ``--no-verify``, which runs no build script. Upload cargo calls run from
an empty directory with ``--manifest-path``, because ``.cargo/config.toml``
and ``rust-toolchain.toml`` in the working directory can name a program to run.

A workspace with more than one crate for crates.io is packed and published
with ``--workspace``, which cargo runs in dependency order.
"""

import json
import os
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hyperi_ci import release_prepare
from hyperi_ci.common import (
    URL_ERRORS,
    error,
    group,
    info,
    resolve_release_version,
    run_cmd,
    success,
    url_read,
    warn,
)
from hyperi_ci.config import CIConfig
from hyperi_ci.languages._build_common import run_destinations
from hyperi_ci.languages.rust import bindings, semver_checks
from hyperi_ci.languages.rust.build import binary_targets, stamp_manifest

CRATE_FACT = "crate"


class CargoMetadataError(Exception):
    """``cargo metadata`` failed, so whether there is a crate is unknown."""


def _sync_cargo_toml_version(version: str) -> bool:
    """Stamp the committed Cargo.toml to the release version before publish.

    Uses the build's `stamp_manifest` so the two stamps cannot drift.
    Returns False only if there is no Cargo.toml.
    """
    if not Path("Cargo.toml").exists():
        error("Cargo.toml not found")
        return False
    stamp_manifest(version, Path.cwd())
    return True


def _cargo_away(root: Path, args: list[str], **kwargs: Any) -> Any:
    """Run ``cargo <args> --manifest-path <root>/Cargo.toml`` from an empty directory."""
    with tempfile.TemporaryDirectory(prefix="hyperi-ci-cargo-") as away:
        return run_cmd(
            ["cargo", *args, "--manifest-path", str(root / "Cargo.toml")],
            cwd=away,
            check=False,
            **kwargs,
        )


def _metadata(root: Path) -> dict[str, Any]:
    """Return ``cargo metadata --no-deps`` for the workspace at ``root``.

    Raises:
        CargoMetadataError: cargo could not answer.

    """
    result = _cargo_away(
        root, ["metadata", "--no-deps", "--format-version=1"], capture=True
    )
    if result.returncode != 0:
        raise CargoMetadataError((result.stderr or "").strip()[-500:])
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise CargoMetadataError(f"unreadable cargo metadata: {exc}") from exc


def _binaries(root: Path) -> list[str]:
    """Return the crate's binary targets, failing rather than guessing.

    Raises:
        CargoMetadataError: cargo could not answer, so a library crate cannot
            be told from a binary application.

    """
    return binary_targets(_metadata(root))


def crates_io_packages(meta: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the workspace packages crates.io takes: no ``publish`` key, or one naming it."""
    return [
        package
        for package in meta.get("packages", [])
        if package.get("publish") is None or "crates-io" in package["publish"]
    ]


def _is_multi_crate(root: Path) -> bool:
    """Say whether the workspace publishes more than one crate to crates.io.

    Raises:
        CargoMetadataError: See :func:`_metadata`.

    """
    return len(crates_io_packages(_metadata(root))) > 1


def _on_crates_io(name: str, version: str) -> bool:
    """Say whether crates.io already has ``name`` at ``version``.

    An unreachable API answers False, so cargo makes the call and its
    "already exists" is caught as on a single crate.
    """
    request = urllib.request.Request(
        f"https://crates.io/api/v1/crates/{name}/{version}",
        headers={"User-Agent": "hyperi-ci (https://github.com/hyperi-io/hyperi-ci)"},
    )
    try:
        url_read(request, timeout=20)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            warn(f"  crates.io lookup of {name} {version} failed: HTTP {exc.code}")
        return False
    except URL_ERRORS as exc:
        warn(f"  crates.io lookup of {name} {version} failed: {exc}")
        return False
    return True


def workspace_publish_args(
    packages: list[dict[str, Any]], published: Callable[[str, str], bool]
) -> list[str] | None:
    """Return the ``cargo publish`` args for a multi-crate workspace.

    cargo publishes the members in dependency order. A member already on
    crates.io at its version is excluded, so a re-run after a partial publish
    finishes the rest.

    Args:
        packages: From :func:`crates_io_packages`.
        published: Answers whether crates.io has a name at a version.

    Returns:
        The args, or None when every member is already published.

    """
    done = [p["name"] for p in packages if published(p["name"], p["version"])]
    if len(done) == len(packages):
        return None
    args = ["publish", "--workspace", "--allow-dirty", "--no-verify"]
    for name in done:
        info(f"  {name} is already on crates.io at this version (skipping)")
        args += ["--exclude", name]
    return args


def _publishes_crate(config: CIConfig, root: Path) -> bool:
    """Say whether this project ships a crate to a registry at all.

    Raises:
        CargoMetadataError: See :func:`_binaries`.

    """
    if _binaries(root):
        info("Binary application -- skipping crate registry publish")
        info("Binary artifacts will be uploaded by the generic binary publisher")
        return False
    if not config.destination_for("cargo"):
        info("No Rust publish destinations configured")
        return False
    return True


def _stamp_and_check(config: CIConfig) -> int:
    """Stamp Cargo.toml, then compare the public API with the last release.

    The order is required: before the stamp, semver-checks compares the last
    release against itself and passes vacuously (issue #186).
    """
    version = resolve_release_version()
    if version:
        info(f"Publishing version {version}")
        if not _sync_cargo_toml_version(version):
            return 1
    else:
        warn(
            "No release version resolved -- publishing with existing Cargo.toml version"
        )
    with group("Public API compatibility"):
        return semver_checks.run(config)


def prepare(config: CIConfig, out_dir: Path) -> tuple[int, dict[str, Any]]:
    """Run the crate checks that execute repo code, in the job with no secrets.

    Args:
        config: Merged CI configuration.
        out_dir: Prepared directory (unused: cargo cannot publish a prepared
            .crate, so nothing is carried but the decision).

    Returns:
        Exit code, and whether the upload has a crate to publish.

    """
    del out_dir
    try:
        publishes = _publishes_crate(config, Path.cwd())
    except CargoMetadataError as exc:
        error(f"cargo metadata failed, so the crate cannot be classified: {exc}")
        return 1, {}
    if not publishes:
        return 0, {CRATE_FACT: False}
    if _stamp_and_check(config) != 0:
        return 1, {}
    try:
        workspace = ["--workspace"] if _is_multi_crate(Path.cwd()) else []
    except CargoMetadataError as exc:
        error(f"cargo metadata failed, so the crates cannot be listed: {exc}")
        return 1, {}
    with group("Package crate"):
        result = run_cmd(
            # --no-verify matches the upload: the Build job compiled it already,
            # and this runner lacks the native build tools a build script needs.
            ["cargo", "package", "--allow-dirty", "--no-verify", *workspace],
            check=False,
        )
        if result.returncode != 0:
            error("cargo package failed -- nothing is tagged or published")
            return result.returncode, {}
    return 0, {CRATE_FACT: True}


def _publish_crates_io(root: Path) -> int:
    """Publish the crate at ``root`` to crates.io without running its code.

    Requires CARGO_REGISTRY_TOKEN.

    Returns:
        Exit code (0 = success).

    """
    if not os.environ.get("CARGO_REGISTRY_TOKEN"):
        error("CARGO_REGISTRY_TOKEN not set -- cannot publish to crates.io")
        return 1

    try:
        packages = crates_io_packages(_metadata(root))
    except CargoMetadataError as exc:
        error(f"cargo metadata failed, so the crates cannot be listed: {exc}")
        return 1
    args: list[str] | None = ["publish", "--allow-dirty", "--no-verify"]
    if len(packages) > 1:
        args = workspace_publish_args(packages, _on_crates_io)
        if args is None:
            warn("  Every crate is already on crates.io at this version (skipping)")
            return 0
    result = _cargo_away(root, args, capture=True)
    if result.returncode != 0:
        if "already exists" in result.stderr:
            warn("  Crate version already exists on crates.io (skipping)")
            return 0
        error("crates.io publish failed")
        if result.stderr:
            error(result.stderr)
        return result.returncode

    success("Published to crates.io")
    return 0


def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Upload a library crate to its registries, then any opted-in Python wheels.

    With ``HYPERCI_RELEASE_PREPARED`` set this only classifies, stamps and
    publishes. Without it the checks run here first, beside the token.

    Args:
        config: Merged CI configuration.
        extra_env: Additional environment variables.

    Returns:
        Exit code (0 = success).

    """
    ok, prepared = release_prepare.load_or_report("run release")
    if not ok:
        return 1

    root = Path.cwd()
    rc = _publish_crate(config, root, prepared)
    if rc != 0:
        return rc
    return bindings.publish(config, root, resolve_release_version() or "")


def _publish_crate(
    config: CIConfig, root: Path, prepared: release_prepare.Prepared | None
) -> int:
    """Upload the crate when it is a library with a registry destination."""
    try:
        publishes = _publishes_crate(config, root)
    except CargoMetadataError as exc:
        error(f"cargo metadata failed, so the crate cannot be classified: {exc}")
        return 1
    if not publishes:
        return 0

    if prepared is not None:
        if prepared.facts.get(CRATE_FACT) is not True:
            error(
                "This is a library crate with a registry destination, but the "
                "prepare job did not check and package it -- refusing to publish"
            )
            return 1
        version = resolve_release_version()
        if version and not _sync_cargo_toml_version(version):
            return 1
    elif _stamp_and_check(config) != 0:
        return 1

    destinations = config.destination_for("cargo")
    info(f"Publishing Rust crate to: {', '.join(destinations)}")
    return run_destinations(
        destinations,
        {"crates-io": ("Publish: crates.io", lambda: _publish_crates_io(root))},
        "Rust",
    )
