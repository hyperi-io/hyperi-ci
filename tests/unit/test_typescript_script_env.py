# Project:   HyperI CI
# File:      tests/unit/test_typescript_script_env.py
# Purpose:   TS package scripts run with turbo's output streamed
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""A TS package script must stream turbo's output, or a cancelled run logs nothing.

turbo groups each task's output until the task ends when it detects CI, so a
cancelled or hung test task printed only turbo's header (issue #265). The
handlers run the project's own script, so the setting has to reach the CHILD
process -- these tests read it back from a real child, not from our own code.
"""

import json
import os
import stat
from pathlib import Path

from hyperi_ci.config import CIConfig
from hyperi_ci.languages.typescript import build as ts_build
from hyperi_ci.languages.typescript import test as ts_test
from hyperi_ci.languages.typescript._common import package_script_env

_RECORDER = '#!/bin/sh\nprintf "%s" "${TURBO_LOG_ORDER-unset}" > "$PWD/seen"\n'


def _npm_project(tmp_path: Path, monkeypatch) -> Path:
    """An npm project whose `npm` records the log order its child process sees."""
    (tmp_path / "package.json").write_text(json.dumps({"name": "t"}))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    npm = bin_dir / "npm"
    npm.write_text(_RECORDER)
    npm.chmod(npm.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_a_test_run_streams_turbo_output(tmp_path, monkeypatch):
    monkeypatch.delenv("TURBO_LOG_ORDER", raising=False)
    root = _npm_project(tmp_path, monkeypatch)

    rc = ts_test.run(CIConfig(_raw={"test": {"coverage": False}}))

    assert rc == 0
    assert (root / "seen").read_text() == "stream"


def test_a_build_run_streams_turbo_output(tmp_path, monkeypatch):
    monkeypatch.delenv("TURBO_LOG_ORDER", raising=False)
    root = _npm_project(tmp_path, monkeypatch)

    rc = ts_build.run(CIConfig())

    assert rc == 0
    assert (root / "seen").read_text() == "stream"


def test_a_project_that_chose_grouped_output_keeps_it(tmp_path, monkeypatch):
    monkeypatch.setenv("TURBO_LOG_ORDER", "grouped")
    root = _npm_project(tmp_path, monkeypatch)

    rc = ts_test.run(CIConfig(_raw={"test": {"coverage": False}}))

    assert rc == 0
    assert (root / "seen").read_text() == "grouped"


def test_the_overlay_adds_nothing_the_project_already_set(monkeypatch):
    monkeypatch.setenv("TURBO_LOG_ORDER", "grouped")
    assert package_script_env() == {}

    monkeypatch.delenv("TURBO_LOG_ORDER")
    assert package_script_env() == {"TURBO_LOG_ORDER": "stream"}
