# Project:   HyperI CI
# File:      tests/unit/test_pytest_plugin.py
# Purpose:   Tests for the pytest plugin that sizes `-n auto`
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for :mod:`hyperi_ci.pytest_plugin`."""

import os
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

from hyperi_ci import pytest_plugin, sizing

_MEM = "test:\n  python:\n    worker_mem_mb: {}\n"


@pytest.mark.parametrize(
    ("yaml_text", "env", "budget", "expected_mb", "expected"),
    [
        pytest.param(None, {}, 3, 1024, 3, id="shipped_default"),
        pytest.param(_MEM.format(4096), {}, 3, 4096, 3, id="project"),
        pytest.param(_MEM.format(0), {}, 3, 1024, 3, id="zero"),
        pytest.param(_MEM.format("lots"), {}, 3, 1024, 3, id="junk"),
        pytest.param(
            _MEM.format(4096),
            {"HYPERCI_TEST_PYTHON_WORKER_MEM_MB": "2048"},
            3,
            2048,
            3,
            id="env-memory-beats-file",
        ),
        pytest.param(None, {}, 40, 1024, 16, id="capped-at-16"),
        pytest.param("a: [\n", {}, 3, 1024, 3, id="unparseable-file"),
        pytest.param("- a\n- list\n", {}, 3, 1024, 3, id="list-shaped-file"),
        pytest.param(None, {"HYPERCI_TEST_WORKERS": "5"}, 3, None, 5, id="forced"),
        pytest.param(None, {"HYPERCI_TEST_WORKERS": "0"}, 3, None, 0, id="forced-0"),
        pytest.param(
            None, {"HYPERCI_TEST_WORKERS": "many"}, 3, 1024, 3, id="forced-junk"
        ),
        pytest.param(
            "test:\n  python:\n    parallel: 6\n", {}, 3, None, 6, id="parallel-int"
        ),
        pytest.param(
            "test:\n  python:\n    parallel: true\n", {}, 3, 1024, 3, id="parallel-true"
        ),
    ],
)
def test_auto_workers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    yaml_text: str | None,
    env: dict[str, str],
    budget: int,
    expected_mb: int | None,
    expected: int,
) -> None:
    for name in ("HYPERCI_TEST_WORKERS", "HYPERCI_TEST_PYTHON_WORKER_MEM_MB"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    if yaml_text is not None:
        (tmp_path / ".hyperi-ci.yaml").write_text(
            yaml_text, encoding="utf-8", newline="\n"
        )
    seen: list[int] = []

    def _budget(mb: int) -> sizing.WorkerBudget:
        seen.append(mb)
        return sizing.WorkerBudget(budget, budget, None, None, mb)

    monkeypatch.setattr(pytest_plugin, "worker_budget", _budget)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert pytest_plugin.auto_workers(tmp_path) == expected
    assert seen == ([] if expected_mb is None else [expected_mb])


def test_an_unreadable_file_warns(tmp_path: Path) -> None:
    (tmp_path / ".hyperi-ci.yaml").write_text("a: [\n", encoding="utf-8")
    with pytest.warns(UserWarning, match="cannot read"):
        pytest_plugin.auto_workers(tmp_path)


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


_PROBE = """
import os
import types


def pytest_configure(config):
    pm = config.pluginmanager
    if os.environ.get("PROBE_XDIST_LAST"):
        # A stand-in for xdist's own plain impl, registered after ours: pluggy
        # calls it first unless ours is tryfirst.
        late = types.ModuleType("xdist.plugin")
        late.pytest_xdist_auto_num_workers = lambda config: 99
        pm.register(late, "late-xdist")
    from hyperi_ci.pytest_plugin import auto_workers

    answer = pm.hook.pytest_xdist_auto_num_workers(config=config)
    print("ANSWER", answer, "PLUGIN", auto_workers(config.rootpath))
"""

_PROJECT_IMPL = """

def pytest_xdist_auto_num_workers(config):
    return 7
"""


@pytest.mark.parametrize(
    ("xdist_last", "project_impl", "xdist_env", "answer"),
    [
        # None: the plugin's own count, read in the same process.
        pytest.param(False, False, "", None, id="plugin-answers"),
        pytest.param(True, False, "", None, id="plugin-answers-when-xdist-loads-last"),
        # xdist is an entry point, so it always registers before any conftest.
        pytest.param(False, True, "", "7", id="project-conftest-wins"),
        pytest.param(False, False, "3", "3", id="xdist-env-wins"),
    ],
)
def test_the_plugin_answers_whatever_the_load_order(
    tmp_path: Path,
    xdist_last: bool,
    project_impl: bool,
    xdist_env: str,
    answer: str | None,
) -> None:
    conftest = _PROBE + (_PROJECT_IMPL if project_impl else "")
    (tmp_path / "conftest.py").write_text(conftest, encoding="utf-8", newline="\n")
    (tmp_path / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8", newline="\n"
    )
    dropped = ("PROBE_XDIST_LAST", "PYTEST_XDIST_AUTO_NUM_WORKERS")
    env = {k: v for k, v in os.environ.items() if k not in dropped}
    if xdist_last:
        env["PROBE_XDIST_LAST"] = "1"
    if xdist_env:
        env["PYTEST_XDIST_AUTO_NUM_WORKERS"] = xdist_env
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-s", "-p", "no:cacheprovider"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    line = next(ln for ln in result.stdout.splitlines() if ln.startswith("ANSWER"))
    _, got, _, plugin = line.split()
    # 99 is the stand-in for xdist's own answer.
    assert got != "99", line
    assert got == (answer or plugin), line


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
