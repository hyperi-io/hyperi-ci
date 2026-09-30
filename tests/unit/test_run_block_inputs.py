# Project:   HyperI CI
# File:      tests/unit/test_run_block_inputs.py
# Purpose:   No workflow input or attacker-set value is pasted into a shell script
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""A ``${{ inputs.x }}`` inside ``run:`` is substituted before bash parses it.

A dispatch input such as ``bump`` then runs as shell: ``bump='${{ inputs.bump }}'``
closes its own quote on a value carrying ``'``. The same holds for anything a
contributor chooses -- a branch name, a PR title, a commit message. Every such
value reaches a step through ``env:`` and is read as ``"$VAR"``, where it stays
data.
"""

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

GITHUB_DIR = Path(__file__).resolve().parents[2] / ".github"

_UNSAFE = (
    r"(?:github\.event\.)?inputs\.",
    r"github\.head_ref\b",
    r"github\.event\.pull_request\.(?:title|body|head\.ref|head\.label)\b",
    r"github\.event\.(?:issue|comment|review|review_comment|discussion)\.",
    r"github\.event\.(?:head_commit|commits)\b",
    r"github\.event\.pages\b",
)
_UNSAFE_EXPRESSION = re.compile(r"\$\{\{[^}]*\b(?:" + "|".join(_UNSAFE) + ")")


def _pipeline_files() -> list[Path]:
    workflows = [*(GITHUB_DIR / "workflows").glob("*.yml")]
    workflows += (GITHUB_DIR / "workflows").glob("*.yaml")
    actions = [*(GITHUB_DIR / "actions").glob("*/action.yml")]
    actions += (GITHUB_DIR / "actions").glob("*/action.yaml")
    return sorted(workflows + actions)


def _steps(document: dict) -> Iterator[tuple[str, dict]]:
    for job_id, job in (document.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            yield job_id, step
    for step in (document.get("runs") or {}).get("steps") or []:
        yield "composite", step


def test_the_scan_reaches_the_release_path() -> None:
    names = {p.relative_to(GITHUB_DIR).as_posix() for p in _pipeline_files()}
    assert {
        "workflows/_release-tail.yml",
        "workflows/rust-ci.yml",
        "workflows/python-ci.yml",
        "workflows/go-ci.yml",
        "workflows/ts-ci.yml",
        "actions/predict-version/action.yml",
        "actions/setup-runtime/action.yml",
    } <= names


@pytest.mark.parametrize(
    "text",
    [
        "bump='${{ inputs.bump }}'",
        'tag="${{ github.event.inputs.tag }}"',
        "${{ inputs.tag || github.ref }}",
        'echo "${{ github.head_ref }}"',
        'echo "${{ github.event.pull_request.title }}"',
        "${{ github.event.head_commit.message }}",
        "${{ github.event.comment.body }}",
    ],
)
def test_the_pattern_catches(text: str) -> None:
    assert _UNSAFE_EXPRESSION.search(text)


@pytest.mark.parametrize(
    "text",
    [
        'bump="$BUMP"',
        "${{ github.ref }}",
        "${{ github.event_name }}",
        "${{ env.HYPERCI_INSTALL }}",
        "${{ github.event.pull_request.number }}",
    ],
)
def test_the_pattern_passes(text: str) -> None:
    assert not _UNSAFE_EXPRESSION.search(text)


@pytest.mark.parametrize(
    "path", _pipeline_files(), ids=lambda p: p.relative_to(GITHUB_DIR).as_posix()
)
def test_no_run_block_interpolates_an_unsafe_value(path: Path) -> None:
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    offenders = [
        f"{job_id}: {step.get('name') or step.get('id')}"
        for job_id, step in _steps(document)
        if _UNSAFE_EXPRESSION.search(str(step.get("run", "")))
    ]
    assert not offenders, (
        f"{path.name}: pass these values through `env:` and read them as "
        f'"$VAR": {offenders}'
    )
