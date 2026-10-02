# Project:   HyperI CI
# File:      src/hyperi_ci/languages/typescript/release.py
# Purpose:   TypeScript/Node release handler (npm + GitHub Packages)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""TypeScript/Node release handler -- packs an npm package, then publishes it.

``prepare`` packs the tarball, which is where the package's own scripts run:
``prepublishOnly``, then ``npm pack``'s ``prepack``, ``prepare`` and
``postpack``. ``run`` publishes that tarball with ``--ignore-scripts`` from an
empty directory, so neither the package's scripts nor the repo's ``.npmrc``
reach the token (issue #409). ``publish`` and ``postpublish`` scripts no longer
run: they fire after the upload, and the upload holds the token.

The token goes in a throwaway user config scoped to the registry host, never
the global ``~/.npmrc`` (see docs/lessons.md, "npm Config Pollution") and never
the project's.
"""

import json
import os
import tarfile
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from hyperi_ci import release_prepare
from hyperi_ci.common import (
    error,
    group,
    info,
    resolve_release_version,
    run_cmd,
    success,
    warn,
)
from hyperi_ci.config import CIConfig, load_org_config
from hyperi_ci.languages.typescript._common import package_script_env

TARBALL_FACT = "tarball"
NPMJS_REGISTRY = "https://registry.npmjs.org/"
GITHUB_NPM_REGISTRY = "https://npm.pkg.github.com/"
_REGISTRIES = {"npmjs": NPMJS_REGISTRY, "ghcr-npm": GITHUB_NPM_REGISTRY}


def prepare(config: CIConfig, out_dir: Path) -> tuple[int, dict[str, Any]]:
    """Pack the package, running its pre-publish scripts, in the job with no secrets.

    Args:
        config: Merged CI configuration.
        out_dir: Prepared directory; the tarball goes under ``npm/``.

    Returns:
        Exit code, and the tarball's path relative to ``out_dir``.

    """
    if not config.destination_for("npm"):
        info("No npm publish destinations configured")
        return 0, {}

    env = package_script_env()
    with group("Pack npm package"):
        result = run_cmd(
            ["npm", "run", "prepublishOnly", "--if-present"], check=False, env=env
        )
        if result.returncode != 0:
            error("prepublishOnly failed -- nothing is tagged or published")
            return result.returncode, {}

        pack_dir = out_dir / "npm"
        pack_dir.mkdir(parents=True, exist_ok=True)
        result = run_cmd(
            ["npm", "pack", "--pack-destination", str(pack_dir)],
            check=False,
            env=env,
        )
        if result.returncode != 0:
            error("npm pack failed -- nothing is tagged or published")
            return result.returncode, {}

    tarballs = sorted(pack_dir.glob("*.tgz"))
    if len(tarballs) != 1:
        error(f"npm pack left {len(tarballs)} tarballs in {pack_dir}, expected 1")
        return 1, {}
    success(f"Packed {tarballs[0].name}")
    return 0, {TARBALL_FACT: f"npm/{tarballs[0].name}"}


@contextmanager
def _isolated_npm(lines: list[str]) -> Iterator[tuple[Path, Path]]:
    """Yield an empty working directory and a user config holding ``lines``.

    The config file is created 0600 and removed with the directory.
    """
    with tempfile.TemporaryDirectory(prefix="hyperi-ci-npm-") as away:
        userconfig = Path(away) / "publish.npmrc"
        fd = os.open(userconfig, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")
        yield Path(away), userconfig


def _npm_publish(tarball: Path, lines: list[str], extra: list[str]) -> int:
    """Publish ``tarball`` with no scripts, no project config, and ``lines`` as config."""
    with _isolated_npm(lines) as (away, userconfig):
        result = run_cmd(
            [
                "npm",
                "publish",
                str(tarball),
                "--ignore-scripts",
                "--userconfig",
                str(userconfig),
                *extra,
            ],
            cwd=away,
            capture=True,
            check=False,
        )
    if result.returncode != 0 and "already exists" in (result.stderr + result.stdout):
        warn("  Package version already exists (skipping)")
        return 0
    if result.returncode != 0 and result.stderr:
        error(result.stderr)
    return result.returncode


def _publish_npm(tarball: Path) -> int:
    """Publish to npmjs.com. Requires NPM_TOKEN.

    Returns:
        Exit code (0 = success).

    """
    token = os.environ.get("NPM_TOKEN")
    if not token:
        error("NPM_TOKEN not set -- cannot publish to npm")
        return 1

    rc = _npm_publish(
        tarball,
        [f"//registry.npmjs.org/:_authToken={token}"],
        ["--access", "public", "--registry", NPMJS_REGISTRY],
    )
    if rc != 0:
        error("npm publish failed")
        return rc
    success("Published to npm")
    return 0


def _publish_ghcr_npm(tarball: Path) -> int:
    """Publish to the GitHub Packages npm registry, as the org scope.

    Uses GITHUB_TOKEN (via GH_TOKEN) for auth. Packages are private by default
    and visible only to org members.

    Returns:
        Exit code (0 = success).

    """
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        error("GH_TOKEN/GITHUB_TOKEN not set -- cannot publish to GitHub Packages")
        return 1

    org = load_org_config()
    rc = _npm_publish(
        tarball,
        [
            f"@{org.github_org}:registry=https://npm.pkg.github.com/{org.github_org}",
            f"//npm.pkg.github.com/:_authToken={token}",
        ],
        ["--registry", GITHUB_NPM_REGISTRY],
    )
    if rc != 0:
        error("GitHub Packages npm publish failed")
        return rc
    success("Published to GitHub Packages npm")
    return 0


def _tarball_manifest(tarball: Path) -> dict[str, Any]:
    """Read ``package/package.json`` out of an npm tarball.

    Raises:
        ValueError: The tarball has no readable package.json.

    """
    try:
        with tarfile.open(tarball, "r:gz") as archive:
            member = archive.extractfile("package/package.json")
            if member is None:
                raise ValueError("package/package.json is not a file")
            data = json.loads(member.read().decode("utf-8", errors="replace"))
    except (tarfile.TarError, KeyError, OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read package/package.json: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("package/package.json is not an object")
    return data


def _tarball_problem(tarball: Path, registry: str) -> str | None:
    """Say why ``tarball`` must not be published to ``registry``, or None.

    The tarball was packed by a job that ran repo code, so it must be this
    checkout's package at this run's version, and must not send itself to a
    registry host other than the one this upload names.
    """
    try:
        manifest = _tarball_manifest(tarball)
    except ValueError as exc:
        return str(exc)
    checkout = Path("package.json")
    try:
        expected = json.loads(checkout.read_text(encoding="utf-8")).get("name")
    except (OSError, ValueError, AttributeError):
        return "cannot read the checkout's package.json name"
    if manifest.get("name") != expected:
        return f"the tarball is {manifest.get('name')!r}, the checkout is {expected!r}"
    version = resolve_release_version()
    if manifest.get("version") != version:
        return f"the tarball is version {manifest.get('version')!r}, this run is {version!r}"
    publish_config = manifest.get("publishConfig") or {}
    target = (
        publish_config.get("registry") if isinstance(publish_config, dict) else None
    )
    if target and urlparse(str(target)).netloc != urlparse(registry).netloc:
        return f"its publishConfig.registry {target} is not {registry}"
    return None


def _publish(destinations: list[str], tarball: Path) -> int:
    info(f"Publishing npm package to: {', '.join(destinations)}")
    for dest in destinations:
        if dest not in _REGISTRIES:
            error(f"Unknown npm publish destination: {dest}")
            return 1
        problem = _tarball_problem(tarball, _REGISTRIES[dest])
        if problem:
            error(f"Refusing to publish {tarball.name}: {problem}")
            return 1
        if dest == "npmjs":
            with group("Publish: npm"):
                rc = _publish_npm(tarball)
        else:
            with group("Publish: GitHub Packages npm"):
                rc = _publish_ghcr_npm(tarball)
        if rc != 0:
            return rc
    return 0


def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Publish the npm package to its registries.

    With ``HYPERCI_RELEASE_PREPARED`` set the tarball comes from the prepare
    job. Without it -- a hand-rolled workflow calling ``run release`` -- it is
    packed here first, in the same process as the token.

    Args:
        config: Merged CI configuration.
        extra_env: Additional environment variables.

    Returns:
        Exit code (0 = success).

    """
    destinations = config.destination_for("npm")
    if not destinations:
        info("No npm publish destinations configured")
        return 0

    ok, prepared = release_prepare.load_or_report("run release")
    if not ok:
        return 1

    if prepared is not None:
        relative = prepared.facts.get(TARBALL_FACT)
        if not isinstance(relative, str):
            error("The prepare job packed no npm tarball -- nothing to publish")
            return 1
        try:
            tarball = prepared.file(relative)
        except release_prepare.PreparedError as exc:
            error(str(exc))
            return 1
        return _publish(destinations, tarball)

    with tempfile.TemporaryDirectory(prefix="hyperi-ci-pack-") as scratch:
        rc, facts = prepare(config, Path(scratch))
        if rc != 0:
            return rc
        return _publish(destinations, Path(scratch) / facts[TARBALL_FACT])
