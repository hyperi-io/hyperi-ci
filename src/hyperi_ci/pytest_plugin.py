# Project:   HyperI CI
# File:      src/hyperi_ci/pytest_plugin.py
# Purpose:   Size `pytest -n auto` the way hyperi-ci sizes its own test run
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""pytest plugin: ``-n auto`` takes hyperi-ci's CPU and memory bounded count.

Loaded through the ``pytest11`` entry point wherever hyperi-ci is installed,
so it imports nothing that sets up scalo's logger. A project's own
``pytest_xdist_auto_num_workers`` in conftest.py still wins, and an explicit
``-n 4`` never asks.
"""

from pathlib import Path

import pytest

from hyperi_ci.config import load_config, shipped_default
from hyperi_ci.sizing import usable_mb, worker_budget

_WORKER_MEM_KEY = "test.python.worker_mem_mb"


def auto_workers(project_dir: Path) -> int:
    """Return the worker count for ``-n auto`` in ``project_dir``.

    Args:
        project_dir: The pytest rootdir, where .hyperi-ci.yaml is read from.

    Returns:
        At least 1.

    """
    config = load_config(project_dir=project_dir, report_removed=False)
    mb = usable_mb(config.setting(_WORKER_MEM_KEY))
    if mb is None:
        mb = int(shipped_default(_WORKER_MEM_KEY))
    return worker_budget(mb).workers


def _another_answer(config: pytest.Config) -> bool:
    """Return True when an impl other than this one and xdist's own is registered."""
    hook = config.pluginmanager.hook.pytest_xdist_auto_num_workers
    return any(
        impl.function is not pytest_xdist_auto_num_workers
        and getattr(impl.plugin, "__name__", "") != "xdist.plugin"
        for impl in hook.get_hookimpls()
    )


# Optional: without pytest-xdist the hook has no spec, and pytest refuses it.
# tryfirst: the hook is firstresult, and entry-point load order alone decided
# whether xdist's raw CPU count answered before this.
@pytest.hookimpl(optionalhook=True, tryfirst=True)
def pytest_xdist_auto_num_workers(config: pytest.Config) -> int | None:
    """Answer xdist's ``-n auto`` with the CPU and memory bounded count.

    Returns:
        The count, or None to let a project's own impl answer instead.

    """
    if _another_answer(config):
        return None
    return auto_workers(Path(config.rootpath))
