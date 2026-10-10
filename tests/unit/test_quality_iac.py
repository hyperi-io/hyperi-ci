# Project:   HyperI CI
# File:      tests/unit/test_quality_iac.py
# Purpose:   lint-iac inside the quality stage, under the quality.iac umbrella mode
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

"""lint-iac runs inside `run quality` under one umbrella mode, `quality.iac`.

It ships `warn`, which caps every dimension at `warn`, so an existing tree with
IaC findings keeps passing while the backlog clears and reports no errors.
`blocking` runs each dimension at its own mode and `disabled` runs nothing. The
stage runs every dimension except the two it must not: hadolint already runs
there, and checkov is a whole-tree advisory.
"""

from collections.abc import Sequence
from pathlib import Path

import pytest

from hyperi_ci import common, dispatch
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import (
    SECURITY_TOOLS,
    mode_ceiling,
    resolve_tool_mode,
)
from hyperi_ci.quality import lint_iac

_NOT_FAILING = "crashed or its config is invalid"


class _Recorder:
    """Stands in for lint_iac.run: records each call and the kubeconform mode it saw."""

    def __init__(self, rc: int) -> None:
        self.rc = rc
        self.calls: list[tuple[Path, tuple[str, ...]]] = []
        self.kubeconform_modes: list[str] = []

    def __call__(
        self,
        root: Path | str,
        config: CIConfig,
        *,
        sarif_path: str | Path | None = None,
        dimensions: Sequence[str] = lint_iac.DIMENSIONS,
    ) -> int:
        self.calls.append((Path(root), tuple(dimensions)))
        self.kubeconform_modes.append(resolve_tool_mode("kubeconform", config))
        return self.rc


@pytest.fixture(autouse=True)
def _plain_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HYPERCI_QUALITY_STRICT", raising=False)
    monkeypatch.delenv("HYPERCI_QUALITY_SKIP", raising=False)
    monkeypatch.setattr(common, "is_github_actions", lambda: False)


def _lint(monkeypatch: pytest.MonkeyPatch, rc: int) -> _Recorder:
    recorder = _Recorder(rc)
    monkeypatch.setattr(dispatch.lint_iac, "run", recorder)
    return recorder


def _warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    said: list[str] = []
    monkeypatch.setattr(dispatch, "warn", said.append)
    return said


def _config(mode: str | None = None) -> CIConfig:
    quality = {} if mode is None else {"iac": mode}
    return CIConfig(_raw={"quality": quality})


class TestUmbrellaMode:
    def test_warn_caps_a_blocking_tool_at_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # kubeconform ships blocking; under the warn umbrella it must report warnings.
        recorder = _lint(monkeypatch, rc=0)
        assert dispatch._run_lint_iac(_config("warn")) == 0
        assert recorder.kubeconform_modes == ["warn"]

    def test_blocking_runs_each_tool_at_its_own_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _lint(monkeypatch, rc=0)
        dispatch._run_lint_iac(_config("blocking"))
        assert recorder.kubeconform_modes == ["blocking"]

    def test_the_cap_ends_with_the_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _lint(monkeypatch, rc=0)
        dispatch._run_lint_iac(_config("warn"))
        assert resolve_tool_mode("kubeconform", _config()) == "blocking"

    def test_warn_reports_a_crashed_dimension_and_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _lint(monkeypatch, rc=1)
        said = _warnings(monkeypatch)
        assert dispatch._run_lint_iac(_config("warn")) == 0
        assert len(recorder.calls) == 1
        assert len(said) == 1
        assert _NOT_FAILING in said[0]

    def test_an_unset_key_behaves_as_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _lint(monkeypatch, rc=1)
        said = _warnings(monkeypatch)
        assert dispatch._run_lint_iac(_config()) == 0
        assert any(_NOT_FAILING in line for line in said), said
        assert recorder.kubeconform_modes == ["warn"]

    def test_warn_says_nothing_when_lint_iac_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _lint(monkeypatch, rc=0)
        said = _warnings(monkeypatch)
        assert dispatch._run_lint_iac(_config("warn")) == 0
        assert said == []

    def test_blocking_fails_on_a_gate_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _lint(monkeypatch, rc=1)
        said = _warnings(monkeypatch)
        assert dispatch._run_lint_iac(_config("blocking")) == 1
        assert said == []

    def test_blocking_passes_a_clean_tree(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _lint(monkeypatch, rc=0)
        assert dispatch._run_lint_iac(_config("blocking")) == 0

    def test_disabled_never_runs_lint_iac(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _lint(monkeypatch, rc=1)
        assert dispatch._run_lint_iac(_config("disabled")) == 0
        assert recorder.calls == []

    def test_strict_promotes_warn_to_blocking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HYPERCI_QUALITY_STRICT", "1")
        recorder = _lint(monkeypatch, rc=1)
        assert dispatch._run_lint_iac(_config("warn")) == 1
        assert recorder.kubeconform_modes == ["blocking"]

    def test_runs_over_the_working_directory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        recorder = _lint(monkeypatch, rc=0)
        dispatch._run_lint_iac(_config())
        assert recorder.calls[0][0] == tmp_path

    def test_the_umbrella_is_not_a_security_gate(self) -> None:
        # Turning it down owes no reason: the per-tool security modes are untouched.
        assert "iac" not in SECURITY_TOOLS


class TestModeCeiling:
    def test_caps_a_harder_mode(self) -> None:
        with mode_ceiling("warn"):
            assert resolve_tool_mode("tofu", _config()) == "warn"

    @pytest.mark.parametrize("mode", ["warn", "disabled"])
    def test_leaves_a_softer_mode_alone(self, mode: str) -> None:
        config = CIConfig(_raw={"quality": {"tofu": mode}})
        with mode_ceiling("warn"):
            assert resolve_tool_mode("tofu", config) == mode

    def test_does_not_cap_a_security_tool(self) -> None:
        assert "gitleaks" in SECURITY_TOOLS
        config = CIConfig(_raw={"quality": {"gitleaks": "blocking"}})
        with mode_ceiling("warn"):
            assert resolve_tool_mode("gitleaks", config) == "blocking"

    def test_resets_after_an_exception(self) -> None:
        with pytest.raises(RuntimeError), mode_ceiling("warn"):
            raise RuntimeError
        assert resolve_tool_mode("tofu", _config()) == "blocking"


class TestQualityDimensions:
    def test_the_stage_passes_the_quality_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _lint(monkeypatch, rc=0)
        dispatch._run_lint_iac(_config())
        assert recorder.calls[0][1] == lint_iac.QUALITY_DIMENSIONS

    @pytest.mark.parametrize("dimension", ["dockerfile", "checkov"])
    def test_excludes_what_the_stage_must_not_run(self, dimension: str) -> None:
        assert dimension in lint_iac.DIMENSIONS
        assert dimension not in lint_iac.QUALITY_DIMENSIONS

    def test_keeps_every_other_dimension_in_order(self) -> None:
        expected = tuple(
            d for d in lint_iac.DIMENSIONS if d not in ("dockerfile", "checkov")
        )
        assert lint_iac.QUALITY_DIMENSIONS == expected
        assert "manifests" in lint_iac.QUALITY_DIMENSIONS


class TestStageWiring:
    """stage_quality reaches lint-iac, and its verdict decides the stage."""

    @pytest.fixture(autouse=True)
    def _other_gates_pass(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(dispatch.deprecated_files, "scan", lambda: None)
        monkeypatch.setattr(dispatch.repo_advisor, "run", lambda *a, **k: 0)
        for module in ("gitleaks", "charset", "semgrep", "hadolint", "droast"):
            monkeypatch.setattr(getattr(dispatch, module), "run", lambda *a, **k: 0)
        monkeypatch.setattr(dispatch.lint_docs, "run", lambda *a, **k: 0)
        monkeypatch.setattr(dispatch, "_dispatch_to_handler", lambda *a, **k: 0)

    def test_a_blocking_failure_fails_the_stage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _lint(monkeypatch, rc=1)
        assert dispatch.stage_quality("python", _config("blocking")) == 1
        assert len(recorder.calls) == 1

    def test_a_warn_failure_leaves_the_stage_green(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _lint(monkeypatch, rc=1)
        assert dispatch.stage_quality("python", _config("warn")) == 0
        assert len(recorder.calls) == 1

    def test_a_disabled_quality_stage_never_runs_lint_iac(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _lint(monkeypatch, rc=1)
        config = CIConfig(
            _raw={"quality": {"enabled": False, "reason": "test", "iac": "blocking"}}
        )
        assert dispatch.stage_quality("python", config) == 0
        assert recorder.calls == []


class TestARealDimensionUnderTheCap:
    """compose-pins runs for real: it needs no external binary."""

    @pytest.fixture(autouse=True)
    def _unpinned_compose_tree(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "docker-compose.yml").write_text(
            "services:\n  web:\n    image: nginx\n", encoding="utf-8", newline="\n"
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(lint_iac.compose_config, "run", lambda *a, **k: 0)

    @staticmethod
    def _surfaced(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[str]]]:
        seen: list[tuple[str, list[str]]] = []

        def _surface(tool: str, findings: list, **_kwargs: object) -> int:
            seen.append((tool, [f.level for f in findings]))
            return 0

        monkeypatch.setattr(lint_iac.fdg, "surface", _surface)
        return seen

    def test_warn_surfaces_the_unpinned_image_as_a_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = self._surfaced(monkeypatch)
        assert dispatch._run_lint_iac(_config("warn")) == 0
        assert ("compose-pins", ["warning"]) in seen

    def test_blocking_surfaces_it_as_an_error_and_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = self._surfaced(monkeypatch)
        assert dispatch._run_lint_iac(_config("blocking")) == 1
        assert ("compose-pins", ["error"]) in seen
