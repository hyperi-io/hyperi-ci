# Project:   HyperI CI
# File:      src/hyperi_ci/stamp.py
# Purpose:   Central version stamping -- VERSION file + language manifest
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Stamp the release version into the project before build.

Two layers:

  * VERSION file -- identical for every language, always written here.
  * manifest (Cargo.toml / pyproject.toml / package.json ...) -- each language's
    `stamp_manifest()` owns it, routed by language detection.

A repo whose committed files carry the version elsewhere (a generated OpenAPI
spec) names a command in ``release.stamp_cmd``, run after both layers, and the
files it writes in ``release.stamp_paths``, which ``release-commit`` puts back
on the branch. ``--no-stamp-cmd`` skips the command, for the Container job,
which holds registry credentials.
"""

import re
import shlex
from pathlib import Path, PurePosixPath

from hyperi_ci.common import error, info, run_cmd, warn
from hyperi_ci.config import CIConfig, load_config
from hyperi_ci.detect import detect_language

# Env form of `stamp-version --no-stamp-cmd`, ignored rather than rejected by a
# CLI too old to know the flag.
SKIP_STAMP_CMD_ENV = "HYPERCI_STAMP_SKIP_CMD"


class StampError(Exception):
    """A manifest cannot carry the release version, so the build must not run."""


def replace_toml_table_version(text: str, table: str, version: str) -> str:
    """Replace `version = "..."` inside one TOML table, if present.

    Scoped to the named table (e.g. ``package``, ``workspace.package``,
    ``project``), from its header to the next ``[`` header. Never inserts, so a
    dynamic-version project with no ``version`` key is left untouched.
    """
    pattern = re.compile(
        r"(?ms)^\[" + re.escape(table) + r"\].*?(?=^\[|\Z)",
    )
    match = pattern.search(text)
    if not match:
        return text
    block = match.group(0)
    new_block = re.sub(
        r'(?m)^(version\s*=\s*)"[^"]*"',
        rf'\g<1>"{version}"',
        block,
        count=1,
    )
    return text[: match.start()] + new_block + text[match.end() :]


# language -> (module path, function name), lazy-imported so one language's
# handlers are not loaded for another.
_MANIFEST_STAMPERS: dict[str, tuple[str, str]] = {
    "rust": ("hyperi_ci.languages.rust.build", "stamp_manifest"),
    "python": ("hyperi_ci.languages.python.build", "stamp_manifest"),
    "typescript": ("hyperi_ci.languages.typescript.build", "stamp_manifest"),
    "javascript": ("hyperi_ci.languages.typescript.build", "stamp_manifest"),
    "golang": ("hyperi_ci.languages.golang.build", "stamp_manifest"),
}


def stamp_command(config: CIConfig) -> list[str] | None:
    """Resolve ``release.stamp_cmd`` to an argv, or None when unset.

    A string is split like a shell would but never run through one, and a list
    is taken as the argv.

    Raises:
        ValueError: The value is neither a string nor a list of strings, or
            the string does not parse.

    """
    raw = config.get("release.stamp_cmd")
    if raw is None or raw == "" or raw == []:
        return None
    if isinstance(raw, str):
        argv = shlex.split(raw)
    elif isinstance(raw, list) and all(isinstance(part, str) for part in raw):
        argv = list(raw)
    else:
        msg = f"release.stamp_cmd must be a string or a list of strings, got {raw!r}"
        raise ValueError(msg)
    return argv or None


# Files release-commit owns outright, never carried as a stamp output.
VERSION_FILE = "VERSION"
CHANGELOG_FILE = "CHANGELOG.md"
SUPPLEMENT_FILE = ".github/release-notes/NEXT.md"
RESERVED_PATHS = (VERSION_FILE, CHANGELOG_FILE, SUPPLEMENT_FILE)


def repo_paths(raw: object, root: Path, key: str) -> list[str]:
    """Resolve a config list of paths to repo-relative POSIX paths inside ``root``.

    An entry that is absolute, holds ``..``, resolves outside the repo (a
    symlink included) or sits under ``.git`` is refused rather than trusted.

    Args:
        raw: The configured value.
        root: Repo root the paths are relative to.
        key: Config key, for the error message.

    Raises:
        ValueError: The value is not a list, or an entry breaks a rule above.

    """
    raw = raw or []
    if not isinstance(raw, list):
        msg = f"{key} must be a list of paths, got {raw!r}"
        raise ValueError(msg)
    base = root.resolve()
    paths: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            msg = f"{key}: {entry!r} is not a path"
            raise ValueError(msg)
        posix = PurePosixPath(entry.strip().replace("\\", "/"))
        if posix.is_absolute() or ".." in posix.parts:
            msg = f"{key}: {entry} must be relative to the repo root"
            raise ValueError(msg)
        if ".git" in posix.parts:
            msg = f"{key}: {entry} is inside .git"
            raise ValueError(msg)
        if not (base / posix).resolve().is_relative_to(base):
            msg = f"{key}: {entry} resolves outside the repo"
            raise ValueError(msg)
        if str(posix) not in paths:
            paths.append(str(posix))
    return paths


def stamp_paths(config: CIConfig, root: Path) -> list[str]:
    """Resolve ``release.stamp_paths`` to repo-relative POSIX paths.

    They land in a commit on the default branch, so :func:`repo_paths` rules
    apply.

    Raises:
        ValueError: See :func:`repo_paths`.

    """
    return repo_paths(config.get("release.stamp_paths"), root, "release.stamp_paths")


def carried_stamp_paths(config: CIConfig, root: Path, *, who: str) -> list[str]:
    """Return the ``stamp_paths`` a release carries from the stamp to the commit.

    Shared by the prepare snapshot, the restore and release-commit. A broken
    list is reported and treated as empty, and reserved names are dropped.

    Args:
        config: Merged config of the checkout.
        root: Repo root.
        who: Command name for the log lines.

    """
    try:
        listed = stamp_paths(config, root)
    except ValueError as exc:
        error(f"{who}: {exc} -- leaving release.stamp_paths out")
        return []
    kept: list[str] = []
    for name in listed:
        if name in RESERVED_PATHS:
            if name != VERSION_FILE and name != CHANGELOG_FILE:
                warn(f"{who}: release.stamp_paths cannot include {name}")
            continue
        kept.append(name)
    return kept


def _run_stamp_command(root: Path) -> int:
    """Run ``release.stamp_cmd`` from ``root``. Returns its exit code."""
    try:
        argv = stamp_command(load_config(project_dir=root, reload=True))
    except ValueError as exc:
        error(f"stamp-version: {exc}")
        return 1
    if argv is None:
        return 0
    info(f"Running release.stamp_cmd: {shlex.join(argv)}")
    try:
        result = run_cmd(argv, check=False, cwd=root)
    except OSError as exc:
        error(f"stamp-version: release.stamp_cmd could not start: {exc}")
        return 1
    if result.returncode != 0:
        error(f"stamp-version: release.stamp_cmd exited {result.returncode}")
        return 1
    return 0


def stamp_version(
    version: str, project_dir: Path | None = None, *, run_stamp_cmd: bool = True
) -> int:
    """Write the version into VERSION and the language manifest.

    Then runs ``release.stamp_cmd``, when set, so files generated from the
    version pick it up.

    Args:
        version: Release version, with or without a leading ``v``.
        project_dir: Project root. Defaults to cwd.
        run_stamp_cmd: False for a job that holds credentials, which must run
            none of the repo's code.

    Returns:
        0 on success, 1 if ``version`` is empty, the manifest cannot carry
        it, or ``release.stamp_cmd`` fails.

    """
    version = version.removeprefix("v").strip()
    if not version:
        error("stamp-version: empty version")
        return 1

    root = project_dir or Path.cwd()

    (root / "VERSION").write_text(f"{version}\n", encoding="utf-8", newline="\n")
    info(f"Stamped VERSION: {version}")

    language = detect_language(root)
    if language and language in _MANIFEST_STAMPERS:
        module_name, func_name = _MANIFEST_STAMPERS[language]
        import importlib

        # module_name comes from the hardcoded _MANIFEST_STAMPERS table.
        # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
        stamp_manifest = getattr(importlib.import_module(module_name), func_name)
        try:
            stamp_manifest(version, root)
        except StampError as exc:
            error(f"stamp-version: {exc}")
            return 1
    elif language:
        info(f"No manifest stamp for {language} -- VERSION file is authoritative")
    else:
        warn("Could not detect language -- wrote VERSION only")

    if not run_stamp_cmd:
        info("Not running release.stamp_cmd: --no-stamp-cmd")
        return 0
    return _run_stamp_command(root)
