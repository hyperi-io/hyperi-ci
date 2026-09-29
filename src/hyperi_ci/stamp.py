# Project:   HyperI CI
# File:      src/hyperi_ci/stamp.py
# Purpose:   Central version stamping -- VERSION file + language manifest
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Stamp the release version into the project before build.

Two layers, split on the central/language rule:

  * VERSION file -- identical for every language, always written here.
  * manifest (Cargo.toml / pyproject.toml / package.json ...) -- differs per
    language, so each language's `stamp_manifest()` owns it in full.

The workflow calls this once (`hyperi-ci stamp-version <version>`) with no
per-language branching; language detection routes the manifest stamp.

A repo whose committed files carry the version somewhere hyperi-ci cannot know
about (a generated OpenAPI spec) names a command in ``release.stamp_cmd``, run
after both layers, and the files it writes in ``release.stamp_paths``, which
``release-commit`` puts back on the branch.
"""

import re
import shlex
from pathlib import Path, PurePosixPath

from hyperi_ci.common import error, info, run_cmd, warn
from hyperi_ci.config import CIConfig, load_config
from hyperi_ci.detect import detect_language


class StampError(Exception):
    """A manifest cannot carry the release version, so the build must not run."""


def replace_toml_table_version(text: str, table: str, version: str) -> str:
    """Replace `version = "..."` inside one TOML table, if present.

    Scoped to the named table (e.g. ``package``, ``workspace.package``,
    ``project``): matches from the ``[table]`` header to the next ``[``
    header. Never inserts -- a dynamic-version project with no ``version``
    key is left untouched. Generic string op shared by the TOML-based
    language stampers; the choice of which table is the language's call.
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


# language -> (module path, function name). Lazy-imported so stamping a
# Rust project doesn't drag in the Python/Node handlers (and vice versa).
_MANIFEST_STAMPERS: dict[str, tuple[str, str]] = {
    "rust": ("hyperi_ci.languages.rust.build", "stamp_manifest"),
    "python": ("hyperi_ci.languages.python.build", "stamp_manifest"),
    "typescript": ("hyperi_ci.languages.typescript.build", "stamp_manifest"),
    "javascript": ("hyperi_ci.languages.typescript.build", "stamp_manifest"),
    "golang": ("hyperi_ci.languages.golang.build", "stamp_manifest"),
}


def stamp_command(config: CIConfig) -> list[str] | None:
    """Resolve ``release.stamp_cmd`` to an argv, or None when unset.

    A string is split the way a shell would split it, but never run through
    one; a list is taken as the argv as-is.

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


def stamp_paths(config: CIConfig, root: Path) -> list[str]:
    """Resolve ``release.stamp_paths`` to repo-relative POSIX paths.

    These become paths in a commit written to the default branch, so an entry
    that is absolute or climbs out of the repo is refused rather than trusted.

    Raises:
        ValueError: The value is not a list, or an entry is not a relative
            path inside ``root``.

    """
    raw = config.get("release.stamp_paths") or []
    if not isinstance(raw, list):
        msg = f"release.stamp_paths must be a list of paths, got {raw!r}"
        raise ValueError(msg)
    base = root.resolve()
    paths: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            msg = f"release.stamp_paths: {entry!r} is not a path"
            raise ValueError(msg)
        posix = PurePosixPath(entry.strip().replace("\\", "/"))
        if posix.is_absolute() or ".." in posix.parts:
            msg = f"release.stamp_paths: {entry} must be relative to the repo root"
            raise ValueError(msg)
        if not (base / posix).resolve().is_relative_to(base):
            msg = f"release.stamp_paths: {entry} resolves outside the repo"
            raise ValueError(msg)
        if str(posix) not in paths:
            paths.append(str(posix))
    return paths


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


def stamp_version(version: str, project_dir: Path | None = None) -> int:
    """Write the version into VERSION and the language manifest.

    Then runs ``release.stamp_cmd``, when set, so files generated from the
    version pick it up.

    Args:
        version: Release version, with or without a leading ``v``.
        project_dir: Project root. Defaults to cwd.

    Returns:
        0 on success, 1 if ``version`` is empty, the manifest cannot carry
        it, or ``release.stamp_cmd`` fails.

    """
    version = version.removeprefix("v").strip()
    if not version:
        error("stamp-version: empty version")
        return 1

    root = project_dir or Path.cwd()

    # Central: VERSION is the language-agnostic source of truth, always written.
    (root / "VERSION").write_text(f"{version}\n", encoding="utf-8", newline="\n")
    info(f"Stamped VERSION: {version}")

    # Language-specific: manifest stamp lives in the language's own code.
    language = detect_language(root)
    if language and language in _MANIFEST_STAMPERS:
        module_name, func_name = _MANIFEST_STAMPERS[language]
        import importlib

        # module_name comes from the hardcoded _MANIFEST_STAMPERS table, not user
        # input, so there is no injection surface.
        # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
        stamp_manifest = getattr(importlib.import_module(module_name), func_name)
        try:
            stamp_manifest(version, root)
        except StampError as exc:
            error(f"stamp-version: {exc}")
            return 1
    elif language:
        info(f"No manifest stamp for {language} — VERSION file is authoritative")
    else:
        warn("Could not detect language — wrote VERSION only")

    return _run_stamp_command(root)
