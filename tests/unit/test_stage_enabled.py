# Project:   HyperI CI
# File:      tests/unit/test_stage_enabled.py
# Purpose:   Every stage's `enabled` key is honoured, and the no-tests escape
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

"""The stage `enabled` switches, and `test.fail_on_missing`.

`test.enabled` was declared in defaults.yaml and named in dispatch's own error
guidance as the escape hatch for a project with no tests, while nothing read it
-- so a project could not opt out of a stage that `quality` and `build` both
allow opting out of. These lock all three in.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from hyperi_ci import config as config_module
from hyperi_ci.config import CIConfig, load_config
from hyperi_ci.dispatch import stage_build, stage_quality, stage_test
from hyperi_ci.languages import quality_common
from hyperi_ci.languages.python.test import _absolve_empty_run

_REASON = "stage rebuilt under #12; gitleaks runs in the org-wide secret scan"
_OWED = "without saying why"


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


class TestDisablingQualityOwesAReason:
    """`quality.enabled: false` turns off every security gate at once.

    One security gate turned below its shipped default has to say why. The
    switch that drops all of them said so in a single info line, so a repo
    could skip the reason by turning the whole stage off (issue #270).
    """

    @pytest.fixture(autouse=True)
    def _isolated(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # The deprecated-file scan reads cwd and runs before the switch is read.
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(quality_common, "is_ci", lambda: False)

    @staticmethod
    def _warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
        said: list[str] = []
        monkeypatch.setattr(quality_common, "warn", said.append)
        return said

    @staticmethod
    def _annotations(capsys: pytest.CaptureFixture[str]) -> list[str]:
        out = capsys.readouterr().out
        return [
            line
            for line in out.splitlines()
            if line.startswith("::")
            and not line.startswith(("::group::", "::endgroup::"))
        ]

    @staticmethod
    def _off(language: str = "python", **quality: object) -> int:
        return stage_quality(language, _config(quality={"enabled": False, **quality}))

    def test_a_missing_reason_is_named_with_a_fix_to_paste(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said = self._warnings(monkeypatch)
        # Stage 1 of issue #259 warns and never fails the stage.
        assert self._off() == 0
        message = "\n".join(said)
        assert "quality.enabled" in message
        assert _OWED in message
        assert "enabled: false" in message
        assert "reason:" in message

    def test_outside_ci_it_is_a_log_line_and_no_annotation(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        said = self._warnings(monkeypatch)
        self._off()
        assert any(_OWED in w for w in said), said
        assert self._annotations(capsys) == []

    @pytest.mark.parametrize(
        ("language", "key"),
        [
            ("python", "quality.python.pip_audit"),
            ("rust", "quality.rust.audit"),
            ("golang", "quality.golang.govulncheck"),
            ("typescript", "quality.typescript.audit"),
            ("javascript", "quality.typescript.audit"),
        ],
    )
    def test_the_gates_this_repo_loses_are_named(
        self, monkeypatch: pytest.MonkeyPatch, language: str, key: str
    ) -> None:
        said = self._warnings(monkeypatch)
        self._off(language)
        message = "\n".join(said)
        assert "quality.gitleaks" in message
        assert "quality.semgrep" in message
        assert key in message

    def test_gates_this_repo_never_ran_are_not_named(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said = self._warnings(monkeypatch)
        self._off("python")
        message = "\n".join(said)
        assert "quality.python.pip_audit" in message
        # bandit ships `disabled`, so turning the stage off takes nothing from it.
        assert "quality.python.bandit" not in message
        assert "quality.rust.audit" not in message

    def test_a_stated_reason_is_printed_and_owes_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said = self._warnings(monkeypatch)
        assert self._off(reason=_REASON) == 0
        assert any(_REASON in w for w in said), said
        assert not any(_OWED in w for w in said), said

    def test_a_whitespace_only_reason_is_no_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said = self._warnings(monkeypatch)
        self._off(reason="   ")
        assert any(_OWED in w for w in said), said

    def test_the_documented_yaml_reaches_the_reader(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A shape the loader does not deliver where the code reads it fails the
        # way #250 did, so go through a real .hyperi-ci.yaml.
        monkeypatch.setattr(config_module, "_config_cache", None)
        (tmp_path / ".hyperi-ci.yaml").write_text(
            f'quality:\n  enabled: false\n  reason: "{_REASON}"\n', encoding="utf-8"
        )
        said = self._warnings(monkeypatch)
        config = load_config(reload=True, project_dir=tmp_path)
        assert stage_quality("python", config) == 0
        assert any(_REASON in w for w in said), said
        assert not any(_OWED in w for w in said), said

    def test_a_missing_reason_is_annotated_in_ci(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(quality_common, "is_ci", lambda: True)
        monkeypatch.setattr(quality_common, "warn", lambda _m: None)
        self._off()
        annotations = self._annotations(capsys)
        assert len(annotations) == 1, annotations
        assert annotations[0].startswith(
            "::warning title=hyperi-ci security gate needs a reason::"
        )
        assert "quality.enabled" in annotations[0]
        # The message is multi-line; a raw newline would end the command early.
        assert "%0A" in annotations[0]

    def test_a_stated_reason_is_annotated_and_cannot_start_a_second_command(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(quality_common, "is_ci", lambda: True)
        monkeypatch.setattr(quality_common, "warn", lambda _m: None)
        self._off(reason="rebuilt under #12\n::error::planted")
        annotations = self._annotations(capsys)
        assert len(annotations) == 1, annotations
        assert annotations[0].startswith("::warning title=hyperi-ci gate turned down::")
        assert "quality.enabled" in annotations[0]
        assert "#12%0A::error::planted" in annotations[0]


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
