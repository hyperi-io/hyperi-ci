# Project:   HyperI CI
# File:      tests/unit/test_touched_files_action.py
# Purpose:   Tests for the predict-version changed-files helper
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for `.github/actions/predict-version/touched_files.py`.

Run as a real subprocess, because what is under test is the by-path package
load the composite performs where hyperi-ci is not installed.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / ".github" / "actions" / "predict-version" / "touched_files.py"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout.strip()


def _run(cwd: Path, event: Path, script: Path = SCRIPT) -> dict[str, str]:
    output = cwd.parent / "github-output"
    output.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.update(
        GITHUB_EVENT_NAME="push",
        GITHUB_EVENT_PATH=str(event),
        GITHUB_OUTPUT=str(output),
    )
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
        cwd=cwd,
    )
    assert result.returncode == 0, result.stderr
    written = {}
    for line in output.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        written[key] = value
    return written


def _pushed(tmp_path: Path, files: dict[str, str]) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.io")
    _git(repo, "config", "user.name", "t")
    _git(repo, "commit", "--allow-empty", "-q", "-m", "chore: seed")
    base = _git(repo, "rev-parse", "HEAD")
    for name, body in files.items():
        (repo / name).write_text(body, encoding="utf-8")
        _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", "chore: the change")
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps({"before": base, "after": _git(repo, "rev-parse", "HEAD")}),
        encoding="utf-8",
    )
    return repo, event


def test_a_chore_that_edits_code_opens_the_gate(tmp_path: Path) -> None:
    repo, event = _pushed(tmp_path, {"app.py": "x = 1\n"})
    written = _run(repo, event)
    assert written["code-changed"] == "true"
    assert "app.py" in written["touched"]


def test_docs_only_names_what_changed(tmp_path: Path) -> None:
    repo, event = _pushed(tmp_path, {"README.md": "x\n"})
    written = _run(repo, event)
    assert written == {
        "code-changed": "false",
        "touched": "only docs and repo metadata changed: README.md",
    }


def test_a_stranded_helper_opens_the_gate(tmp_path: Path) -> None:
    # A moved action directory leaves src/hyperi_ci unreachable. The gate
    # must then run the checks, never skip them.
    repo, event = _pushed(tmp_path, {"README.md": "x\n"})
    stranded = tmp_path / "x" / ".github" / "actions" / "predict-version"
    stranded.mkdir(parents=True)
    shutil.copy(SCRIPT, stranded / SCRIPT.name)

    written = _run(repo, event, stranded / SCRIPT.name)
    assert written["code-changed"] == "true"
