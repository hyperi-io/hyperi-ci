# Project:   HyperI CI
# File:      src/hyperi_ci/quality/diagram_check.py
# Purpose:   Mermaid diagrams against the HyperI diagram standard
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Mermaid diagrams against the HyperI diagram standard, ``warn`` by default.

The rules live in merrow, run through uvx at the versions.yaml pin with
``merrow diagram lint --no-render --json``, which needs no browser. A
``quality.ignore`` entry with ``tool: diagram-check`` names a page to leave out.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

from hyperi_ci.common import info
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_tool_cmd, resolve_tool_mode
from hyperi_ci.quality import findings as fdg
from hyperi_ci.quality.ignores import for_tool, load_ignores
from hyperi_ci.tools import missing_tool
from hyperi_ci.versions import tool_version

_FENCE = re.compile(r"^\s*(```|~~~)\s*mermaid\b", re.MULTILINE)
_LOCATION = re.compile(r"^(?P<path>.+?):(?P<line>\d+)\s+(?P<message>.*)$")
# A problem reads `[acc-title] R7 no accTitle`. The R-number stays in the message.
_RULE = re.compile(r"^\[(?P<code>[\w-]+)\]\s*(?P<rest>.*)$")


def parse(output: str, root: Path) -> list[fdg.Finding]:
    """Turn merrow's ``--json`` report into findings."""
    report = json.loads(output)
    lines = list(report.get("page") or [])
    for diagram in report.get("diagrams") or []:
        name = diagram.get("name", "")
        lines += [f"{name} {problem}" for problem in diagram.get("problems") or []]
    found = []
    for text in lines:
        match = _LOCATION.match(text)
        if match:
            path, line, message = match["path"], int(match["line"]), match["message"]
        else:
            path, _, message = text.partition(" ")
            line = None
        rule = "diagram"
        if m := _RULE.match(message):
            rule, message = m["code"], m["rest"]
        # merrow runs in root, so `lint-docs docs` reports docs/ pages as root-relative.
        where = fdg.relpath(root / path)
        found.append(fdg.Finding("diagram-check", where, line, "error", rule, message))
    return found


def run(
    files: list[Path],
    config: CIConfig,
    *,
    root: Path | None = None,
    sarif_path: str | Path | None = None,
) -> int:
    """Run merrow's source pass over ``files`` and return the exit code."""
    mode = resolve_tool_mode("diagram_check", config, default="warn")
    if mode == "disabled":
        info("  diagram-check: disabled")
        return 0
    root = Path(root or Path.cwd())
    skipped = {e.id for e in for_tool(load_ignores(config._raw), "diagram-check")}
    # An id is repo-relative, so `lint-docs <subdir>` from the root matches it too.
    pages = [
        f
        for f in files
        if not {fdg.relpath(f, root), fdg.relpath(f)} & skipped
        and _FENCE.search(f.read_text(encoding="utf-8", errors="replace"))
    ]
    if not pages:
        info("  diagram-check: no mermaid diagrams - skipping")
        return 0
    cmd = resolve_tool_cmd(
        ["merrow"], via="uvx", spec=f"merrow=={tool_version('merrow')}"
    )
    if cmd == ["merrow"] and not shutil.which("merrow"):
        return missing_tool("merrow", mode)

    info(f"  diagram-check: {len(pages)} page(s) with mermaid...")
    cmd += ["diagram", "lint", "--no-render", "--json"]
    cmd += [fdg.relpath(page, root) for page in pages]

    def parsed(result: subprocess.CompletedProcess[str]) -> list[fdg.Finding]:
        return parse(result.stdout, root) if result.stdout.strip() else []

    found = fdg.run_check("diagram-check", cmd, mode, parsed, cwd=root)
    if isinstance(found, int):
        return found
    fdg.report("diagram-check", found, mode, sarif_path=sarif_path)
    return fdg.verdict("diagram-check", len(found), mode, f"{len(pages)} page(s) clean")
