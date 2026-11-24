# Project:   HyperI CI
# File:      src/hyperi_ci/build_targets.py
# Purpose:   The Rust targets a project declares, read in the plan job
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""The Rust targets a project lists under ``build.rust.targets``.

The Rust Plan builds its matrix from these, so a project whose release build
does not fit the arm64 runner still releases amd64 (issue #127). An empty list
means every target.

Stdlib-only, importing nothing heavier than :mod:`hyperi_ci.project_config`,
because the predict-version composite loads it by path where hyperi-ci is not
installed.
"""

# KEEP on a 3.14 floor, where this import is otherwise wrong (issue #184).
# GitHub runs the composite's scripts before any install, on whatever python3
# the runner has, which may predate our floor.
# predict-version loads this file by path on the runner's python3, which may predate 3.14.
from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from hyperi_ci.project_config import read_project_config

#: Where the list lives in the project config.
TARGETS_KEY = "build.rust.targets"

#: What an empty list builds in CI.
DEFAULT_CI_TRIPLES = ("x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu")

#: Every target CI builds, as (os, arch). The pair names the shipped binary.
TARGET_OS_ARCH = {
    "x86_64-unknown-linux-gnu": ("linux", "amd64"),
    "aarch64-unknown-linux-gnu": ("linux", "arm64"),
    "x86_64-pc-windows-msvc": ("windows", "amd64"),
    "x86_64-apple-darwin": ("darwin", "amd64"),
    "aarch64-apple-darwin": ("darwin", "arm64"),
}

#: The GitHub-hosted runner for each target outside Linux. The runner mode
#: does not choose these, and only Windows takes a self-hosted override.
HOSTED_RUNNERS = {
    "x86_64-pc-windows-msvc": "windows-2025",
    "x86_64-apple-darwin": "macos-26-intel",
    "aarch64-apple-darwin": "macos-26",
}

WINDOWS_TARGET = "x86_64-pc-windows-msvc"

# GitHub-hosted Windows and macOS labels, billed per minute on a private repo.
_HOSTED_LABEL = re.compile(r"^(windows|macos)-(latest|\d+)")


def os_legs(targets: Sequence[str], windows_runner: str = "") -> list[dict[str, str]]:
    """Return one build leg per listed target outside Linux, in listed order.

    Args:
        targets: The declared ``build.rust.targets``.
        windows_runner: Runner label for the Windows leg, such as a self-hosted
            one. Empty keeps the GitHub-hosted default.

    Returns:
        Matrix entries carrying ``target``, ``runner`` and ``os_arch``.

    """
    legs: list[dict[str, str]] = []
    for target in dict.fromkeys(targets):
        runner = HOSTED_RUNNERS.get(target)
        if runner and target == WINDOWS_TARGET and windows_runner:
            runner = windows_runner
        if runner:
            os_name, arch = TARGET_OS_ARCH[target]
            legs.append(
                {"target": target, "runner": runner, "os_arch": f"{os_name}-{arch}"}
            )
    return legs


def os_test_runners(
    legs: Sequence[dict[str, str]], *, private: bool, event: str, ref: str
) -> list[str]:
    """Return the runners the Windows and macOS Test legs use on this run.

    On a private repo a GitHub-hosted leg skips pull requests and branch
    pushes, which bill per commit, and runs on main, the merge queue,
    schedules and dispatches. A self-hosted leg always runs.

    Args:
        legs: The OS build legs.
        private: Whether the repository is private.
        event: ``github.event_name``.
        ref: ``github.ref``.

    Returns:
        Each runner once, in leg order.

    """
    per_commit = event == "pull_request" or (
        event == "push" and ref != "refs/heads/main"
    )
    runners = dict.fromkeys(leg["runner"] for leg in legs)
    if private and per_commit:
        return [r for r in runners if not _HOSTED_LABEL.match(r)]
    return list(runners)


def unbuilt_targets(targets: Sequence[str]) -> list[str]:
    """Return the listed targets CI has no leg for."""
    return [target for target in targets if target not in TARGET_OS_ARCH]


def declared_targets(config: dict[str, Any]) -> list[str]:
    """Return ``build.rust.targets`` from a parsed config.

    Args:
        config: The parsed project config.

    Returns:
        The listed targets, empty when none are listed.

    Raises:
        ValueError: If the key holds something other than a list of strings.

    """
    build = config.get("build")
    rust = build.get("rust") if isinstance(build, dict) else None
    targets = rust.get("targets") if isinstance(rust, dict) else None
    if targets is None:
        return []
    if not isinstance(targets, list) or not all(
        isinstance(target, str) for target in targets
    ):
        raise ValueError(
            f"{TARGETS_KEY} must be a list of target triples, not {targets!r}"
        )
    return [target.strip() for target in targets if target.strip()]


def ci_targets(config: dict[str, Any]) -> tuple[str, ...]:
    """Return the targets CI builds: the declared list, else the default pair.

    An unreadable list falls back to the default pair too, and the Plan job
    reports it.
    """
    try:
        return tuple(declared_targets(config)) or DEFAULT_CI_TRIPLES
    except ValueError:
        return DEFAULT_CI_TRIPLES


def read_rust_targets(root: Path) -> tuple[list[str], str]:
    """Read the targets the project lists.

    Args:
        root: The checkout root.

    Returns:
        The targets (empty meaning every target), and why the list could not
        be read, else empty. An unreadable list returns no targets.

    """
    project = read_project_config(root)
    if project.data is None:
        return [], project.unreadable
    try:
        return declared_targets(project.data), ""
    except ValueError as exc:
        return [], f"{project.name}: {exc}"
