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
from dataclasses import replace
from pathlib import Path, PurePosixPath

import yaml

from hyperi_ci import jsonc
from hyperi_ci.common import info
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


def config_dir(root: Path) -> Path:
    """Return the nearest directory from ``root`` up to the cwd with a repo config.

    cli2 reads its config from the directory it runs in, so `lint-docs docs`
    from a repo root has to run from the root to see the root config.
    """
    top = Path.cwd().resolve()
    here = root.resolve()
    for candidate in (here, *here.parents):
        if repo_config(candidate) is not None:
            return candidate
        if candidate == top or top not in candidate.parents:
            break
    return root


def root_ignores(root: Path) -> list[str]:
    """Return the ``ignores`` globs of the root ``.markdownlint-cli2`` config.

    cli2 applies root ``ignores`` only while it expands globs, so a literal
    path skips them, and they are applied here instead. A nested config's
    ``ignores`` cli2 applies itself. A ``.cjs`` or ``.mjs`` config is code
    and is not read.
    """
    for name, load in (
        (".markdownlint-cli2.jsonc", jsonc.loads),
        (".markdownlint-cli2.yaml", yaml.safe_load),
    ):
        path = root / name
        if not path.is_file():
            continue
        try:
            raw = load(path.read_text(encoding="utf-8"))
        except (ValueError, yaml.YAMLError):
            return []
        ignores = raw.get("ignores") if isinstance(raw, dict) else None
        if not isinstance(ignores, list):
            return []
        return [g for g in ignores if isinstance(g, str)]
    return []


def _ignored(rel: str, globs: list[str]) -> bool:
    return any(PurePosixPath(rel).full_match(glob) for glob in globs)


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
    root = config_dir(Path(root or Path.cwd()))
    ignores = root_ignores(root)
    files = [f for f in files if not _ignored(fdg.relpath(f, root), ignores)]
    if not files:
        info("  markdownlint: no markdown to check - skipping")
        return 0

    exe = shutil.which("markdownlint-cli2") or node_tools.executable(
        "markdownlint-cli2"
    )
    if not exe:
        return missing_tool("markdownlint-cli2", mode)

    # A leading `:` marks a literal path, so glob characters are not expanded.
    args = [exe]
    if repo_config(root) is None:
        args += ["--config", str(DEFAULT_CONFIG)]
    args += [f":{fdg.relpath(f, root)}" for f in files]

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
    found = [replace(f, path=fdg.relpath(root / f.path)) for f in found]

    fdg.report("markdownlint", found, mode, sarif_path=sarif_path)

    return fdg.verdict("markdownlint", len(found), mode, f"{len(files)} file(s) clean")
