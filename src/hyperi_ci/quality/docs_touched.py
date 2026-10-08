# Project:   HyperI CI
# File:      src/hyperi_ci/quality/docs_touched.py
# Purpose:   Nudge when a change moves code and leaves every doc alone
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Note a change that touches source and no doc; never fails a build.

Many legitimate changes need no doc, so ``quality.docs_touched`` only chooses
between running and not, and the mode skips the ``--strict`` upgrade.
"""

import os
from pathlib import Path

from hyperi_ci.common import info, run_cmd, success
from hyperi_ci.config import CIConfig
from hyperi_ci.quality import findings as fdg

_SOURCE_SUFFIXES = {
    ".py",
    ".rs",
    ".go",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".java",
    ".rb",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
}

_DOC_SUFFIXES = {".md", ".markdown", ".rst", ".adoc"}

# Test files count as neither source nor docs.
_TEST_MARKERS = ("tests/", "test/", "_test.", "test_", ".test.", ".spec.")


def classify(paths: list[str]) -> tuple[list[str], list[str]]:
    """Split changed ``paths`` into (source, docs). Tests count as neither."""
    source: list[str] = []
    docs: list[str] = []
    for raw in paths:
        path = raw.strip()
        if not path:
            continue
        suffix = Path(path).suffix.lower()
        if suffix in _DOC_SUFFIXES:
            docs.append(path)
        elif suffix in _SOURCE_SUFFIXES and not any(
            marker in path for marker in _TEST_MARKERS
        ):
            source.append(path)
    return source, docs


def base_ref() -> str:
    """Return the ref to diff against: the PR's base branch, else ``origin/HEAD``."""
    pr_base = os.environ.get("GITHUB_BASE_REF", "").strip()
    return f"origin/{pr_base}" if pr_base else "origin/HEAD"


def changed_files(root: Path, base: str) -> list[str] | None:
    """Return paths changed between ``base`` and HEAD, or None when git cannot say.

    None covers a shallow clone, a repo with no remote, and a first commit.
    """
    try:
        result = run_cmd(
            ["git", "diff", "--name-only", f"{base}...HEAD"],
            check=False,
            capture=True,
            cwd=root,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.splitlines()


def run(
    config: CIConfig,
    *,
    root: Path | None = None,
    sarif_path: str | Path | None = None,
) -> int:
    """Note a source-only change; always returns 0."""
    if str(config.get("quality.docs_touched", "warn")).strip().lower() == "disabled":
        info("  docs-touched: disabled")
        return 0

    root = Path(root or Path.cwd())
    base = base_ref()
    paths = changed_files(root, base)
    if paths is None:
        info(f"  docs-touched: no comparison against {base} available - skipping")
        return 0
    if not paths:
        info(f"  docs-touched: nothing changed against {base} - skipping")
        return 0

    source, docs = classify(paths)
    if not source or docs:
        success("  docs-touched: nothing to note")
        return 0

    fdg.surface(
        "docs-touched",
        [
            fdg.Finding(
                tool="docs-touched",
                path="",
                line=None,
                level="notice",
                rule="docs/untouched",
                message=(
                    f"{len(source)} source file(s) changed and no doc did. If the "
                    "behaviour, interface or setup moved, the doc for it moved too."
                ),
            )
        ],
        sarif_path=sarif_path,
    )
    return 0
