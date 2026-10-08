# Project:   HyperI CI
# File:      src/hyperi_ci/quality/lint_docs.py
# Purpose:   Orchestrate the documentation quality dimension
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Orchestrate the documentation checks.

Runs inside ``stage_quality``, and as ``hyperi-ci lint-docs <dir>`` for a
docs-only repo. The checks, in cost order:

1. doc-paths - path-existence drift, pure Python.
2. doc-links - lychee over internal links and anchors, offline.
3. mermaid-parse - mermaid's own grammar over every fenced block.
4. markdownlint - markdown syntax.
5. docs-touched - the source-changed, docs-did-not nudge, which never gates.

Every check defaults to ``warn``, and a repo promotes one once it reads zero.
doc-paths leaves link destinations to lychee unless doc-paths is stricter.
"""

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

    Every check runs even after one fails. Returns non-zero only when a check
    promoted to ``blocking`` fails.
    """
    root = Path(root)
    files = discover_markdown_files(root, exclude_dirs=get_exclude_dirs(config._raw))
    if not files:
        info(f"lint-docs: no markdown under {root} - skipping")
        return 0

    info(f"lint-docs: {len(files)} markdown file(s) under {root}")
    lychee_mode = doc_links.planned_mode(config)

    with group("doc path drift"):
        paths_rc = doc_paths.run(
            files, config, root=root, lychee_mode=lychee_mode, sarif_path=sarif_path
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
