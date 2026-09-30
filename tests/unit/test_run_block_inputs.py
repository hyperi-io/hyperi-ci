# Project:   HyperI CI
# File:      tests/unit/test_run_block_inputs.py
# Purpose:   No workflow input is ever pasted into a shell script
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""A ``${{ inputs.x }}`` inside ``run:`` is substituted before bash parses it.

A dispatch input such as ``bump`` then runs as shell: ``bump='${{ inputs.bump }}'``
closes its own quote on a value carrying ``'``. Every input reaches a step
through ``env:`` and is read as ``"$VAR"``, where it stays data.
"""

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

GITHUB_DIR = Path(__file__).resolve().parents[2] / ".github"

_INPUT_EXPRESSION = re.compile(r"\$\{\{[^}]*\b(?:github\.event\.)?inputs\.")


# The release path: dispatch inputs reach every one of these.
_RELEASE_PATH = (
    "workflows/_release-tail.yml",
    "workflows/rust-ci.yml",
    "workflows/python-ci.yml",
    "workflows/go-ci.yml",
    "workflows/ts-ci.yml",
    "actions/predict-version/action.yml",
)


def _pipeline_files() -> list[Path]:
    return [GITHUB_DIR / name for name in _RELEASE_PATH]


def _steps(document: dict) -> Iterator[tuple[str, dict]]:
    for job_id, job in (document.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            yield job_id, step
    for step in (document.get("runs") or {}).get("steps") or []:
        yield "composite", step


def test_the_scan_reads_real_files() -> None:
    for path in _pipeline_files():
        assert path.is_file(), path


def test_the_pattern_catches_both_spellings() -> None:
    assert _INPUT_EXPRESSION.search("bump='${{ inputs.bump }}'")
    assert _INPUT_EXPRESSION.search('tag="${{ github.event.inputs.tag }}"')
    assert _INPUT_EXPRESSION.search("${{ inputs.tag || github.ref }}")
    assert not _INPUT_EXPRESSION.search('bump="$BUMP"')


@pytest.mark.parametrize(
    "path", _pipeline_files(), ids=lambda p: p.relative_to(GITHUB_DIR).as_posix()
)
def test_no_run_block_interpolates_an_input(path: Path) -> None:
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    offenders = [
        f"{job_id}: {step.get('name') or step.get('id')}"
        for job_id, step in _steps(document)
        if _INPUT_EXPRESSION.search(str(step.get("run", "")))
    ]
    assert not offenders, (
        f"{path.name}: pass these inputs through `env:` and read them as "
        f'"$VAR": {offenders}'
    )
