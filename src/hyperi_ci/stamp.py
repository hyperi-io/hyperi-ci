# Project:   HyperI CI
# File:      src/hyperi_ci/stamp.py
# Purpose:   Central version stamping — VERSION file + language manifest
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Stamp the release version into the project before build.

Two layers, split on the central/language rule:

  * VERSION file — identical for every language, always written here.
  * manifest (Cargo.toml / pyproject.toml / package.json …) — differs per
    language, so each language's `stamp_manifest()` owns it in full.

The workflow calls this once (`hyperi-ci stamp-version <version>`) with no
per-language branching; language detection routes the manifest stamp.
"""

from __future__ import annotations

import re
from pathlib import Path

from hyperi_ci.common import info, warn
from hyperi_ci.detect import detect_language


def replace_toml_table_version(text: str, table: str, version: str) -> str:
    """Replace `version = "..."` inside one TOML table, if present.

    Scoped to the named table (e.g. ``package``, ``workspace.package``,
    ``project``): matches from the ``[table]`` header to the next ``[``
    header. Never inserts — a dynamic-version project with no ``version``
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


def stamp_version(version: str, project_dir: Path | None = None) -> int:
    """Write the version into VERSION and the language manifest.

    Args:
        version: Release version, with or without a leading ``v``.
        project_dir: Project root. Defaults to cwd.

    Returns:
        0 on success, 1 if ``version`` is empty.
    """
    version = version.removeprefix("v").strip()
    if not version:
        from hyperi_ci.common import error

        error("stamp-version: empty version")
        return 1

    root = project_dir or Path.cwd()

    # Central: VERSION is the language-agnostic source of truth, always written.
    (root / "VERSION").write_text(f"{version}\n")
    info(f"Stamped VERSION: {version}")

    # Language-specific: manifest stamp lives in the language's own code.
    language = detect_language(root)
    if language and language in _MANIFEST_STAMPERS:
        module_name, func_name = _MANIFEST_STAMPERS[language]
        import importlib

        stamp_manifest = getattr(importlib.import_module(module_name), func_name)
        stamp_manifest(version, root)
    elif language:
        info(f"No manifest stamp for {language} — VERSION file is authoritative")
    else:
        warn("Could not detect language — wrote VERSION only")

    return 0
