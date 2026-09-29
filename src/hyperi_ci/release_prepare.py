# Project:   HyperI CI
# File:      src/hyperi_ci/release_prepare.py
# Purpose:   Run a release's repo code apart from the credentials that upload it
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Split a release into a prepare half and an upload half.

The upload holds every publish credential, so nothing the repo controls may run
beside it: a crate's build script runs under ``cargo semver-checks``, an npm
package's lifecycle scripts run under ``npm pack``, and ``release.stamp_cmd``
is the repo's own command (issue #409). ``release-prepare`` runs all of them
in a job that holds no secrets, and writes what the upload needs into a
directory the workflow carries to the publish job:

* ``prepared.json`` -- the version, the language, and the decisions that took
  repo code to reach, such as whether there is a crate to publish.
* ``stamped/`` -- ``VERSION`` and the ``release.stamp_paths`` files as the stamp
  rendered them, for ``release-commit``.
* whatever the language packs, such as the npm tarball.

``run release`` with ``HYPERCI_RELEASE_PREPARED`` naming that directory only
uploads. The directory comes from a job that ran repo code, so everything in it
is read as data and checked: the version must match the run's, and every path
must stay inside the directory.
"""

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hyperi_ci.common import error, group, info, run_cmd, success, warn
from hyperi_ci.config import CIConfig, load_config
from hyperi_ci.detect import detect_language
from hyperi_ci.stamp import stamp_paths, stamp_version

PREPARED_ENV = "HYPERCI_RELEASE_PREPARED"
MANIFEST_NAME = "prepared.json"
STAMPED_DIR = "stamped"
VERSION_FILE = "VERSION"

# Bumped when a field changes meaning, so an upload never reads a directory
# written to a different contract.
_SCHEMA = 1


class PreparedError(Exception):
    """The prepared directory is missing, malformed, or points outside itself."""


@dataclass(frozen=True)
class Prepared:
    """What ``release-prepare`` handed to the upload."""

    root: Path
    version: str
    language: str
    facts: dict[str, Any] = field(default_factory=dict)

    def file(self, relative: str) -> Path:
        """Resolve a path the manifest names, refusing one outside the directory.

        Raises:
            PreparedError: The path escapes the directory, is a symlink, or is
                not a regular file.

        """
        base = self.root.resolve()
        candidate = self.root / relative
        if candidate.is_symlink():
            raise PreparedError(f"{relative} in the prepared directory is a symlink")
        resolved = candidate.resolve()
        if not resolved.is_relative_to(base):
            raise PreparedError(f"{relative} resolves outside the prepared directory")
        if not resolved.is_file():
            raise PreparedError(f"{relative} is not in the prepared directory")
        return resolved


def load() -> Prepared | None:
    """Read the directory ``HYPERCI_RELEASE_PREPARED`` names, or None when unset.

    Raises:
        PreparedError: The variable is set but the directory cannot be used.

    """
    raw = os.environ.get(PREPARED_ENV, "").strip()
    if not raw:
        return None
    root = Path(raw)
    manifest = root / MANIFEST_NAME
    if not manifest.is_file() or manifest.is_symlink():
        raise PreparedError(
            f"{PREPARED_ENV}={raw} has no {MANIFEST_NAME}. The prepare job did "
            "not finish, or its artefact was not downloaded here."
        )
    try:
        data = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
    except ValueError as exc:
        raise PreparedError(f"{MANIFEST_NAME} is not JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != _SCHEMA:
        raise PreparedError(f"{MANIFEST_NAME} is not schema {_SCHEMA}")
    version = data.get("version")
    language = data.get("language")
    facts = data.get("facts", {})
    if not isinstance(version, str) or not version:
        raise PreparedError(f"{MANIFEST_NAME} carries no version")
    if not isinstance(language, str) or not language:
        raise PreparedError(f"{MANIFEST_NAME} carries no language")
    if not isinstance(facts, dict):
        raise PreparedError(f"{MANIFEST_NAME} facts must be a mapping")
    return Prepared(root=root, version=version, language=language, facts=facts)


def _tracked(root: Path, name: str) -> bool:
    """Say whether git tracks ``name``. Outside a git checkout, whether it exists."""
    result = run_cmd(
        ["git", "ls-files", "--error-unmatch", "--", name],
        capture=True,
        check=False,
        cwd=root,
    )
    if result.returncode == 0:
        return True
    if "not a git repository" in (result.stderr or "").lower():
        return (root / name).is_file()
    return False


def _snapshot(root: Path, config: CIConfig, target: Path, *, keep_version: bool) -> int:
    """Copy ``VERSION`` and the ``release.stamp_paths`` files into ``target``.

    A repo that commits no ``VERSION`` has opted out of one, so the file the
    stamp wrote is left behind. A broken ``stamp_paths`` is reported and
    skipped, the way ``release-commit`` treats it.

    Returns:
        How many files were copied.

    """
    names = [VERSION_FILE] if keep_version else []
    try:
        names += stamp_paths(config, root)
    except ValueError as exc:
        warn(f"release-prepare: {exc} -- carrying VERSION only")
    copied = 0
    for name in dict.fromkeys(names):
        source = root / name
        if ".git" in Path(name).parts or source.is_symlink() or not source.is_file():
            continue
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied += 1
    return copied


def _language_prepare(
    language: str, config: CIConfig, out_dir: Path
) -> tuple[int, dict[str, Any]]:
    """Run the language's ``prepare`` hook, if its release module has one."""
    from hyperi_ci.dispatch import _find_handler_module

    hook = getattr(_find_handler_module(language, "release"), "prepare", None)
    if hook is None:
        return 0, {}
    return hook(config, out_dir)


def prepare_release(
    version: str,
    *,
    out_dir: Path,
    project_dir: Path | None = None,
) -> int:
    """Stamp the release, run everything that executes repo code, write the result.

    Args:
        version: Version being released, with or without a leading ``v``.
        out_dir: Directory the prepared artefacts are written to.
        project_dir: Project root. Defaults to cwd.

    Returns:
        0 on success, 1 when the stamp, a check or the packaging fails.

    """
    version = version.removeprefix("v").strip()
    if not version:
        error("release-prepare: empty version")
        return 1
    root = (project_dir or Path.cwd()).resolve()
    language = detect_language(root)
    if not language:
        error("release-prepare: could not detect the project language")
        return 1

    keep_version = _tracked(root, VERSION_FILE)
    with group("Stamp the release version"):
        if stamp_version(version, project_dir=root) != 0:
            return 1

    config = load_config(reload=True, project_dir=root)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamped = out_dir / STAMPED_DIR
    copied = _snapshot(root, config, stamped, keep_version=keep_version)
    info(f"Carried {copied} stamped file(s) for release-commit")

    facts: dict[str, Any] = {}
    if config.get("release.enabled", False):
        # Same working directory as the handlers expect when `run` calls them.
        cwd = Path.cwd()
        os.chdir(root)
        try:
            rc, facts = _language_prepare(language, config, out_dir)
        finally:
            os.chdir(cwd)
        if rc != 0:
            return rc
    else:
        info("Release disabled in configuration -- nothing to package")

    from hyperi_ci.dispatch import _LANGUAGE_ALIASES

    manifest = {
        "schema": _SCHEMA,
        "version": version,
        "language": _LANGUAGE_ALIASES.get(language, language),
        "facts": facts,
    }
    (out_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    success(f"Prepared v{version} in {out_dir}")
    return 0


def restore_stamped(prepared: Prepared, root: Path, names: list[str]) -> list[str]:
    """Copy the named stamped files from the prepared directory into the checkout.

    The caller names the files from the checkout's own config. The prepared
    directory was written by a job that ran repo code, so a file it carries
    under any other name (``.git/config`` included) never leaves it.

    Returns:
        The repo-relative names restored.

    """
    restored: list[str] = []
    for name in dict.fromkeys(names):
        if ".git" in Path(name).parts:
            continue
        try:
            source = prepared.file(f"{STAMPED_DIR}/{name}")
        except PreparedError:
            continue
        target = root / name
        if target.is_symlink():
            warn(f"release-commit: {name} is a symlink in the checkout -- not restored")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        restored.append(name)
    return restored
