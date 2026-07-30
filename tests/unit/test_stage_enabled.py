# Project:   HyperI CI
# File:      tests/unit/test_stage_enabled.py
# Purpose:   Every stage's `enabled` key is honoured, and the no-tests escape
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

"""The stage `enabled` switches, and `test.fail_on_missing`.

`test.enabled` was declared in defaults.yaml and named in dispatch's own error
guidance as the escape hatch for a project with no tests, while nothing read it
-- so a project could not opt out of a stage that `quality` and `build` both
allow opting out of. These lock all three in.
"""

from __future__ import annotations

from unittest.mock import patch

from hyperi_ci.config import CIConfig
from hyperi_ci.dispatch import stage_build, stage_quality, stage_test
from hyperi_ci.languages.python.test import _absolve_empty_run


def _config(**raw: object) -> CIConfig:
    return CIConfig(_raw=raw)


class TestStageEnabledSwitches:
    """Each stage can be turned off from .hyperi-ci.yaml."""

    def test_test_stage_honours_its_key(self) -> None:
        with patch("hyperi_ci.dispatch._dispatch_to_handler") as handler:
            assert stage_test("python", _config(test={"enabled": False})) == 0
        handler.assert_not_called()

    def test_test_stage_runs_when_unset(self) -> None:
        with patch(
            "hyperi_ci.dispatch._dispatch_to_handler", return_value=0
        ) as handler:
            assert stage_test("python", _config()) == 0
        handler.assert_called_once()

    def test_test_stage_runs_when_explicitly_enabled(self) -> None:
        with patch(
            "hyperi_ci.dispatch._dispatch_to_handler", return_value=0
        ) as handler:
            assert stage_test("python", _config(test={"enabled": True})) == 0
        handler.assert_called_once()

    def test_build_stage_honours_its_key(self) -> None:
        with patch("hyperi_ci.dispatch._dispatch_to_handler") as handler:
            assert stage_build("python", _config(build={"enabled": False})) == 0
        handler.assert_not_called()

    def test_quality_stage_honours_its_key(self) -> None:
        with patch("hyperi_ci.dispatch._dispatch_to_handler") as handler:
            with patch("hyperi_ci.dispatch.deprecated_files.scan"):
                with patch("hyperi_ci.dispatch.repo_advisor.run"):
                    assert (
                        stage_quality("python", _config(quality={"enabled": False}))
                        == 0
                    )
        handler.assert_not_called()

    def test_a_disabled_test_stage_does_not_mask_a_missing_handler(self) -> None:
        """Disabling tests is an opt-out, so the packaging-bug check is skipped."""
        with patch("hyperi_ci.dispatch._dispatch_to_handler", return_value=-1):
            assert stage_test("python", _config(test={"enabled": False})) == 0


class TestFailOnMissing:
    """pytest exit 5 means "collected nothing", which is not a failure."""

    def test_an_empty_run_passes_by_default(self) -> None:
        assert _absolve_empty_run(5, _config()) == 0

    def test_an_empty_run_fails_when_configured_to(self) -> None:
        assert _absolve_empty_run(5, _config(test={"fail_on_missing": True})) == 5

    def test_a_real_failure_is_never_absolved(self) -> None:
        """Only 5 is remapped -- a genuine test failure keeps its exit code."""
        assert _absolve_empty_run(1, _config()) == 1
        assert _absolve_empty_run(1, _config(test={"fail_on_missing": True})) == 1

    def test_success_passes_through(self) -> None:
        assert _absolve_empty_run(0, _config()) == 0

    def test_other_exit_codes_pass_through(self) -> None:
        for rc in (2, 3, 4, 130):
            assert _absolve_empty_run(rc, _config()) == rc
