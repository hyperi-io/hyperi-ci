#!/usr/bin/env python3
# Project:   HyperI CI
# File:      scripts/fixture_fleet.py
# Purpose:   Read config/fixtures.yaml and answer which fixtures a change needs
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""The fixture fleet, read from its SSoT and turned into answers (issue #215).

`config/fixtures.yaml` lists the fleet. Three things read it and none of them
should parse YAML of their own: the org consistency check, the PR rehearsal
gate, and the full-fleet sweep.

The question the gate asks is "which fixtures does THIS diff have to be proven
against". Workflows and composite actions reach every consumer the instant they
merge, so a change to one is live before anybody has run it anywhere. The
mapping from a changed path to a fixture is data in the SSoT (`workflow`,
`rehearsal`), not a table in code, so adding a fixture does not mean editing a
selector.
"""

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
FLEET_PATH = ROOT / "config" / "fixtures.yaml"
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
ORG = "hyperi-io"

_WORKFLOW_PREFIX = ".github/workflows/"
_ACTION_PREFIX = ".github/actions/"
_ACTION_REF = re.compile(r"\.github/actions/([A-Za-z0-9._-]+)")

type Entry = dict[str, Any]


def load_fleet(path: Path | None = None) -> list[Entry]:
    """Fleet entries in declaration order.

    Args:
        path: Override for the SSoT location. Tests pass a temporary file.

    Returns:
        The `fleet` list from the YAML.
    """
    data = yaml.safe_load((path or FLEET_PATH).read_text(encoding="utf-8"))
    return list(data.get("fleet", []))


def declared_names(fleet: list[Entry]) -> set[str]:
    """Upstream repo names the fleet claims exist."""
    return {entry["name"] for entry in fleet}


def token_scope(fleet: list[Entry], *, negative_cases_only: bool = False) -> list[str]:
    """The repos a fleet workflow's app token should reach, in fleet order.

    The token action widens an EMPTY list to every repo in the org, so callers
    must treat an empty answer as an error, never pass it on.
    """
    return [
        entry["name"]
        for entry in fleet
        if not negative_cases_only or entry.get("negative_cases")
    ]


def language_workflows(fleet: list[Entry]) -> set[str]:
    """Reusable workflow filenames the fleet routes through."""
    return {entry["workflow"] for entry in fleet if entry.get("workflow")}


def rehearsal_targets(fleet: list[Entry]) -> dict[str, Entry]:
    """Workflow filename -> the fixture a change to it is rehearsed against."""
    return {
        entry["workflow"]: entry
        for entry in fleet
        if entry.get("rehearsal") in ("canary", "default")
    }


def canary(fleet: list[Entry]) -> Entry | None:
    """The fixture a shared-surface change is rehearsed against."""
    for entry in fleet:
        if entry.get("rehearsal") == "canary":
            return entry
    return None


def read_workflow_texts(directory: Path | None = None) -> dict[str, str]:
    """Every workflow file in the repo, keyed by filename."""
    source = directory or WORKFLOWS_DIR
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(source.glob("*.yml"))
    }


def actions_in(text: str) -> set[str]:
    """Composite-action names a workflow refers to, local or cross-repo."""
    return set(_ACTION_REF.findall(text))


def workflows_for_action(
    action: str, texts: dict[str, str], language_files: set[str]
) -> set[str]:
    """Language workflows that call ``action`` directly.

    Shared workflows are deliberately not consulted. `_release-tail.yml` calls
    setup-rust-tools from its Rust publish path, and counting that would widen
    a Rust-only change to the whole fleet.

    Args:
        action: Composite-action directory name.
        texts: Workflow filename -> content.
        language_files: The filenames the fleet routes through.

    Returns:
        The subset of ``language_files`` referring to ``action``.
    """
    return {
        name
        for name in language_files
        if name in texts and action in actions_in(texts[name])
    }


def select_for_paths(
    paths: list[str], fleet: list[Entry], texts: dict[str, str]
) -> list[Entry]:
    """Fixtures a diff must be rehearsed against, in name order.

    A language workflow selects its own target. A shared workflow, and an
    action every language workflow calls, select the canary: the per-language
    difference a PR run can reach is thin, and rehearsing five fixtures on
    every shared change costs more than it proves.

    Args:
        paths: Repo-relative paths changed by the diff.
        fleet: Fleet entries.
        texts: Workflow filename -> content.

    Returns:
        The fixtures to rehearse. Empty means the diff touched no consumer
        surface, which is a pass, not a skip.
    """
    language_files = language_workflows(fleet)
    targets = rehearsal_targets(fleet)
    selected: set[str] = set()
    shared_touched = False

    for raw in paths:
        path = raw.replace("\\", "/").removeprefix("./")
        if path.startswith(_WORKFLOW_PREFIX):
            name = path[len(_WORKFLOW_PREFIX) :]
            if name in language_files:
                selected.add(name)
            elif name.startswith("_"):
                shared_touched = True
        elif path.startswith(_ACTION_PREFIX):
            action = path[len(_ACTION_PREFIX) :].split("/", 1)[0]
            callers = workflows_for_action(action, texts, language_files)
            if not callers or callers == language_files:
                shared_touched = True
            else:
                selected |= callers

    chosen = {
        targets[name]["name"]: targets[name] for name in selected if name in targets
    }
    if shared_touched and (head := canary(fleet)) is not None:
        chosen[head["name"]] = head
    return [chosen[name] for name in sorted(chosen)]


def masks(fleet: list[Entry]) -> list[tuple[str, Entry]]:
    """Every declared mask as (fixture name, mask), in name order."""
    found: list[tuple[str, Entry]] = []
    for entry in sorted(fleet, key=lambda e: e["name"]):
        for mask in entry.get("masks") or []:
            found.append((entry["name"], mask))
    return found


def mask_problems(fleet: list[Entry]) -> list[str]:
    """Masks that do not carry what makes them removable.

    A mask without an issue reference is indistinguishable from a fixture
    somebody configured wrong, and nothing will ever take it out again.
    """
    problems: list[str] = []
    for name, mask in masks(fleet):
        if not mask.get("feature"):
            problems.append(f"{name}: a mask with no `feature`")
        if not mask.get("why"):
            problems.append(f"{name}: mask {mask.get('feature')!r} has no `why`")
        issue = mask.get("issue")
        if not isinstance(issue, int) or issue <= 0:
            problems.append(
                f"{name}: mask {mask.get('feature')!r} needs an `issue` number, "
                f"got {issue!r}"
            )
    return problems


def mask_lines(fleet: list[Entry]) -> list[str]:
    """One printable line per declared mask."""
    return [
        f"  {name}: {mask.get('feature')} off - {mask.get('why')} "
        f"(hyperi-io/hyperi-ci#{mask.get('issue')})"
        for name, mask in masks(fleet)
    ]


def main() -> int:
    """Print `repos=<a,b,c>` for a workflow step to append to $GITHUB_OUTPUT."""
    parser = argparse.ArgumentParser(description="Scope a fleet app token")
    parser.add_argument(
        "--negative-cases",
        action="store_true",
        help="only the fixtures that carry .ci-negative/ cases",
    )
    args = parser.parse_args()
    repos = token_scope(load_fleet(), negative_cases_only=args.negative_cases)
    if not repos:
        print(
            "ERROR: no fixtures matched - refusing an org-wide token", file=sys.stderr
        )
        return 1
    print(f"repos={','.join(repos)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
