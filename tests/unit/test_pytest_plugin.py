# Project:   HyperI CI
# File:      tests/unit/test_pytest_plugin.py
# Purpose:   Tests for the pytest plugin that sizes `-n auto`
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for :mod:`hyperi_ci.pytest_plugin`."""

import subprocess
import sys
from pathlib import Path

import pytest

from hyperi_ci import pytest_plugin, sizing


@pytest.mark.parametrize(
    ("yaml_text", "expected_mb"),
    [
        pytest.param(None, 1024, id="shipped_default"),
        pytest.param("test:\n  python:\n    worker_mem_mb: 4096\n", 4096, id="project"),
        pytest.param("test:\n  python:\n    worker_mem_mb: 0\n", 1024, id="zero"),
        pytest.param("test:\n  python:\n    worker_mem_mb: lots\n", 1024, id="junk"),
    ],
)
def test_auto_workers_reads_the_project_memory_figure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    yaml_text: str | None,
    expected_mb: int,
) -> None:
    if yaml_text is not None:
        (tmp_path / ".hyperi-ci.yaml").write_text(
            yaml_text, encoding="utf-8", newline="\n"
        )
    seen: list[int] = []

    def _budget(mb: int) -> sizing.WorkerBudget:
        seen.append(mb)
        return sizing.WorkerBudget(3, 3, None, None, mb)

    monkeypatch.setattr(pytest_plugin, "worker_budget", _budget)
    assert pytest_plugin.auto_workers(tmp_path) == 3
    assert seen == [expected_mb]


@pytest.mark.parametrize("xdist", ["-pxdist", "-pno:xdist"], ids=["with", "without"])
def test_pytest_starts_with_the_plugin_loaded(tmp_path: Path, xdist: str) -> None:
    (tmp_path / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8", newline="\n"
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", xdist, "-q", "-p", "no:cacheprovider"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_importing_the_plugin_loads_no_scalo() -> None:
    probe = (
        "import sys, hyperi_ci.pytest_plugin; "
        "print(sorted(m for m in sys.modules if m.split('.')[0] == 'scalo'))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    assert result.stdout.strip() == "[]"
