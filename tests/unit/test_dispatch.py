# Project:   HyperI CI
# File:      tests/unit/test_dispatch.py
# Purpose:   Unit tests for the stage dispatcher
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from hyperi_ci import common, dispatch
from hyperi_ci import config as config_module
from hyperi_ci.cli import app
from hyperi_ci.config import CIConfig
from hyperi_ci.dispatch import _find_handler_module

_OWED_TITLE = "hyperi-ci security gate needs a reason"
_REASON = "security gates run in the org-level pipeline for this mirror"


class TestPublishIsAnAliasForRelease:
    """hyperi-io/vector-vrl calls `hyperi-ci run publish` from its own
    workflow, so the old stage name resolves to the release handler."""

    def test_the_alias_reaches_the_same_handler_module(self) -> None:
        assert _find_handler_module("python", "publish") is _find_handler_module(
            "python", "release"
        )

    def test_both_names_are_accepted_stages(self) -> None:
        assert {"release", "publish"} <= set(dispatch.VALID_STAGES)

    def test_the_alias_is_not_rejected_as_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An unknown stage returns before the chdir, so reaching it proves
        # `publish` resolved rather than being rejected.
        seen: list[Path] = []
        monkeypatch.setattr("os.chdir", lambda path: seen.append(Path(path)))
        monkeypatch.setattr(dispatch, "detect_language", lambda _dir: None)
        assert dispatch.run_stage("publish", project_dir=tmp_path) == 1
        assert seen == [tmp_path.resolve()]


class TestProjectDirReachesTheHandlers:
    """`-C` has to move the process, because handlers run tools in cwd.

    `hyperi-ci run quality -C <other-repo>` resolved the root for language
    detection and config, then ran cargo/uv/npm wherever the shell happened
    to be (issue #109).
    """

    @staticmethod
    def _record_chdir(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
        seen: list[Path] = []
        monkeypatch.setattr("os.chdir", lambda path: seen.append(Path(path)))
        monkeypatch.setattr(dispatch, "detect_language", lambda _dir: None)
        return seen

    def test_chdir_to_project_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = self._record_chdir(monkeypatch)
        assert dispatch.run_stage("quality", project_dir=tmp_path) == 1
        assert seen == [tmp_path.resolve()]

    def test_chdir_to_cwd_when_no_project_dir(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = self._record_chdir(monkeypatch)
        assert dispatch.run_stage("quality") == 1
        assert seen == [Path.cwd().resolve()]

    def test_unknown_stage_does_not_move_the_process(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = self._record_chdir(monkeypatch)
        assert dispatch.run_stage("nonsense", project_dir=tmp_path) == 1
        assert seen == []

    def test_check_gives_every_stage_the_same_relative_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The first stage chdirs into `-C`, so the second must not re-resolve it."""
        project = tmp_path / "proj"
        project.mkdir()
        monkeypatch.chdir(tmp_path)
        seen: list[Path | None] = []

        def _stage(_stage: str, *, project_dir: Path | None, **_kwargs: object) -> int:
            seen.append(project_dir)
            monkeypatch.chdir(project_dir or Path.cwd())
            return 0

        monkeypatch.setattr("hyperi_ci.cli.run_stage", _stage)
        result = CliRunner().invoke(app, ["check", "-C", "proj"])
        assert result.exit_code == 0, result.output
        assert seen == [project.resolve(), project.resolve()]


class TestAnOwedReasonFailsTheStage:
    """A security gate turned down with no reason fails the quality stage.

    The reader gets the message that names the key and the YAML to paste,
    reported by run_stage, never a traceback.
    """

    @pytest.fixture(autouse=True)
    def _project(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # run_stage moves the process into the project and caches its config.
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(config_module, "_config_cache", None)
        monkeypatch.delenv("HYPERCI_QUALITY_SKIP", raising=False)
        monkeypatch.delenv("HYPERCI_QUALITY_STRICT", raising=False)
        monkeypatch.setattr(common, "is_github_actions", lambda: False)
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "probe"\nversion = "0.0.0"\n', encoding="utf-8"
        )

    @staticmethod
    def _run(tmp_path: Path, quality: dict[str, object]) -> int:
        (tmp_path / ".hyperi-ci.yaml").write_text(
            yaml.safe_dump({"quality": quality}), encoding="utf-8"
        )
        return dispatch.run_stage("quality", project_dir=tmp_path)

    @staticmethod
    def _errors(monkeypatch: pytest.MonkeyPatch) -> list[str]:
        said: list[str] = []
        monkeypatch.setattr(common, "error", said.append)
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

    def test_a_security_gate_down_without_a_reason_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said = self._errors(monkeypatch)
        assert self._run(tmp_path, {"alint": "disabled", "gitleaks": "warn"}) == 1
        message = "\n".join(said)
        assert "quality.gitleaks: gitleaks is a security gate" in message
        assert "mode: warn" in message
        assert "reason:" in message

    def test_quality_off_without_a_reason_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said = self._errors(monkeypatch)
        assert self._run(tmp_path, {"enabled": False}) == 1
        message = "\n".join(said)
        assert "quality.enabled: false turns off every security gate" in message
        assert "reason:" in message

    def test_quality_off_with_a_reason_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said = self._errors(monkeypatch)
        assert self._run(tmp_path, {"enabled": False, "reason": _REASON}) == 0
        assert said == []

    def test_in_ci_the_failure_is_one_error_annotation_on_one_line(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # The annotation reaches the run summary, where a folded log group
        # cannot hide it, and a raw newline would end the command early.
        monkeypatch.setattr(common, "is_github_actions", lambda: True)
        said = self._errors(monkeypatch)
        assert self._run(tmp_path, {"alint": "disabled", "gitleaks": "warn"}) == 1
        annotations = self._annotations(capsys)
        assert len(annotations) == 1, annotations
        assert annotations[0].startswith(f"::error title={_OWED_TITLE}::")
        assert "quality.gitleaks" in annotations[0]
        assert "%0A" in annotations[0]
        # Under GitHub Actions the logger's own line is a second annotation.
        assert said == []


class TestLocalGatesCloseTheCiOnlyHole:
    """A gate that runs only as a CI job makes a local green a lie. A repo
    declares those commands and gets them back in `hyperi-ci check`."""

    @staticmethod
    def _config(gates: object) -> CIConfig:
        return CIConfig(_raw={"quality": {"local_gates": gates}})

    def test_no_declaration_is_a_clean_no_op(self) -> None:
        assert dispatch._run_local_gates(CIConfig(_raw={})) == 0

    def test_a_passing_gate_returns_zero(self) -> None:
        assert (
            dispatch._run_local_gates(
                self._config([{"name": "ok", "command": ["true"]}])
            )
            == 0
        )

    def test_a_failing_gate_fails_the_check(self) -> None:
        """The whole point: CI would have failed, so the local check must."""
        assert (
            dispatch._run_local_gates(
                self._config([{"name": "boom", "command": ["false"]}])
            )
            != 0
        )

    def test_a_malformed_entry_fails_loudly_rather_than_skipping(self) -> None:
        """A silently skipped gate is the defect this exists to close."""
        assert dispatch._run_local_gates(self._config([{"name": "no command"}])) == 1
        assert dispatch._run_local_gates(self._config([{"command": ["true"]}])) == 1
        assert dispatch._run_local_gates(self._config(["not-a-mapping"])) == 1

    def test_the_first_failure_stops_the_rest(self) -> None:
        gates = [
            {"name": "first", "command": ["false"]},
            {"name": "second", "command": ["true"]},
        ]
        assert dispatch._run_local_gates(self._config(gates)) != 0

    def test_this_repo_declares_its_two_ci_only_gates(self) -> None:
        """versions SSOT and workflow interfaces are blocking jobs in ci.yml
        with no other local path. Read by PATH, not `load_config()`, which
        resolves against the working directory and made this pass alone and
        fail in the suite."""
        root = Path(__file__).resolve().parents[2]
        declared = yaml.safe_load(
            (root / ".hyperi-ci.yaml").read_text(encoding="utf-8")
        )["quality"]["local_gates"]
        assert {g["name"] for g in declared} == {
            "version SSOT",
            "workflow interfaces",
        }
