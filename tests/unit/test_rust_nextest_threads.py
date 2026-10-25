# Project:   HyperI CI
# File:      tests/unit/test_rust_nextest_threads.py
# Purpose:   Tests for the NEXTEST_TEST_THREADS cap and what beats it
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for :mod:`hyperi_ci.languages.rust._threads`."""

from pathlib import Path

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.languages.rust import _threads
from hyperi_ci.workers import WORKERS_ENV, WorkerBudget


@pytest.fixture(autouse=True)
def _pinned_host(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Pin the computed budget and record the per-thread MiB it was asked for."""
    asked: list[int] = []

    def _budget(mb: int) -> WorkerBudget:
        asked.append(mb)
        return WorkerBudget(5, 5, None, None, mb)

    monkeypatch.setattr(_threads, "worker_budget", _budget)
    monkeypatch.delenv(WORKERS_ENV, raising=False)
    return asked


def _nextest_config(root: Path, body: str) -> None:
    path = root / ".config" / "nextest.toml"
    path.parent.mkdir(parents=True)
    path.write_text(body, encoding="utf-8", newline="\n")


class TestNextestThreadsEnv:
    def test_a_bare_project_gets_the_computed_count(
        self, tmp_path: Path, _pinned_host: list[int]
    ) -> None:
        env = _threads.nextest_threads_env(CIConfig(_raw={}), {}, cwd=tmp_path)
        assert env == {_threads.THREADS_ENV: "5"}
        assert _pinned_host == [2048]

    def test_the_rust_thread_memory_key_is_used(
        self, tmp_path: Path, _pinned_host: list[int]
    ) -> None:
        config = CIConfig(_raw={"test": {"rust": {"thread_mem_mb": 4096}}})
        _threads.nextest_threads_env(config, {}, cwd=tmp_path)
        assert _pinned_host == [4096]

    def test_nextest_env_in_the_environment_wins(self, tmp_path: Path) -> None:
        environ = {_threads.THREADS_ENV: "3"}
        assert (
            _threads.nextest_threads_env(CIConfig(_raw={}), environ, cwd=tmp_path) == {}
        )

    def test_hyperci_test_workers_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(WORKERS_ENV, "9")
        env = _threads.nextest_threads_env(CIConfig(_raw={}), {}, cwd=tmp_path)
        assert env == {_threads.THREADS_ENV: "9"}

    def test_hyperci_test_workers_zero_is_one_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(WORKERS_ENV, "0")
        env = _threads.nextest_threads_env(CIConfig(_raw={}), {}, cwd=tmp_path)
        assert env == {_threads.THREADS_ENV: "1"}

    def test_a_nextest_config_test_threads_wins(self, tmp_path: Path) -> None:
        _nextest_config(tmp_path, "[profile.default]\ntest-threads = 2\n")
        assert _threads.nextest_threads_env(CIConfig(_raw={}), {}, cwd=tmp_path) == {}

    def test_a_ci_profile_test_threads_counts(self, tmp_path: Path) -> None:
        _nextest_config(tmp_path, '[profile.ci]\ntest-threads = "num-cpus"\n')
        assert _threads.config_sets_threads(tmp_path)

    def test_a_nextest_config_without_threads_claims_nothing(
        self, tmp_path: Path
    ) -> None:
        _nextest_config(tmp_path, "[profile.default]\nretries = 2\n")
        assert not _threads.config_sets_threads(tmp_path)

    def test_a_broken_nextest_config_claims_nothing(self, tmp_path: Path) -> None:
        _nextest_config(tmp_path, "[profile.default\n")
        assert not _threads.config_sets_threads(tmp_path)


class TestCappedNextestThreads:
    def test_the_setting_is_removed_on_exit(self, tmp_path: Path) -> None:
        environ: dict[str, str] = {}
        with _threads.capped_nextest_threads(CIConfig(_raw={}), environ, cwd=tmp_path):
            assert environ == {_threads.THREADS_ENV: "5"}
        assert environ == {}

    def test_an_existing_setting_survives(self, tmp_path: Path) -> None:
        environ = {_threads.THREADS_ENV: "2"}
        with _threads.capped_nextest_threads(CIConfig(_raw={}), environ, cwd=tmp_path):
            pass
        assert environ == {_threads.THREADS_ENV: "2"}
