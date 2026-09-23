# Project:   HyperI CI
# File:      src/hyperi_ci/quality/lint_docs.py
# Purpose:   Orchestrate the documentation quality dimension
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Orchestrate documentation quality checking.

Runs from two places, like the container dimension: inside ``stage_quality``
for a repo with a language pipeline, and as ``hyperi-ci lint-docs <dir>`` for a
docs-only repo that has none.

Five checks, in cost order:

1. **doc-paths** - path-existence drift. Pure Python, so it always runs.
2. **doc-links** - lychee over internal links + anchors, offline.
3. **mermaid-parse** - mermaid's own grammar over every fenced block.
4. **markdownlint** - mechanical markdown syntax.
5. **docs-touched** - the source-changed-docs-did-not nudge.

**Every one starts at ``warn``.** A new lint introduced as blocking on an
existing tree fails CI for everybody until the backlog is cleared, so the
estate learns to disable it. The ratchet is the alternative with the same end
state: land at warn, watch the count, promote the check in a repo once that
repo reads zero. doc-paths, doc-links and mermaid-parse are deterministic and
carry no style opinion, so they are the ones ready to be promoted first;
markdownlint will light up an existing tree and docs-touched is advisory by
construction and cannot be promoted at all.

doc-paths and doc-links overlap on link destinations, so the link half of
doc-paths is turned off whenever lychee will actually run - one broken link,
one finding, whichever tool is present.
"""

from __future__ import annotations

from pathlib import Path

from hyperi_ci.common import get_exclude_dirs, group, info
from hyperi_ci.config import CIConfig
from hyperi_ci.quality import (
    doc_links,
    doc_paths,
    docs_touched,
    markdownlint,
    mermaid_parse,
)
from hyperi_ci.quality.targets import discover_markdown_files


def run(
    root: Path | str, config: CIConfig, *, sarif_path: str | Path | None = None
) -> int:
    """Run every documentation check over the markdown under ``root``.

    Returns non-zero only when a check a repo has PROMOTED to ``blocking``
    fails. Every check runs even after one fails, so a single broken link does
    not hide the rest of the report.
    """
    root = Path(root)
    files = discover_markdown_files(root, exclude_dirs=get_exclude_dirs(config._raw))
    if not files:
        info(f"lint-docs: no markdown under {root} - skipping")
        return 0

    info(f"lint-docs: {len(files)} markdown file(s) under {root}")
    lychee_runs = doc_links.will_run(config)

    with group("doc path drift"):
        paths_rc = doc_paths.run(
            files, config, root=root, check_links=not lychee_runs, sarif_path=sarif_path
        )

    with group("internal links and anchors (lychee)"):
        links_rc = doc_links.run(files, config, root=root, sarif_path=sarif_path)

    with group("mermaid diagram parsing"):
        mermaid_rc = mermaid_parse.run(files, config, root=root, sarif_path=sarif_path)

    with group("markdown syntax (markdownlint)"):
        lint_rc = markdownlint.run(files, config, root=root, sarif_path=sarif_path)

    with group("docs-untouched nudge"):
        docs_touched.run(config, root=root, sarif_path=sarif_path)

    return paths_rc or links_rc or mermaid_rc or lint_rc
