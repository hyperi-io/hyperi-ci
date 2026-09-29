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
in a job that holds no secrets, in two phases so each output can leave the job
before the next phase's code runs:

* ``--phase stamp`` stamps the version, runs ``release.stamp_cmd`` and copies
  the ``release.stamp_paths`` files into the output directory, for
  ``release-commit``.
* ``--phase package`` runs the language's checks and packing and writes
  ``prepared.json`` (version, language, commit, and the decisions that took
  repo code to reach) plus whatever it packs, such as the npm tarball.

``run release`` with ``HYPERCI_RELEASE_PREPARED`` naming the package directory
only uploads, and ``release-commit`` reads the stamp outputs from its
``stamped/`` subdirectory. Both came from a job that ran repo code, so
everything in them is read as data and checked: the version and language must
match the run's, every path must stay inside the directory, and ``VERSION`` is
never taken from them.
"""

import json
import os
import shutil
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from hyperi_ci.common import error, group, info, run_cmd, success, warn
from hyperi_ci.config import CIConfig, load_config
from hyperi_ci.detect import detect_language
from hyperi_ci.stamp import VERSION_FILE, carried_stamp_paths, stamp_version

PREPARED_ENV = "HYPERCI_RELEASE_PREPARED"
MANIFEST_NAME = "prepared.json"
STAMPED_DIR = "stamped"

# Bumped when a field changes meaning, so an upload never reads a directory
# written to a different contract.
_SCHEMA = 2


class Phase(StrEnum):
    """Which half of ``release-prepare`` to run; ``all`` is both, in order."""

    STAMP = "stamp"
    PACKAGE = "package"
    ALL = "all"


class PreparedError(Exception):
    """The prepared directory is missing, malformed, or points outside itself."""


@dataclass(frozen=True)
class Prepared:
    """What ``release-prepare`` handed to the upload."""

    root: Path
    version: str
    language: str
    head: str = ""
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
    head = data.get("head", "")
    facts = data.get("facts", {})
    if not isinstance(version, str) or not version:
        raise PreparedError(f"{MANIFEST_NAME} carries no version")
    if not isinstance(language, str) or not language:
        raise PreparedError(f"{MANIFEST_NAME} carries no language")
    if not isinstance(head, str):
        raise PreparedError(f"{MANIFEST_NAME} head must be a string")
    if not isinstance(facts, dict):
        raise PreparedError(f"{MANIFEST_NAME} facts must be a mapping")
    return Prepared(
        root=root, version=version, language=language, head=head, facts=facts
    )


def load_or_report(who: str) -> tuple[bool, Prepared | None]:
    """Load the prepared directory, logging why when it cannot be used.

    Returns:
        ``(ok, prepared)``. ``ok`` is False when the directory is set but
        unusable; ``prepared`` is None when it is unset.

    """
    try:
        return True, load()
    except PreparedError as exc:
        error(f"{who}: {exc}")
        return False, None


def tracked(root: Path, name: str) -> bool:
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


def head_commit(root: Path) -> str:
    """Return the checkout's HEAD commit, or "" outside a git checkout."""
    result = run_cmd(
        ["git", "rev-parse", "HEAD^{commit}"], capture=True, check=False, cwd=root
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _snapshot(root: Path, config: CIConfig, target: Path) -> int:
    """Copy the ``release.stamp_paths`` files the stamp rendered into ``target``.

    Returns:
        How many files were copied.

    """
    copied = 0
    for name in carried_stamp_paths(config, root, who="release-prepare"):
        source = root / name
        if source.is_symlink() or not source.is_file():
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


def _stamp_phase(version: str, root: Path, stamped: Path) -> int:
    with group("Stamp the release version"):
        if stamp_version(version, project_dir=root) != 0:
            return 1
    config = load_config(reload=True, project_dir=root)
    stamped.mkdir(parents=True, exist_ok=True)
    copied = _snapshot(root, config, stamped)
    info(f"Carried {copied} stamped file(s) for release-commit")
    return 0


def _package_phase(version: str, language: str, root: Path, out_dir: Path) -> int:
    config = load_config(reload=True, project_dir=root)
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
        "head": head_commit(root),
        "facts": facts,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    success(f"Prepared v{version} in {out_dir}")
    return 0


def prepare_release(
    version: str,
    *,
    out_dir: Path,
    phase: Phase = Phase.ALL,
    project_dir: Path | None = None,
) -> int:
    """Stamp the release and run everything in it that executes repo code.

    Args:
        version: Version being released, with or without a leading ``v``.
        out_dir: Directory the phase writes to. ``stamp`` writes the stamped
            files at their repo paths; ``package`` writes ``prepared.json`` and
            what it packs; ``all`` does both, the stamped files under
            ``stamped/``.
        phase: Which half to run.
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

    if phase is Phase.STAMP:
        return _stamp_phase(version, root, out_dir)
    if phase is Phase.ALL:
        rc = _stamp_phase(version, root, out_dir / STAMPED_DIR)
        if rc != 0:
            return rc
    return _package_phase(version, language, root, out_dir)


def restore_stamped(prepared: Prepared, root: Path, names: list[str]) -> list[str]:
    """Copy the named stamped files from the prepared directory into the checkout.

    The caller names the files from the checkout's own config. The prepared
    directory was written by a job that ran repo code, so a file it carries
    under any other name never leaves it.

    Returns:
        The repo-relative names restored.

    """
    restored: list[str] = []
    for name in dict.fromkeys(names):
        if name == VERSION_FILE or ".git" in Path(name).parts:
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
