# Project:   HyperI CI
# File:      src/hyperi_ci/quality/markdownlint.py
# Purpose:   Mechanical markdown syntax linting via markdownlint-cli2
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Markdown syntax linting via markdownlint-cli2.

Mechanical faults only - a list that does not render because it has no blank
line above it, a heading jumping two levels, trailing spaces that silently
become a line break. Nothing here has an opinion about prose.

Ships a default rule set (``config/markdownlint.yaml``) for a repo that
declares none, with MD013 off: the house style has no markdown width limit, so
a line-length rule would report every correctly written paragraph. A repo with
its own ``.markdownlint*`` config keeps it and the default is not passed at
all - one config, not a merge whose winner nobody can predict from the repo.

This is the ratchet's clearest case. An existing tree lights up on the first
run, so it starts at ``warn`` and is promoted per repo once its count reaches
zero; a greenfield repo can set ``blocking`` on day one and never accumulate.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from hyperi_ci.common import error, info, is_ci, run_cmd, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_cross_tool_mode
from hyperi_ci.quality import findings as fdg
from hyperi_ci.tools import missing_tool_notice

DEFAULT_CONFIG = Path(__file__).parent.parent / "config" / "markdownlint.yaml"

# Every config name markdownlint-cli2 discovers on its own. Finding one means
# the repo owns its rules and ours must stay out of the way.
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
    """Parse markdownlint-cli2's text output into normalised findings.

    cli2 has no JSON output without an extra formatter package, so the stable
    text line is what is read. Its banner and summary lines do not match the
    pattern and are dropped.
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
    """Lint ``files`` for markdown syntax faults. Returns exit code.

    0 = clean / advisory mode / disabled / no files; 1 = a blocking check found
    a violation, or markdownlint-cli2 is required-but-missing in CI.
    """
    mode = resolve_cross_tool_mode(config, "markdownlint", "warn")
    if mode == "disabled":
        info("  markdownlint: disabled")
        return 0
    if not files:
        info("  markdownlint: no markdown to check - skipping")
        return 0

    root = Path(root or Path.cwd())
    exe = shutil.which("markdownlint-cli2")
    if not exe:
        if mode == "blocking" and is_ci():
            error(missing_tool_notice("markdownlint-cli2"))
            return 1
        warn(missing_tool_notice("markdownlint-cli2"))
        return 0

    # A leading `:` marks a literal file path, so a filename holding a glob
    # character is linted rather than expanded.
    args = [exe]
    if repo_config(root) is None:
        args += ["--config", str(DEFAULT_CONFIG)]
    args += [f":{_relative(f, root)}" for f in files]

    info(f"  markdownlint: linting {len(files)} markdown file(s)...")
    try:
        result = run_cmd(args, check=False, capture=True, cwd=root)
    except OSError as exc:
        warn(f"  markdownlint-cli2 could not be run ({exc})")
        if mode == "blocking" and is_ci():
            error("  markdownlint: the check is blocking and could not run")
            return 1
        return 0

    # cli2 writes findings to stderr and progress to stdout; read both so a
    # future change of stream does not silently empty the report.
    found = parse(f"{result.stdout}\n{result.stderr}")

    # Exit 1 means violations, 2 means bad usage. A non-zero exit with nothing
    # parsed is the tool erroring, not a clean tree.
    if result.returncode not in (0, 1) and not found:
        warn(
            f"  markdownlint-cli2 exited {result.returncode} with no parseable "
            "output - tool error, not a clean pass"
        )
        if mode == "blocking" and is_ci():
            error("  markdownlint: the check is blocking and could not complete")
            return 1
        return 0

    dropped = fdg.surface("markdownlint", found, sarif_path=sarif_path)
    if dropped:
        info(f"  markdownlint: +{dropped} more finding(s) in the job summary")

    if not found:
        success(f"  markdownlint: {len(files)} file(s) clean")
        return 0
    if mode == "blocking":
        error(f"  markdownlint: {len(found)} violation(s) must be fixed")
        return 1
    warn(f"  markdownlint: {len(found)} violation(s) (non-blocking)")
    return 0


def _relative(path: Path, root: Path) -> str:
    """Path as ``root``-relative POSIX, falling back to absolute when outside it."""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()
