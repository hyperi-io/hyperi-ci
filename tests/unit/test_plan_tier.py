# Project:   HyperI CI
# File:      tests/unit/test_plan_tier.py
# Purpose:   Which test tier a run owes, and the release opt-in
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""The plan-side half of test tiering.

Nothing lowers a tier: a scheduled run, a release in a repo that set
``test.full.required_for_release``, and a repo whose own ``test.tier`` is full
are full whatever a caller passes, because a caller forwarding
``inputs.test-tier || 'core'`` on every event would otherwise pin them to core.
A release in a repo that did not opt in runs core, which is what every release
ran before tiers existed.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hyperi_ci import plan_tier
from hyperi_ci.config import packaged_default
from hyperi_ci.plan_tier import (
    CORE,
    FULL,
    REQUIRED_FOR_RELEASE_KEY,
    TIER_KEY,
    ProjectTier,
    owed_tier,
    read_project_tier,
    resolve_tier,
)

HELPER = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "actions"
    / "predict-version"
    / "resolve_tier.py"
)

_OPT_IN = "test:\n  full:\n    required_for_release: true\n"
_UNSET = ProjectTier(CORE, False, "")


def _tier(
    event: str,
    *,
    will_release: bool = False,
    requested: str = "",
    project: ProjectTier = _UNSET,
) -> tuple[str, str]:
    return resolve_tier(
        event_name=event,
        will_release=will_release,
        requested=requested,
        project=project,
    )


class TestResolveTier:
    @pytest.mark.parametrize("event", ["pull_request", "push"])
    def test_a_pr_or_push_runs_core(self, event: str) -> None:
        assert _tier(event)[0] == CORE

    def test_a_schedule_runs_full(self) -> None:
        assert _tier("schedule")[0] == FULL

    def test_a_schedule_asking_for_core_still_runs_full(self) -> None:
        assert _tier("schedule", requested="core")[0] == FULL

    @pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
    def test_an_opted_in_release_runs_full_whatever_was_asked(self, event: str) -> None:
        tier, why = _tier(
            event,
            will_release=True,
            requested="core",
            project=ProjectTier(CORE, True, ""),
        )
        assert tier == FULL
        assert "is on" in why

    def test_a_release_without_the_opt_in_runs_core_and_says_why(self) -> None:
        tier, why = _tier("push", will_release=True)
        assert tier == CORE
        assert REQUIRED_FOR_RELEASE_KEY in why
        assert "is off" in why

    def test_a_release_asking_for_full_runs_full_without_the_opt_in(self) -> None:
        tier, _ = _tier("workflow_dispatch", will_release=True, requested="full")
        assert tier == FULL

    def test_the_opt_in_alone_does_not_raise_a_pr(self) -> None:
        assert _tier("pull_request", project=ProjectTier(CORE, True, ""))[0] == CORE

    def test_a_project_tier_of_full_is_a_floor(self) -> None:
        tier, why = _tier(
            "pull_request", requested="core", project=ProjectTier(FULL, False, "")
        )
        assert tier == FULL
        assert TIER_KEY in why

    def test_a_dispatch_asking_for_full_runs_full(self) -> None:
        assert _tier("workflow_dispatch", requested="full")[0] == FULL

    def test_a_bare_dispatch_runs_core(self) -> None:
        assert _tier("workflow_dispatch", requested="core")[0] == CORE

    def test_the_input_is_case_and_space_tolerant(self) -> None:
        assert _tier("push", requested=" FULL ")[0] == FULL

    @pytest.mark.parametrize("requested", ["ful", "nightly", "smoke"])
    def test_an_unknown_tier_is_refused(self, requested: str) -> None:
        with pytest.raises(ValueError, match="test-tier"):
            _tier("push", requested=requested)


class TestOwedTier:
    """What a crash falls back to: the event's own tier, never a blanket full."""

    @pytest.mark.parametrize(
        ("event", "will_release", "full_required", "expected"),
        [
            ("pull_request", False, False, CORE),
            ("push", False, True, CORE),
            ("workflow_dispatch", False, False, CORE),
            ("push", True, False, CORE),
            ("push", True, True, FULL),
            ("schedule", False, False, FULL),
        ],
    )
    def test_each_event(
        self, event: str, will_release: bool, full_required: bool, expected: str
    ) -> None:
        assert (
            owed_tier(
                event_name=event,
                will_release=will_release,
                full_required=full_required,
            )
            == expected
        )


class TestReadProjectTier:
    def _config(self, root: Path, text: str, name: str = ".hyperi-ci.yaml") -> Path:
        (root / name).write_text(text, encoding="utf-8")
        return root

    def test_unset_with_no_config(self, tmp_path: Path) -> None:
        assert read_project_tier(tmp_path) == _UNSET

    def test_unset_when_the_keys_are_absent(self, tmp_path: Path) -> None:
        root = self._config(tmp_path, "language: python\n")
        assert read_project_tier(root) == _UNSET

    def test_the_opt_in_is_read(self, tmp_path: Path) -> None:
        assert read_project_tier(self._config(tmp_path, _OPT_IN)).full_required

    @pytest.mark.parametrize(
        "name", [".hyperi-ci.yml", ".hypersec-ci.yaml", ".hypersec-ci.yml"]
    )
    def test_every_config_spelling_is_read(self, tmp_path: Path, name: str) -> None:
        # An opted-in repo on a legacy spelling must not fail open.
        root = self._config(tmp_path, _OPT_IN + "  tier: full\n", name)
        assert read_project_tier(root) == ProjectTier(FULL, True, "")

    def test_the_first_spelling_wins_as_in_load_config(self, tmp_path: Path) -> None:
        self._config(tmp_path, "test:\n  tier: core\n")
        self._config(tmp_path, "test:\n  tier: full\n", ".hyperi-ci.yml")
        assert read_project_tier(tmp_path).tier == CORE

    def test_a_quoted_true_counts(self, tmp_path: Path) -> None:
        root = self._config(
            tmp_path, 'test:\n  full:\n    required_for_release: "yes"\n'
        )
        assert read_project_tier(root).full_required

    def test_off_when_set_false(self, tmp_path: Path) -> None:
        root = self._config(
            tmp_path, "test:\n  full:\n    required_for_release: false\n"
        )
        assert not read_project_tier(root).full_required

    def test_the_project_tier_is_read(self, tmp_path: Path) -> None:
        root = self._config(tmp_path, "test:\n  tier: Full\n")
        assert read_project_tier(root).tier == FULL

    def test_an_unknown_project_tier_is_refused(self, tmp_path: Path) -> None:
        root = self._config(tmp_path, "test:\n  tier: nightly\n")
        with pytest.raises(ValueError, match=TIER_KEY):
            read_project_tier(root)

    def test_an_unreadable_config_reads_as_unset_and_names_the_file(
        self, tmp_path: Path
    ) -> None:
        root = self._config(tmp_path, "test: [unclosed\n", ".hyperi-ci.yml")
        assert read_project_tier(root) == ProjectTier(CORE, False, ".hyperi-ci.yml")

    def test_the_shipped_defaults_match_what_the_code_assumes(self) -> None:
        # The plan job cannot read defaults.yaml, so an unset key must mean
        # what the packaged default says.
        assert packaged_default(REQUIRED_FOR_RELEASE_KEY, False) is False
        assert packaged_default(TIER_KEY, CORE) == CORE


def _run_helper(
    tmp_path: Path, *, event: str, will_release: str, requested: str
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    output = tmp_path / "github_output"
    output.touch()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "GITHUB_OUTPUT": str(output),
        "GITHUB_WORKSPACE": str(tmp_path),
        "TIER_EVENT": event,
        "TIER_WILL_RELEASE": will_release,
        "TIER_REQUESTED": requested,
    }
    result = subprocess.run(
        [sys.executable, str(HELPER)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )
    return result, _outputs(output)


def _outputs(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )


class TestTheActionHelper:
    """The composite runs the helper by path, with no hyperi-ci install."""

    def test_a_schedule_writes_full_with_one_line(self, tmp_path: Path) -> None:
        result, outputs = _run_helper(
            tmp_path, event="schedule", will_release="false", requested=""
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert outputs == {"test-tier": "full", "full-required-for-release": "false"}
        assert result.stdout.splitlines() == [
            "::notice title=test tier full::scheduled run"
        ]

    def test_a_pr_writes_core(self, tmp_path: Path) -> None:
        _, outputs = _run_helper(
            tmp_path, event="pull_request", will_release="false", requested="core"
        )
        assert outputs["test-tier"] == "core"

    def test_a_release_without_the_opt_in_writes_core(self, tmp_path: Path) -> None:
        _, outputs = _run_helper(
            tmp_path, event="push", will_release="true", requested="core"
        )
        assert outputs == {"test-tier": "core", "full-required-for-release": "false"}

    def test_the_opt_in_makes_a_release_full(self, tmp_path: Path) -> None:
        (tmp_path / ".hyperi-ci.yml").write_text(_OPT_IN, encoding="utf-8")
        _, outputs = _run_helper(
            tmp_path, event="push", will_release="true", requested="core"
        )
        assert outputs == {"test-tier": "full", "full-required-for-release": "true"}

    def test_an_unreadable_config_warns_in_one_line(self, tmp_path: Path) -> None:
        (tmp_path / ".hyperi-ci.yaml").write_text("test: [unclosed\n", encoding="utf-8")
        result, outputs = _run_helper(
            tmp_path, event="push", will_release="true", requested=""
        )
        assert outputs["full-required-for-release"] == "false"
        lines = result.stdout.splitlines()
        assert len(lines) == 1
        assert lines[0].startswith("::warning title=test tier core::")

    def test_an_unknown_tier_fails_the_step(self, tmp_path: Path) -> None:
        result, outputs = _run_helper(
            tmp_path, event="workflow_dispatch", will_release="false", requested="ful"
        )
        assert result.returncode == 1
        assert "::error" in result.stdout
        assert outputs == {}


class TestTheHelperCrashPath:
    """A crash writes the event's owed tier and warns, and never fails Plan."""

    def _crash(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        event: str,
        will_release: str,
        opted_in: bool = False,
    ) -> tuple[int, dict[str, str]]:
        if opted_in:
            (tmp_path / ".hyperi-ci.yaml").write_text(_OPT_IN, encoding="utf-8")

        def boom(**_: object) -> tuple[str, str]:
            raise RuntimeError("resolver exploded")

        output = tmp_path / "github_output"
        output.touch()
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))
        monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
        monkeypatch.setenv("TIER_EVENT", event)
        monkeypatch.setenv("TIER_WILL_RELEASE", will_release)
        monkeypatch.setenv("TIER_REQUESTED", "")
        monkeypatch.setattr(plan_tier, "resolve_tier", boom)

        # Loaded without registering it, so the real hyperi_ci package stays in
        # sys.modules and the helper reuses it rather than installing its stub.
        spec = importlib.util.spec_from_file_location("resolve_tier_helper", HELPER)
        assert spec is not None and spec.loader is not None
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        return helper.run(), _outputs(output)

    @pytest.mark.parametrize(
        ("event", "will_release", "opted_in", "expected"),
        [
            ("pull_request", "false", False, "core"),
            ("push", "true", False, "core"),
            ("schedule", "false", False, "full"),
            ("push", "true", True, "full"),
        ],
    )
    def test_a_crash_falls_back_to_the_events_tier(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        event: str,
        will_release: str,
        opted_in: bool,
        expected: str,
    ) -> None:
        rc, outputs = self._crash(
            tmp_path,
            monkeypatch,
            event=event,
            will_release=will_release,
            opted_in=opted_in,
        )
        assert rc == 0
        assert outputs["test-tier"] == expected
        assert "resolver exploded" in capsys.readouterr().out
