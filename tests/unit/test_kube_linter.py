# Project:   HyperI CI
# File:      tests/unit/test_kube_linter.py
# Purpose:   Tests for the kube-linter k8s best-practice advisory
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for hyperi_ci.quality.kube_linter - advisory only, never gates."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.quality import findings as fdg
from hyperi_ci.quality import kube_linter

_SARIF = json.dumps(
    {
        "runs": [
            {
                "tool": {"driver": {"name": "kube-linter", "rules": []}},
                "results": [
                    {
                        "ruleId": "no-read-only-root-fs",
                        "level": "warning",
                        "message": {"text": "container should run read-only"},
                        "locations": [
                            {"physicalLocation": {"artifactLocation": {"uri": "chart"}}}
                        ],
                    }
                ],
            }
        ]
    }
)


def _cfg(raw: dict | None = None) -> CIConfig:
    return CIConfig(_raw=raw or {})


def _stub(
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    *,
    exe: str | None = "/usr/bin/kube-linter",
) -> None:
    monkeypatch.setattr(kube_linter, "ci_binary", lambda _name: exe)
    monkeypatch.setattr(kube_linter, "find_tool", lambda *a, **k: exe)
    monkeypatch.setattr(
        fdg,
        "run_cmd",
        lambda *a, **k: SimpleNamespace(stdout=stdout, stderr="", returncode=1),
    )


class TestRun:
    def test_disabled(self) -> None:
        assert (
            kube_linter.run(
                [Path("chart")], _cfg({"quality": {"kube_linter": "disabled"}})
            )
            == 0
        )

    def test_no_targets_skips(self) -> None:
        assert kube_linter.run([], _cfg()) == 0

    def test_findings_never_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub(monkeypatch, _SARIF)
        # returncode 1 from kube-linter (it found violations) must NOT propagate.
        assert kube_linter.run([Path("chart")], _cfg()) == 0

    def test_missing_tool_info_skips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub(monkeypatch, "", exe=None)
        assert kube_linter.run([Path("chart")], _cfg()) == 0

    def test_oserror_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            kube_linter, "ci_binary", lambda _name: "/usr/bin/kube-linter"
        )

        def _boom(*a, **k):  # noqa: ANN002, ANN003
            raise OSError("exec failed")

        monkeypatch.setattr(fdg, "run_cmd", _boom)
        assert kube_linter.run([Path("chart")], _cfg()) == 0

    def test_a_run_with_no_report_is_not_a_silent_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in ("GITHUB_ACTIONS", "GITHUB_STEP_SUMMARY"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(kube_linter, "ci_binary", lambda _name: "kube-linter")
        monkeypatch.setattr(
            fdg,
            "run_cmd",
            lambda *a, **k: SimpleNamespace(
                stdout="", stderr=_SKIPPED_CHART, returncode=0
            ),
        )
        before = fdg.surfaced_count()
        assert kube_linter.run([Path("chart")], _cfg()) == 0
        assert fdg.surfaced_count() - before == 1


# kube-linter 0.8.3 --verbose stderr, verbatim, for a chart with no values.yaml.
_SKIPPED_CHART = (
    "Warning: failed to load object from charts/app: loading values.yaml file: "
    "open charts/app/values.yaml: no such file or directory\n"
    "Warning: no valid objects found.\n"
)


class TestRunProblems:
    def test_a_clean_report_has_none(self) -> None:
        assert kube_linter.run_problems(_SARIF, "", 1) == []

    def test_each_skipped_target_is_a_finding(self) -> None:
        found = kube_linter.run_problems("", _SKIPPED_CHART, 0)
        assert [(f.path, f.rule) for f in found] == [
            ("charts/app", "kube-linter/load-failed")
        ]
        assert "values.yaml" in found[0].message

    def test_a_skipped_target_beside_a_report_still_counts(self) -> None:
        found = kube_linter.run_problems(_SARIF, _SKIPPED_CHART, 1)
        assert [f.rule for f in found] == ["kube-linter/load-failed"]

    def test_a_rejected_config_is_a_finding(self) -> None:
        stderr = 'Error: enabled checks validation error: check "x" not found\n'
        found = kube_linter.run_problems("", stderr, 1)
        assert [f.rule for f in found] == ["kube-linter/no-report"]
        assert 'check "x" not found' in found[0].message

    def test_no_output_at_all_names_the_exit_code(self) -> None:
        found = kube_linter.run_problems("", "", 2)
        assert found[0].message.endswith("exited 2")


class TestRelocate:
    def test_a_finding_in_a_render_moves_to_its_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        render = tmp_path / "scratch" / "c--defaults.yaml"
        in_render = fdg.Finding(
            "kube-linter", "scratch/c--defaults.yaml", 1, "warning", "r", "m"
        )
        elsewhere = fdg.Finding("kube-linter", "deploy.yaml", 1, "warning", "r", "m")
        moved = kube_linter.relocate([in_render, elsewhere], {render: Path("charts/c")})
        assert [(f.path, f.line) for f in moved] == [
            ("charts/c", None),
            ("deploy.yaml", 1),
        ]
