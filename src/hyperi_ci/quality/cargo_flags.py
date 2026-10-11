# Project:   HyperI CI
# File:      src/hyperi_ci/quality/cargo_flags.py
# Purpose:   Catch a Rust repo whose own rustflags never reach the compiler
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Catch a Rust repo whose ``.cargo/config.toml`` rustflags silently never apply.

Two causes, both of which cost the fleet AVX2 (issue #178):

* An inert negation: ``.cargo/`` then ``!.cargo/config.toml`` in
  ``.gitignore``. Git cannot re-include a file under an excluded directory, so
  the config is never committed. ``.cargo/*`` then the negation works.
* Flags under ``[build]``. Cargo takes the first of ``CARGO_ENCODED_RUSTFLAGS``,
  ``RUSTFLAGS``, the joined ``target.*`` entries, then ``build.rustflags``. The
  ARC pod's ``CARGO_TARGET_<TRIPLE>_RUSTFLAGS`` is a ``target`` entry, so it
  replaces ``[build]`` outright.

Flags under ``[target.<triple>]`` join the pod's and are not flagged. Nor is a
``[build]`` block when every triple CI builds has its own ``[target]`` rustflags,
since ``[build]`` can then only apply on a host CI never builds for.
"""

import subprocess
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from hyperi_ci.build_targets import declared_targets
from hyperi_ci.common import info, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_tool_mode
from hyperi_ci.quality import findings as fdg

_TOOL = "cargo-flags"
CARGO_CONFIG = Path(".cargo/config.toml")

#: The triples CI builds when ``build.rust.targets`` lists none.
DEFAULT_CI_TRIPLES = ("x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu")


def _negations_under_excluded_dirs(gitignore: str) -> list[tuple[int, str, str]]:
    """Return ``(line, directory pattern, negated path)`` for inert negations.

    A negation is inert when an earlier line excludes the directory it sits in.
    """
    excluded_dirs: list[str] = []
    out: list[tuple[int, str, str]] = []
    for number, raw in enumerate(gitignore.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("!"):
            negated = line[1:].lstrip("/")
            for directory in excluded_dirs:
                if negated.startswith(directory):
                    out.append((number, directory, negated))
                    break
            continue
        directory = _excluded_directory(line)
        if directory:
            excluded_dirs.append(directory)
    return out


def _excluded_directory(line: str) -> str | None:
    """Return the directory prefix a pattern excludes, or None.

    ``.cargo/``, ``.cargo`` and ``**/.cargo/`` exclude the directory. ``dir/*``
    excludes only its contents, which a negation can re-include.
    """
    pattern = line.removeprefix("**/").lstrip("/")
    if not pattern or pattern.endswith("*"):
        return None
    if "*" in pattern.rstrip("/"):
        return None
    return pattern if pattern.endswith("/") else f"{pattern}/"


def _is_tracked(path: str, project_root: Path) -> bool:
    """Return whether git tracks ``path``, which no ignore rule then affects."""
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", path],
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return result.returncode == 0


def _has_rustflags(table: object) -> bool:
    """Whether a parsed cargo config table sets ``rustflags``."""
    return isinstance(table, dict) and bool(table.get("rustflags"))


def _uncovered_triples(parsed: dict[str, Any], triples: Sequence[str]) -> list[str]:
    """Return the triples with no ``[target.<triple>]`` rustflags of their own."""
    targets = parsed.get("target")
    tables = targets if isinstance(targets, dict) else {}
    return [t for t in triples if not _has_rustflags(tables.get(t))]


def scan(
    project_root: Path, *, ci_triples: Sequence[str] = DEFAULT_CI_TRIPLES
) -> list[fdg.Finding]:
    """Return a finding for each way this repo's rustflags could go missing.

    ``ci_triples`` are the targets CI builds. A ``[build]`` block is a local
    fallback, not a finding, when each of them has its own target rustflags.
    """
    out: list[fdg.Finding] = []

    gitignore = project_root / ".gitignore"
    if gitignore.is_file():
        text = gitignore.read_text(encoding="utf-8", errors="replace")
        for number, directory, negated in _negations_under_excluded_dirs(text):
            if _is_tracked(negated, project_root):
                continue
            out.append(
                fdg.Finding(
                    tool=_TOOL,
                    path=".gitignore",
                    line=number,
                    level="warning",
                    rule="cargo/inert-gitignore-negation",
                    message=(
                        f"`!{negated}` cannot re-include a file under the "
                        f"excluded directory `{directory}`, and {negated} is "
                        f"untracked -- so CI never sees it. Exclude the "
                        f"contents instead: `{directory}*`."
                    ),
                )
            )

    cargo_config = project_root / CARGO_CONFIG
    if cargo_config.is_file():
        text = cargo_config.read_text(encoding="utf-8", errors="replace")
        try:
            parsed = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            parsed = {}
        uncovered = _uncovered_triples(parsed, ci_triples)
        if _has_rustflags(parsed.get("build")) and not uncovered:
            info(
                f"  {_TOOL}: `[build]` rustflags are a local fallback -- every CI "
                f"triple ({', '.join(ci_triples)}) has its own `[target]` rustflags"
            )
        elif _has_rustflags(parsed.get("build")):
            out.append(
                fdg.Finding(
                    tool=_TOOL,
                    path=str(CARGO_CONFIG),
                    line=None,
                    level="warning",
                    rule="cargo/rustflags-under-build",
                    message=(
                        "rustflags under `[build]` are DISCARDED, not merged, "
                        "whenever a target entry exists -- and the ARC pool "
                        "sets one via CARGO_TARGET_<TRIPLE>_RUSTFLAGS. Every "
                        "flag here is lost on those builds with nothing in the "
                        "log. Move them under `[target.<triple>]`, which joins "
                        "instead of replacing. No target rustflags for: "
                        f"{', '.join(uncovered)}."
                    ),
                )
            )

    return out


def run(
    config: CIConfig,
    *,
    project_root: Path | None = None,
    sarif_path: str | Path | None = None,
) -> int:
    """Report rustflags this repo declares but cannot ship.

    Args:
        config: Merged CI configuration.
        project_root: Repo root; defaults to the working directory.
        sarif_path: Where to write SARIF, when the caller collects it.

    Returns:
        0 unless a blocking mode found something.

    """
    mode = resolve_tool_mode("cargo_flags", config, default="warn")
    if mode == "disabled":
        info(f"  {_TOOL}: disabled")
        return 0

    root = project_root or Path.cwd()
    try:
        triples = declared_targets(config._raw) or DEFAULT_CI_TRIPLES
    except ValueError:
        triples = DEFAULT_CI_TRIPLES
    found = scan(root, ci_triples=triples)

    fdg.report(_TOOL, found, mode, sarif_path=sarif_path)

    if not found:
        success(f"  {_TOOL}: this repo's rustflags reach the compiler")
        return 0
    if mode == "blocking":
        return 1
    warn(f"  {_TOOL}: {len(found)} finding(s) -- advisory")
    return 0
