# Project:   HyperI CI
# File:      src/hyperi_ci/release/freeze.py
# Purpose:   Freeze internal @main sibling refs → @vX for an atomic release graph
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Freeze hyperi-ci's own sibling refs for a release (issue #31 Phase 2b).

The language reusable workflows reference their siblings + composites at
`@main`, so a pinned consumer's graph floats. At release time we rewrite every
`hyperi-io/hyperi-ci/.github/(workflows|actions)/<name>@main` → `@vX` on the
(off-main) release commit, so `<lang>-ci.yml@vX` carries a frozen, atomic,
auditable graph. External action pins (setup-uv, checkout, rust-toolchain) are
NOT touched — those are the /deps tool's job.
"""

from __future__ import annotations

import re
from pathlib import Path

# Only hyperi-ci's OWN sibling refs at @main. The path is captured so any
# composite/workflow name is rewritten; the external-action pins (different
# owner/repo) never match.
_INTERNAL_MAIN = re.compile(
    r"(hyperi-io/hyperi-ci/\.github/(?:workflows|actions)/[^@\s]+)@main\b"
)


def freeze_text(text: str, version: str) -> str:
    """Rewrite internal `@main` sibling refs to `@v<version>`. External pins
    and already-pinned internal refs are left untouched."""
    tag = f"v{version.removeprefix('v')}"
    return _INTERNAL_MAIN.sub(rf"\1@{tag}", text)


def count_floating(text: str) -> int:
    """Number of internal `hyperi-io/hyperi-ci/...@main` refs remaining."""
    return len(_INTERNAL_MAIN.findall(text))


def _pipeline_files(root: Path) -> list[Path]:
    gh = root / ".github"
    files = sorted((gh / "workflows").glob("*.yml"))
    actions = gh / "actions"
    if actions.is_dir():
        files += sorted(actions.glob("*/action.yml"))
    return files


def freeze_repo(version: str, root: Path | None = None) -> list[Path]:
    """Freeze every pipeline file in place; verify zero internal `@main` remain.

    Returns the files changed. Raises RuntimeError if any internal `@main`
    survives (a missed ref would be a floating hole in the frozen graph).
    """
    root = root or Path.cwd()
    changed: list[Path] = []
    for path in _pipeline_files(root):
        original = path.read_text()
        frozen = freeze_text(original, version)
        if frozen != original:
            path.write_text(frozen)
            changed.append(path)

    leftover = sum(count_floating(p.read_text()) for p in _pipeline_files(root))
    if leftover:
        raise RuntimeError(
            f"freeze-internals: {leftover} internal @main ref(s) still floating "
            "after rewrite — the frozen graph would have a hole"
        )
    return changed
