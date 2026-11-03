# Project:   HyperI CI
# File:      tests/unit/test_check_lock.py
# Purpose:   The host-wide lock that serialises local checks
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

import fcntl
import os
import threading
from pathlib import Path

import pytest

from hyperi_ci import check_lock as module
from hyperi_ci.check_lock import check_lock


@pytest.fixture(autouse=True)
def _not_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "is_ci", lambda: False)
    # This suite runs inside a real `hyperi-ci check`, which sets it.
    monkeypatch.delenv(module.HELD_ENV, raising=False)


def _enter_in_thread(path: Path, entered: threading.Event) -> threading.Thread:
    def body() -> None:
        with check_lock(path):
            entered.set()

    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    return thread


def test_a_second_check_waits_for_the_first(tmp_path: Path) -> None:
    path = tmp_path / "check.lock"
    holder = path.open("a+", encoding="utf-8")
    fcntl.flock(holder, fcntl.LOCK_EX)
    entered = threading.Event()
    thread = _enter_in_thread(path, entered)
    assert not entered.wait(0.5)
    fcntl.flock(holder, fcntl.LOCK_UN)
    holder.close()
    assert entered.wait(5)
    thread.join(5)


def test_the_holder_is_named_in_the_file(tmp_path: Path) -> None:
    path = tmp_path / "check.lock"
    with check_lock(path):
        assert path.read_text(encoding="utf-8").startswith(f"pid {os.getpid()} ")


def test_a_check_inside_the_holder_does_not_wait_on_it(tmp_path: Path) -> None:
    path = tmp_path / "check.lock"
    with check_lock(path):
        assert os.environ[module.HELD_ENV] == "1"
        with check_lock(path):
            pass
    assert module.HELD_ENV not in os.environ


def test_ci_takes_no_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "is_ci", lambda: True)
    path = tmp_path / "check.lock"
    with check_lock(path):
        pass
    assert not path.exists()
