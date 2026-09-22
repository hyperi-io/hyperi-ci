# Project:   HyperI CI
# File:      src/hyperi_ci/quality/cargo_flags.py
# Purpose:   Catch a Rust repo whose own rustflags never reach the compiler
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Catch a Rust repo that cannot ship its own rustflags.

Two ways a repo carries a correct-looking ``.cargo/config.toml`` and builds
without it. Both are silent, both cost the target-cpu the project asked for,
and both have already cost the fleet AVX2 (issue #178).

THE INERT NEGATION. A ``.gitignore`` excluding ``.cargo/`` and then writing
``!.cargo/config.toml`` does nothing -- git cannot re-include a file underneath
an excluded DIRECTORY. The config is never committed, CI never sees it, and the
repo builds baseline while the developer's machine builds v3. The working form
globs the CONTENTS instead: ``.cargo/*`` then the negation.

THE WRONG TABLE. Cargo's four sources of extra flags are mutually exclusive and
checked in order: ``CARGO_ENCODED_RUSTFLAGS``, ``RUSTFLAGS``, all matching
``target.<triple>``/``target.<cfg>`` entries joined, then ``build.rustflags``.
So a ``target.*`` entry does not merge with ``[build]`` -- it REPLACES it. The
ARC pod sets ``CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS``, which counts
as such an entry, so a repo declaring flags under ``[build]`` loses all of them
on every x86_64 build there with nothing in the log.

Flags under ``[target.<triple>]`` are SAFE and must not be flagged: matching
target entries join, so the pod's flags and the repo's coexist. That
distinction is what keeps this check free of false positives.
"""

import subprocess
import tomllib
from pathlib import Path

from hyperi_ci.common import info, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_cross_tool_mode
from hyperi_ci.quality import findings as fdg

_TOOL = "cargo-flags"
CARGO_CONFIG = Path(".cargo/config.toml")


def _negations_under_excluded_dirs(gitignore: str) -> list[tuple[int, str, str]]:
    """Return ``(line, directory pattern, negated path)`` for inert negations.

    A negation is inert when an earlier line excludes the DIRECTORY the negated
    path sits in. Git offers no way to re-include from under one, so the
    negation reads as intent and does nothing.
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

    Git excludes a directory by a trailing slash (``.cargo/``), by a bare name
    (``.cargo``), and through a globstar (``**/.cargo/``) -- all three make a
    negation underneath inert. ``dir/*`` globs the CONTENTS instead, which is
    the form a negation CAN escape, so it is not one.
    """
    pattern = line.removeprefix("**/").lstrip("/")
    if not pattern or pattern.endswith("*"):
        return None
    if "*" in pattern.rstrip("/"):
        return None
    return pattern if pattern.endswith("/") else f"{pattern}/"


def _is_tracked(path: str, project_root: Path) -> bool:
    """Whether git tracks ``path``. A tracked file ignores every ignore rule."""
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


def _build_table_flags(config_text: str) -> bool:
    """Whether the cargo config declares rustflags under ``[build]``."""
    try:
        parsed = tomllib.loads(config_text)
    except tomllib.TOMLDecodeError:
        return False
    build = parsed.get("build")
    return isinstance(build, dict) and bool(build.get("rustflags"))


def scan(project_root: Path) -> list[fdg.Finding]:
    """Return a finding for each way this repo's rustflags could go missing."""
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
        if _build_table_flags(text):
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
                        "instead of replacing."
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
    """Report rustflags this repo declares but cannot ship. Returns exit code.

    Args:
        config: Merged CI configuration.
        project_root: Repo root; defaults to the working directory.
        sarif_path: Where to write SARIF, when the caller collects it.

    Returns:
        0 unless a blocking mode found something.

    """
    mode = resolve_cross_tool_mode(config, "cargo_flags", "warn")
    if mode == "disabled":
        info(f"  {_TOOL}: disabled")
        return 0

    root = project_root or Path.cwd()
    found = scan(root)

    dropped = fdg.surface(_TOOL, found, sarif_path=sarif_path)
    if dropped:
        info(f"  {_TOOL}: +{dropped} more finding(s) in the job summary")

    if not found:
        success(f"  {_TOOL}: this repo's rustflags reach the compiler")
        return 0
    if mode == "blocking":
        return 1
    warn(f"  {_TOOL}: {len(found)} finding(s) -- advisory")
    return 0
