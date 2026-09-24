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

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from hyperi_ci import common
from hyperi_ci import config as config_module
from hyperi_ci.common import run_cmd
from hyperi_ci.config import CIConfig, load_config
from hyperi_ci.dispatch import stage_build, stage_quality, stage_test
from hyperi_ci.languages import quality_common
from hyperi_ci.languages.python.test import _absolve_empty_run
from hyperi_ci.languages.quality_common import GateReasonRequiredError

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
        off = _config(quality={"enabled": False, "reason": _REASON})
        with patch("hyperi_ci.dispatch._dispatch_to_handler") as handler:
            with patch("hyperi_ci.dispatch.deprecated_files.scan"):
                with patch("hyperi_ci.dispatch.repo_advisor.run"):
                    assert stage_quality("python", off) == 0
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
        monkeypatch.setattr(common, "is_github_actions", lambda: False)

    @staticmethod
    def _warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
        said: list[str] = []
        monkeypatch.setattr(quality_common, "warn", said.append)
        monkeypatch.setattr(common, "warn", said.append)
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

    @classmethod
    def _owed(cls, language: str = "python", **quality: object) -> str:
        with pytest.raises(GateReasonRequiredError) as caught:
            cls._off(language, **quality)
        return str(caught.value)

    def test_a_missing_reason_fails_with_a_fix_to_paste(self) -> None:
        message = self._owed()
        assert "quality.enabled" in message
        assert _OWED in message
        assert "enabled: false" in message
        assert "reason:" in message

    def test_a_missing_reason_is_neither_logged_nor_annotated_here(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # run_stage reports the failure once, so a warning here would be twice.
        monkeypatch.setattr(common, "is_github_actions", lambda: True)
        said = self._warnings(monkeypatch)
        self._owed()
        assert said == []
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
    def test_the_gates_this_repo_loses_are_named(self, language: str, key: str) -> None:
        message = self._owed(language)
        assert "quality.gitleaks" in message
        assert "quality.semgrep" in message
        assert key in message

    def test_gates_this_repo_never_ran_are_not_named(self) -> None:
        message = self._owed("python")
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

    def test_a_whitespace_only_reason_is_no_reason(self) -> None:
        assert _OWED in self._owed(reason="   ")

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

    def test_in_ci_a_stated_reason_is_one_annotation_on_one_line(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(common, "is_github_actions", lambda: True)
        said = self._warnings(monkeypatch)
        self._off(reason="rebuilt under #12\r\n::error::planted")
        annotations = self._annotations(capsys)
        assert len(annotations) == 1, annotations
        assert annotations[0].startswith("::warning title=hyperi-ci gate turned down::")
        assert "reason: rebuilt under #12 ::error::planted" in annotations[0]
        assert said == []

    def test_outside_ci_a_stated_reason_is_one_log_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said = self._warnings(monkeypatch)
        self._off(reason="rebuilt under #12\n::error::planted")
        assert len(said) == 1, said
        assert "reason: rebuilt under #12 ::error::planted" in said[0]
        assert "\n" not in said[0]


class TestRelaxedGateUnderTheRealLogger:
    """What a GitHub Actions runner reads when quality is switched off.

    scalo writes a warning there as ``::warning::`` plus its first line and
    passes every later line through raw, so a stub of ``warn`` cannot show
    whether repo config reaches the runner as a workflow command.
    """

    @staticmethod
    def _commands(reason: str) -> list[str]:
        code = (
            "from hyperi_ci.languages.quality_common import note_quality_disabled\n"
            f"note_quality_disabled('python', {reason!r})\n"
        )
        result = run_cmd(
            [sys.executable, "-c", code],
            capture=True,
            env={"CI": "true", "GITHUB_ACTIONS": "true"},
            timeout=60,
        )
        output = f"{result.stdout}\n{result.stderr}"
        return [line for line in output.splitlines() if line.startswith("::")]

    def test_a_planted_line_break_starts_no_command(self) -> None:
        commands = self._commands("rebuilt under #12\n::error title=planted::x")
        assert len(commands) == 1, commands
        assert commands[0].startswith("::warning title=hyperi-ci gate turned down::")

    def test_the_multi_line_reason_owed_failure_is_one_annotation(
        self, tmp_path: Path
    ) -> None:
        # The owed reason fails the stage, so it has to reach the runner through
        # run_stage, as one error annotation and never as a traceback.
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "probe"\nversion = "0.0.0"\n', encoding="utf-8"
        )
        (tmp_path / ".hyperi-ci.yaml").write_text(
            "quality:\n  enabled: false\n", encoding="utf-8"
        )
        code = (
            "import sys\n"
            "from pathlib import Path\n"
            "from hyperi_ci.dispatch import run_stage\n"
            f"sys.exit(run_stage('quality', project_dir=Path({str(tmp_path)!r})))\n"
        )
        result = run_cmd(
            [sys.executable, "-c", code],
            capture=True,
            check=False,
            env={"CI": "true", "GITHUB_ACTIONS": "true"},
            timeout=60,
        )
        output = f"{result.stdout}\n{result.stderr}"
        commands = [
            line
            for line in output.splitlines()
            if line.startswith("::")
            and not line.startswith(("::group::", "::endgroup::"))
        ]
        assert result.returncode == 1, output
        assert "Traceback" not in output, output
        assert len(commands) == 1, commands
        assert commands[0].startswith(
            "::error title=hyperi-ci security gate needs a reason::"
        )
        assert "quality.enabled" in commands[0]


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
