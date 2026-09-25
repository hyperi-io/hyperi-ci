# Project:   HyperI CI
# File:      tests/unit/test_typescript_test_tier.py
# Purpose:   TypeScript package script chosen per test tier
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.languages.tiering import SuiteTier
from hyperi_ci.languages.typescript import test as ts_test

MODULE = "hyperi_ci.languages.typescript.test"


class _Calls:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.notices: list[tuple[SuiteTier, str]] = []
        self.logged: list[str] = []

    def run_cmd(self, cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess:
        self.commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    def announce(self, tier: SuiteTier, detail: str) -> None:
        self.notices.append((tier, detail))


@pytest.fixture
def calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Calls:
    monkeypatch.chdir(tmp_path)
    rec = _Calls()
    monkeypatch.setattr(f"{MODULE}.detect_package_manager", lambda: "npm")
    monkeypatch.setattr(f"{MODULE}.ensure_pm_available", lambda _pm: True)
    monkeypatch.setattr(f"{MODULE}.run_cmd", rec.run_cmd)
    monkeypatch.setattr(f"{MODULE}.announce_tier", rec.announce)
    monkeypatch.setattr(f"{MODULE}.info", rec.logged.append)
    return rec


def _package(scripts: dict[str, str]) -> None:
    Path("package.json").write_text(
        json.dumps(
            {"name": "t", "devDependencies": {"vitest": "5"}, "scripts": scripts}
        ),
        encoding="utf-8",
    )


_CONFIG = CIConfig(_raw={})


class TestScriptSelection:
    def test_core_without_a_tier_script_is_unchanged(self, calls: _Calls) -> None:
        _package({"test": "vitest run", "test:full": "vitest run && playwright test"})
        assert ts_test.run(_CONFIG, extra_env={"TEST_TIER": "core"}) == 0
        assert calls.commands == [["npm", "run", "test", "--", "--coverage"]]

    def test_full_runs_test_full_when_defined(self, calls: _Calls) -> None:
        _package({"test": "vitest run", "test:full": "vitest run && playwright test"})
        assert ts_test.run(_CONFIG, extra_env={"TEST_TIER": "full"}) == 0
        assert calls.commands == [["npm", "run", "test:full"]]

    def test_full_falls_back_to_test_when_undefined(self, calls: _Calls) -> None:
        _package({"test": "vitest run"})
        assert ts_test.run(_CONFIG, extra_env={"TEST_TIER": "full"}) == 0
        assert calls.commands == [["npm", "run", "test", "--", "--coverage"]]

    def test_core_script_is_used_when_defined(self, calls: _Calls) -> None:
        _package({"test": "vitest run", "test:core": "vitest run --project unit"})
        assert ts_test.run(_CONFIG, extra_env={"TEST_TIER": "core"}) == 0
        assert calls.commands == [["npm", "run", "test:core"]]

    def test_no_package_json_runs_test(self, calls: _Calls) -> None:
        assert ts_test.run(_CONFIG, extra_env={"TEST_TIER": "full"}) == 0
        assert calls.commands[0][:3] == ["npm", "run", "test"]


class TestTheChoiceIsSaid:
    def test_log_names_the_script_and_why(self, calls: _Calls) -> None:
        _package({"test": "vitest run"})
        ts_test.run(_CONFIG, extra_env={"TEST_TIER": "full"})
        assert "  Test script: test (package.json has no test:full)" in calls.logged

    def test_notice_names_the_full_script(self, calls: _Calls) -> None:
        _package({"test": "vitest run", "test:full": "vitest run"})
        ts_test.run(_CONFIG, extra_env={"TEST_TIER": "full"})
        assert calls.notices == [(SuiteTier.FULL, "ran package script test:full")]

    def test_core_has_no_notice(self, calls: _Calls) -> None:
        """It would say only that `test` ran, which every core run does."""
        _package({"test": "vitest run"})
        ts_test.run(_CONFIG, extra_env={"TEST_TIER": "core"})
        assert calls.notices == []

    def test_full_without_test_full_warns_that_it_ran_core(
        self,
        calls: _Calls,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        _package({"test": "vitest run"})
        ts_test.run(_CONFIG, extra_env={"TEST_TIER": "full"})
        out = capsys.readouterr().out
        assert "::warning title=test tier full::" in out
        assert "package.json has no test:full" in out
        assert calls.notices == []

    def test_full_with_test_full_does_not_warn(
        self,
        calls: _Calls,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        _package({"test": "vitest run", "test:full": "vitest run"})
        ts_test.run(_CONFIG, extra_env={"TEST_TIER": "full"})
        assert "::warning" not in capsys.readouterr().out
