# Project:   HyperI CI
# File:      src/hyperi_ci/pytest_plugin.py
# Purpose:   Size `pytest -n auto` the way hyperi-ci sizes its own test run
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""pytest plugin: ``-n auto`` takes hyperi-ci's CPU and memory bounded count.

Loaded through the ``pytest11`` entry point wherever hyperi-ci is installed,
so it reads ``.hyperi-ci.yaml`` itself rather than through ``load_config``,
which can import scalo. A project's own ``pytest_xdist_auto_num_workers``,
``PYTEST_XDIST_AUTO_NUM_WORKERS`` and ``-n logical`` all leave the answer to
xdist, and an explicit ``-n 4`` never asks.
"""

import os
import warnings
from pathlib import Path

import pytest
import yaml

from hyperi_ci.config import shipped_default
from hyperi_ci.project_config import CONFIG_FILES
from hyperi_ci.sizing import (
    MAX_AUTO_WORKERS,
    WORKERS_ENV,
    parse_workers,
    usable_mb,
    worker_budget,
)

_WORKER_MEM_KEY = "test.python.worker_mem_mb"
_WORKER_MEM_ENV = "HYPERCI_TEST_PYTHON_WORKER_MEM_MB"
_XDIST_ENV = "PYTEST_XDIST_AUTO_NUM_WORKERS"


def _env_count(name: str) -> int | None:
    """Return the variable ``name`` as a count, None when unset or not a number."""
    raw = os.environ.get(name, "").strip()
    return parse_workers(raw) if raw else None


def _python_test_settings(project_dir: Path) -> dict:
    """Return the project's ``test.python`` mapping, empty when unreadable."""
    for name in CONFIG_FILES:
        path = project_dir / name
        if not path.is_file():
            continue
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, yaml.YAMLError) as exc:
            warnings.warn(
                f"hyperi-ci: cannot read {path} ({exc}), sizing -n auto from defaults"
            )
            return {}
        test = doc.get("test") if isinstance(doc, dict) else None
        python = test.get("python") if isinstance(test, dict) else None
        return python if isinstance(python, dict) else {}
    return {}


def auto_workers(project_dir: Path) -> int:
    """Return the worker count for ``-n auto`` in ``project_dir``.

    ``HYPERCI_TEST_WORKERS`` wins, then an integer ``test.python.parallel``,
    then the CPU and memory budget capped at :data:`MAX_AUTO_WORKERS`.

    Args:
        project_dir: The pytest rootdir, where .hyperi-ci.yaml is read from.

    Returns:
        The count, 0 for serial.

    """
    forced = _env_count(WORKERS_ENV)
    if forced is not None:
        return forced
    settings = _python_test_settings(project_dir)
    parallel = settings.get("parallel")
    if isinstance(parallel, int) and not isinstance(parallel, bool) and parallel > 0:
        return parallel
    mb = usable_mb(_env_count(_WORKER_MEM_ENV)) or usable_mb(
        settings.get("worker_mem_mb")
    )
    if mb is None:
        mb = int(shipped_default(_WORKER_MEM_KEY))
    return min(worker_budget(mb).workers, MAX_AUTO_WORKERS)


def _another_answer(config: pytest.Config) -> bool:
    """Return True when an impl other than this one and xdist's own is registered."""
    hook = config.pluginmanager.hook.pytest_xdist_auto_num_workers
    return any(
        impl.function is not pytest_xdist_auto_num_workers
        and getattr(impl.plugin, "__name__", "") != "xdist.plugin"
        for impl in hook.get_hookimpls()
    )


# Optional: without pytest-xdist the hook has no spec, and pytest refuses it.
# tryfirst: the hook is firstresult, so without it entry-point load order
# decides whether xdist answers first.
@pytest.hookimpl(optionalhook=True, tryfirst=True)
def pytest_xdist_auto_num_workers(config: pytest.Config) -> int | None:
    """Answer xdist's ``-n auto`` with the CPU and memory bounded count.

    Returns:
        The count, or None to let a project's impl or xdist answer instead.

    """
    if os.environ.get(_XDIST_ENV, "").strip():
        return None
    if getattr(config.option, "numprocesses", None) == "logical":
        return None
    if _another_answer(config):
        return None
    return auto_workers(Path(config.rootpath))
