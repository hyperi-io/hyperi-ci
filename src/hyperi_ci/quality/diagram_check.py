# Project:   HyperI CI
# File:      src/hyperi_ci/quality/diagram_check.py
# Purpose:   Mermaid diagrams against the HyperI diagram standard
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Mermaid diagrams against the HyperI diagram standard, ``warn`` by default.

The checker is hyperi-ai's ``tools/diagram_check.py``, in a private repo, so it
is never copied here. ``hyperi-ci fetch-diagram-check`` downloads it from the
branch the org config names, with a token that can read hyperi-ai, and the
quality stage runs its source pass (``--no-render``) over every page with a
mermaid fence. Without a fetched copy the check skips and says so. A
``quality.ignore`` entry with ``tool: diagram-check`` names a page to leave out.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from hyperi_ci.common import error, info, run_cmd, set_github_output, success
from hyperi_ci.config import CIConfig, OrgConfig
from hyperi_ci.languages.quality_common import resolve_tool_mode
from hyperi_ci.quality import findings as fdg
from hyperi_ci.quality.ignores import for_tool, load_ignores
from hyperi_ci.versions import runtime_version

SCRIPT_ENV = "HYPERCI_DIAGRAM_CHECK"
_FENCE = re.compile(r"^\s*(```|~~~)\s*mermaid\b", re.MULTILINE)
_LOCATION = re.compile(r"^(?P<path>.+?):(?P<line>\d+)\s+(?P<message>.*)$")
_RULE = re.compile(r"R\d+\b")


def fetch(org: OrgConfig, dest: Path) -> Path | None:
    """Download the checker into ``dest`` with ``gh`` and its ``GH_TOKEN``.

    The org config's ref is resolved to a commit first, and the log names it.
    Sets the step output ``path`` in GitHub Actions.

    Returns:
        The script's path, or None when it could not be fetched.
    """
    repo = f"repos/{org.github_org}/{org.diagram_check_repo}"
    head = run_cmd(
        ["gh", "api", f"{repo}/commits/{org.diagram_check_ref}", "--jq", ".sha"],
        capture=True,
        check=False,
    )
    sha = head.stdout.strip()
    if head.returncode != 0 or not sha:
        error(
            f"diagram-check: could not resolve {org.diagram_check_ref}: "
            f"{head.stderr.strip()}"
        )
        return None
    endpoint = f"{repo}/contents/{org.diagram_check_path}?ref={sha}"
    raw = "Accept: application/vnd.github.raw"
    result = run_cmd(["gh", "api", endpoint, "-H", raw], capture=True, check=False)
    if result.returncode != 0 or not result.stdout:
        error(f"diagram-check: could not fetch {endpoint}: {result.stderr.strip()}")
        return None
    dest.mkdir(parents=True, exist_ok=True)
    script = dest / Path(org.diagram_check_path).name
    script.write_text(result.stdout, encoding="utf-8", newline="\n")
    set_github_output(path=str(script))
    success(f"diagram-check: fetched {org.diagram_check_ref} at {sha[:12]} to {script}")
    return script


def parse(output: str, root: Path) -> list[fdg.Finding]:
    """Turn the checker's ``--json`` report into findings."""
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
        if _RULE.match(message):
            rule, _, message = message.partition(" ")
        # The checker runs in root, so `lint-docs docs` reports docs/ pages as root-relative.
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
    """Run the checker's source pass over ``files`` and return the exit code."""
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
    script = os.environ.get(SCRIPT_ENV, "")
    uv = shutil.which("uv")
    if not script or not Path(script).is_file() or not uv:
        info(
            "  diagram-check: no checker fetched (hyperi-ci fetch-diagram-check) - skipping"
        )
        return 0

    info(f"  diagram-check: {len(pages)} page(s) with mermaid...")
    cmd = [
        uv,
        "run",
        "--python",
        runtime_version("python"),
        script,
        "--no-render",
        "--json",
    ]
    cmd += [fdg.relpath(page, root) for page in pages]

    def parsed(result: subprocess.CompletedProcess[str]) -> list[fdg.Finding]:
        return parse(result.stdout, root) if result.stdout.strip() else []

    found = fdg.run_check("diagram-check", cmd, mode, parsed, cwd=root)
    if isinstance(found, int):
        return found
    fdg.report("diagram-check", found, mode, sarif_path=sarif_path)
    return fdg.verdict("diagram-check", len(found), mode, f"{len(pages)} page(s) clean")
