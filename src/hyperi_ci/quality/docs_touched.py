# Project:   HyperI CI
# File:      src/hyperi_ci/quality/docs_touched.py
# Purpose:   Nudge when a change moves code and leaves every doc alone
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""The docs-untouched nudge - a reminder, never a gate.

Docs drift because the doc change is a separate act of will from the code
change. Naming it at review time is what closes that gap: the change moved
source and touched no markdown, so either a doc needs updating or it does not,
and the author is the one who knows.

**This never fails a build, in any mode.** Plenty of legitimate changes need no
doc: a refactor, a test, a dependency bump. A rule that is right often but not
always must not hold the merge button, or it gets disabled and takes the useful
signal with it. ``quality.docs_touched`` therefore only chooses between running
and not running; ``--strict`` cannot promote it, which is why this module does
not route the mode through the usual strict upgrade.
"""

from __future__ import annotations

import os
from pathlib import Path

from hyperi_ci.common import info, run_cmd, success
from hyperi_ci.config import CIConfig
from hyperi_ci.quality import findings as fdg

# Extensions that make a changed file "source" for this purpose.
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

# A test is source that documents itself. Changing one is not a reason to ask
# after the README.
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
    """Return the ref this change is measured against.

    In a pull request GitHub names the target branch in ``GITHUB_BASE_REF``;
    everywhere else the comparison is against the remote default branch, which
    is what a local ``hyperi-ci check`` wants before a push.
    """
    pr_base = os.environ.get("GITHUB_BASE_REF", "").strip()
    return f"origin/{pr_base}" if pr_base else "origin/HEAD"


def changed_files(root: Path, base: str) -> list[str] | None:
    """Return paths changed between ``base`` and HEAD, or None when unknowable.

    None covers a shallow clone, a repo with no remote and a first commit -
    all ordinary states in which there is simply no comparison to make.
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
    """Note a source-only change. ALWAYS returns 0 - this never gates."""
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
