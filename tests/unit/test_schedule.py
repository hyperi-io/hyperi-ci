# Project:   HyperI CI
# File:      tests/unit/test_schedule.py
# Purpose:   Tests for the nightly full-tier schedule scaffold
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from pathlib import Path

import pytest
import yaml

from hyperi_ci.config import shipped_default
from hyperi_ci.init import _render_workflow, init_project
from hyperi_ci.schedule import ScheduleResult, ensure_schedule, schedule_cron

WINDOW = shipped_default("test.full.schedule_window_utc")

HAND_WRITTEN = """\
# hand-written caller
name: CI

"on":
  push:
    branches: [main]
  # nightly kept out on purpose
  workflow_dispatch:

jobs:
  ci:
    uses: hyperi-io/hyperi-ci/.github/workflows/python-ci.yml@main
"""


def triggers(doc: dict) -> dict:
    """Return `on:`, which PyYAML reads as True when the key is unquoted."""
    return doc.get("on", doc.get(True))


def _fields(cron: str) -> tuple[int, int]:
    minute, hour, *rest = cron.split()
    assert rest == ["*", "*", "*"]
    return int(minute), int(hour)


class TestScheduleCron:
    def test_shipped_window_is_read_from_defaults(self) -> None:
        assert WINDOW == "14-18"

    def test_stable_for_a_name(self) -> None:
        assert schedule_cron("dfe-loader", WINDOW) == schedule_cron(
            "dfe-loader", WINDOW
        )

    def test_fleet_stays_in_window_off_the_half_hours(self) -> None:
        names = [f"repo-{i}" for i in range(400)]
        slots = {_fields(schedule_cron(n, "14-18")) for n in names}
        assert {h for _, h in slots} == {14, 15, 16, 17, 18}
        assert not {m for m, _ in slots} & {0, 30}
        assert len(slots) > 100

    def test_window_moves_the_hour(self) -> None:
        assert {_fields(schedule_cron(f"r{i}", "3-3"))[1] for i in range(50)} == {3}

    @pytest.mark.parametrize("bad", ["", "14", "18-14", "14-24", "a-b", "14:18"])
    def test_bad_window_fails(self, bad: str) -> None:
        with pytest.raises(ValueError, match="schedule_window_utc"):
            schedule_cron("repo", bad)


class TestEnsureSchedule:
    def test_adds_when_missing_and_keeps_the_rest(self, tmp_path: Path) -> None:
        path = tmp_path / "ci.yml"
        path.write_text(HAND_WRITTEN)
        assert ensure_schedule(path, "7 15 * * *") == ScheduleResult.ADDED
        text = path.read_text()
        doc = yaml.safe_load(text)
        assert triggers(doc)["schedule"] == [{"cron": "7 15 * * *"}]
        assert "# hand-written caller" in text
        assert "# nightly kept out on purpose" in text
        assert "branches: [main]" in text

    def test_leaves_an_existing_schedule_alone(self, tmp_path: Path) -> None:
        path = tmp_path / "ci.yml"
        existing = HAND_WRITTEN.replace(
            "  workflow_dispatch:\n", "  schedule:\n    - cron: '5 2 * * 1'\n"
        )
        path.write_text(existing)
        assert ensure_schedule(path, "7 15 * * *") == ScheduleResult.PRESENT
        assert path.read_text() == existing

    def test_second_run_changes_nothing(self, tmp_path: Path) -> None:
        path = tmp_path / "ci.yml"
        path.write_text(HAND_WRITTEN)
        ensure_schedule(path, "7 15 * * *")
        once = path.read_text()
        assert ensure_schedule(path, "9 16 * * *") == ScheduleResult.PRESENT
        assert path.read_text() == once

    @pytest.mark.parametrize(
        "text",
        ["on: [push]\njobs: {}\n", "on: push\n", "- not\n- a mapping\n", "a: [\n"],
    )
    def test_inline_or_unparseable_triggers_are_reported(
        self, tmp_path: Path, text: str
    ) -> None:
        path = tmp_path / "ci.yml"
        path.write_text(text)
        assert ensure_schedule(path, "7 15 * * *") == ScheduleResult.UNSUPPORTED
        assert path.read_text() == text


class TestInitScaffold:
    def test_render_includes_the_cron_when_given(self) -> None:
        doc = yaml.safe_load(_render_workflow("p", "python-ci.yml", cron="7 15 * * *"))
        assert triggers(doc)["schedule"] == [{"cron": "7 15 * * *"}]

    def test_init_writes_a_schedule_inside_the_window(self, tmp_path: Path) -> None:
        project = tmp_path / "my-service"
        project.mkdir()
        (project / "pyproject.toml").write_text("[project]\n")
        init_project(project, seed_tag=False)
        doc = yaml.safe_load((project / ".github/workflows/ci.yml").read_text())
        [entry] = triggers(doc)["schedule"]
        minute, hour = _fields(entry["cron"])
        assert 14 <= hour <= 18
        assert minute not in (0, 30)
        assert entry["cron"] == schedule_cron("my-service", WINDOW)

    def test_init_takes_the_window_from_project_config(self, tmp_path: Path) -> None:
        project = tmp_path / "my-service"
        project.mkdir()
        (project / "pyproject.toml").write_text("[project]\n")
        (project / ".hyperi-ci.yaml").write_text(
            "test:\n  full:\n    schedule_window_utc: '2-2'\n"
        )
        init_project(project, seed_tag=False)
        doc = yaml.safe_load((project / ".github/workflows/ci.yml").read_text())
        assert _fields(triggers(doc)["schedule"][0]["cron"])[1] == 2

    def test_schedule_only_adds_to_an_existing_caller(self, tmp_path: Path) -> None:
        workflow = tmp_path / ".github" / "workflows" / "ci.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text(HAND_WRITTEN)
        assert init_project(tmp_path, schedule_only=True) == 0
        assert "schedule" in triggers(yaml.safe_load(workflow.read_text()))
        assert not (tmp_path / ".hyperi-ci.yaml").exists()
        assert not (tmp_path / "Makefile").exists()

    def test_schedule_only_leaves_an_existing_schedule(self, tmp_path: Path) -> None:
        workflow = tmp_path / ".github" / "workflows" / "ci.yml"
        workflow.parent.mkdir(parents=True)
        existing = HAND_WRITTEN.replace(
            "  workflow_dispatch:\n", "  schedule:\n    - cron: '5 2 * * 1'\n"
        )
        workflow.write_text(existing)
        assert init_project(tmp_path, schedule_only=True) == 0
        assert workflow.read_text() == existing

    def test_schedule_only_without_a_caller_fails(self, tmp_path: Path) -> None:
        assert init_project(tmp_path, schedule_only=True) == 1
