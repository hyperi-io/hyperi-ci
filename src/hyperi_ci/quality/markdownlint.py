# Project:   HyperI CI
# File:      src/hyperi_ci/quality/markdownlint.py
# Purpose:   Mechanical markdown syntax linting via markdownlint-cli2
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Markdown syntax linting via markdownlint-cli2, ``warn`` by default.

A repo with no ``.markdownlint*`` config gets ``config/markdownlint.yaml``,
which turns MD013 off because the house style has no markdown width limit. A
repo config replaces the default outright rather than merging with it. A
``markdownlint-cli2`` on PATH wins; otherwise on CI the pinned release comes
from :mod:`hyperi_ci.quality.node_tools`.
"""

import re
import shutil
from pathlib import Path

from hyperi_ci.common import error, info, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_tool_mode
from hyperi_ci.quality import findings as fdg
from hyperi_ci.quality import node_tools
from hyperi_ci.tools import missing_tool

DEFAULT_CONFIG = Path(__file__).parent.parent / "config" / "markdownlint.yaml"

# Every config name markdownlint-cli2 discovers on its own.
_REPO_CONFIG_NAMES = (
    ".markdownlint-cli2.jsonc",
    ".markdownlint-cli2.yaml",
    ".markdownlint-cli2.cjs",
    ".markdownlint-cli2.mjs",
    ".markdownlint.jsonc",
    ".markdownlint.json",
    ".markdownlint.yaml",
    ".markdownlint.yml",
    ".markdownlint.cjs",
    ".markdownlint.mjs",
)

# `path:line[:col] severity MDnnn/alias message [Context: "..."]`
_FINDING = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+)(?::\d+)?\s+"
    r"(?P<level>\w+)\s+(?P<rule>MD\d+)/(?P<alias>\S+)\s+(?P<message>.*)$"
)


def repo_config(root: Path) -> Path | None:
    """Return the repo's own markdownlint config, or None when it has none."""
    for name in _REPO_CONFIG_NAMES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def parse(output: str) -> list[fdg.Finding]:
    """Parse markdownlint-cli2's text output into findings.

    cli2 has no JSON output without an extra formatter package. Banner and
    summary lines do not match and are dropped.
    """
    out: list[fdg.Finding] = []
    for line in output.splitlines():
        match = _FINDING.match(line.strip())
        if match is None:
            continue
        rule = match.group("rule")
        out.append(
            fdg.Finding(
                tool="markdownlint",
                path=match.group("path"),
                line=int(match.group("line")),
                level=fdg.normalise_level(match.group("level")),
                rule=f"{rule}/{match.group('alias')}",
                message=match.group("message"),
                url=f"https://github.com/DavidAnson/markdownlint/blob/main/doc/{rule}.md",
            )
        )
    return out


def run(
    files: list[Path],
    config: CIConfig,
    *,
    root: Path | None = None,
    sarif_path: str | Path | None = None,
) -> int:
    """Lint ``files`` for markdown syntax faults; return the exit code.

    Returns 1 when a blocking check finds a violation, or in CI is missing the
    tool or cannot run or complete it.
    """
    mode = resolve_tool_mode("markdownlint", config, default="warn")
    if mode == "disabled":
        info("  markdownlint: disabled")
        return 0
    if not files:
        info("  markdownlint: no markdown to check - skipping")
        return 0

    root = Path(root or Path.cwd())
    exe = shutil.which("markdownlint-cli2") or node_tools.executable(
        "markdownlint-cli2"
    )
    if not exe:
        return missing_tool("markdownlint-cli2", mode)

    # A leading `:` marks a literal path, so glob characters are not expanded.
    args = [exe]
    if repo_config(root) is None:
        args += ["--config", str(DEFAULT_CONFIG)]
    args += [f":{_relative(f, root)}" for f in files]

    info(f"  markdownlint: linting {len(files)} markdown file(s)...")
    # cli2 writes findings to stderr today; both streams are read in case that
    # moves. Exit 1 is violations, so an exit 1 with none parsed is a parse miss.
    found = fdg.run_check(
        "markdownlint",
        args,
        mode,
        lambda result: parse(f"{result.stdout}\n{result.stderr}"),
        cwd=root,
    )
    if isinstance(found, int):
        return found

    fdg.report("markdownlint", found, mode, sarif_path=sarif_path)

    if not found:
        success(f"  markdownlint: {len(files)} file(s) clean")
        return 0
    if mode == "blocking":
        error(f"  markdownlint: {len(found)} violation(s) must be fixed")
        return 1
    warn(f"  markdownlint: {len(found)} violation(s) (non-blocking)")
    return 0


def _relative(path: Path, root: Path) -> str:
    """Return ``path`` as ``root``-relative POSIX, or as given when outside it."""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()
