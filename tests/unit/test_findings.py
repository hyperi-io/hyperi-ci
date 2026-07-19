# Project:   HyperI CI
# File:      tests/unit/test_findings.py
# Purpose:   Tests for the shared finding surface (annotations/summary/SARIF)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for hyperi_ci.quality.findings - the shared linting surface.

Covers the load-bearing bits: severity folding, the step-global annotation
budget (cap + errors-first + overflow count + no-op outside Actions), the job
summary table, and the multi-run SARIF writer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyperi_ci.quality import findings
from hyperi_ci.quality.findings import Finding


@pytest.fixture(autouse=True)
def _fresh_budget() -> None:
    findings.reset_annotation_budget()


def _f(
    level: str, rule: str = "R1", path: str = "Dockerfile", line: int | None = 1
) -> Finding:
    return Finding(
        tool="hadolint", path=path, line=line, level=level, rule=rule, message="m"
    )


class TestNormaliseLevel:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("error", "error"),
            ("ERROR", "error"),
            ("warning", "warning"),
            ("warn", "warning"),
            ("info", "notice"),
            ("style", "notice"),
            ("note", "notice"),
            ("something-odd", "warning"),  # unknown -> safe middle
        ],
    )
    def test_folds(self, raw: str, expected: str) -> None:
        assert findings.normalise_level(raw) == expected


class TestEmitAnnotations:
    def test_noop_outside_actions(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        dropped = findings.emit_annotations([_f("error")])
        assert dropped == 0
        assert capsys.readouterr().out == ""

    def test_emits_workflow_command(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        findings.emit_annotations(
            [_f("error", rule="DL3008", path="Dockerfile", line=7)]
        )
        out = capsys.readouterr().out
        assert "::error " in out
        assert "file=Dockerfile" in out
        assert "line=7" in out
        assert "DL3008" in out

    def test_budget_cap_and_overflow_count(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        # 12 warnings, cap is 10 -> 2 overflow, 10 printed.
        dropped = findings.emit_annotations([_f("warning") for _ in range(12)])
        assert dropped == 2
        assert capsys.readouterr().out.count("::warning ") == 10

    def test_budget_is_shared_across_calls(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        findings.emit_annotations([_f("error") for _ in range(6)])
        # Second tool in the same step draws from the same pool: 4 left.
        dropped = findings.emit_annotations([_f("error") for _ in range(6)])
        assert dropped == 2

    def test_errors_first(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        # One error last in the list still annotates before warnings are considered.
        findings.reset_annotation_budget()
        findings.emit_annotations([_f("warning"), _f("warning"), _f("error", rule="E")])
        lines = [
            ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("::")
        ]
        assert lines[0].startswith("::error ")

    def test_newlines_encoded(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        f = Finding("t", "f", 1, "error", "R", "line one\nline two")
        findings.emit_annotations([f])
        out = capsys.readouterr().out
        assert "%0A" in out
        assert "\nline two" not in out.split("::error", 1)[1]


class TestJobSummary:
    def test_writes_table(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        findings.append_job_summary("hadolint", [_f("warning", rule="DL3008")])
        text = summary.read_text(encoding="utf-8")
        assert "hadolint: 1 finding(s)" in text
        assert "DL3008" in text
        assert "| Severity | Rule | Location | Message |" in text

    def test_noop_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        # Must not raise.
        findings.append_job_summary("hadolint", [_f("warning")])

    def test_pipe_in_message_escaped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        summary = tmp_path / "s.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        f = Finding("t", "f", 1, "warning", "R", "a | b")
        findings.append_job_summary("t", [f])
        assert "a \\| b" in summary.read_text(encoding="utf-8")


class TestSarif:
    def test_valid_single_run(self, tmp_path: Path) -> None:
        out = tmp_path / "r.sarif"
        findings.write_sarif("hadolint", [_f("error", rule="DL4006")], out)
        doc = json.loads(out.read_text(encoding="utf-8"))
        assert doc["version"] == "2.1.0"
        assert len(doc["runs"]) == 1
        run = doc["runs"][0]
        assert run["tool"]["driver"]["name"] == "hadolint"
        assert run["results"][0]["ruleId"] == "DL4006"
        assert run["results"][0]["level"] == "error"

    def test_appends_second_run(self, tmp_path: Path) -> None:
        out = tmp_path / "r.sarif"
        findings.write_sarif("hadolint", [_f("error")], out)
        findings.write_sarif("kubeconform", [_f("error", path="deploy.yaml")], out)
        doc = json.loads(out.read_text(encoding="utf-8"))
        assert [r["tool"]["driver"]["name"] for r in doc["runs"]] == [
            "hadolint",
            "kubeconform",
        ]

    def test_notice_maps_to_note(self, tmp_path: Path) -> None:
        out = tmp_path / "r.sarif"
        findings.write_sarif("droast", [_f("notice", rule="DF033")], out)
        doc = json.loads(out.read_text(encoding="utf-8"))
        assert doc["runs"][0]["results"][0]["level"] == "note"

    def test_corrupt_prior_file_starts_fresh(self, tmp_path: Path) -> None:
        out = tmp_path / "r.sarif"
        out.write_text("{ not json", encoding="utf-8")
        findings.write_sarif("t", [_f("error")], out)
        doc = json.loads(out.read_text(encoding="utf-8"))
        assert len(doc["runs"]) == 1


class TestParseSarif:
    def test_extracts_finding(self) -> None:
        text = json.dumps(
            {
                "runs": [
                    {
                        "tool": {
                            "driver": {
                                "name": "droast",
                                "rules": [
                                    {"id": "DF070", "helpUri": "https://x/DF070"}
                                ],
                            }
                        },
                        "results": [
                            {
                                "ruleId": "DF070",
                                "level": "warning",
                                "message": {"text": "cache buster"},
                                "locations": [
                                    {
                                        "physicalLocation": {
                                            "artifactLocation": {"uri": "Dockerfile"},
                                            "region": {"startLine": 4},
                                        }
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        )
        out = findings.parse_sarif(text, "droast")
        assert len(out) == 1
        f = out[0]
        assert f.tool == "droast"
        assert f.rule == "DF070"
        assert f.level == "warning"
        assert f.path == "Dockerfile"
        assert f.line == 4
        assert f.url == "https://x/DF070"

    def test_note_folds_to_notice(self) -> None:
        text = json.dumps(
            {
                "runs": [
                    {
                        "tool": {"driver": {}},
                        "results": [
                            {"ruleId": "R", "level": "note", "message": {"text": "m"}}
                        ],
                    }
                ]
            }
        )
        assert findings.parse_sarif(text, "t")[0].level == "notice"

    def test_no_location(self) -> None:
        text = json.dumps(
            {
                "runs": [
                    {
                        "tool": {"driver": {}},
                        "results": [
                            {"ruleId": "R", "level": "error", "message": {"text": "m"}}
                        ],
                    }
                ]
            }
        )
        f = findings.parse_sarif(text, "t")[0]
        assert f.path == "" and f.line is None

    def test_malformed_returns_empty(self) -> None:
        assert findings.parse_sarif("not json", "t") == []
        assert findings.parse_sarif("[]", "t") == []


class TestSurface:
    def test_returns_dropped_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        # 12 warnings, cap 10 -> surface reports 2 dropped.
        assert findings.surface("t", [_f("warning") for _ in range(12)]) == 2

    def test_shared_budget_across_two_tools(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        # Two tools in one process share the budget: 6 + 6 errors -> 2 overflow.
        findings.surface("hadolint", [_f("error") for _ in range(6)])
        assert findings.surface("droast", [_f("error") for _ in range(6)]) == 2

    def test_writes_summary_and_sarif(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "s.md"))
        sarif = tmp_path / "out.sarif"
        findings.surface("t", [_f("error")], sarif_path=sarif)
        assert (tmp_path / "s.md").exists()
        assert sarif.exists()

    def test_summary_write_failure_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Unwritable summary path (a directory) must not crash surfacing.
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path))
        findings.append_job_summary("t", [_f("error")])  # no exception


class TestAnnotationEscaping:
    def test_property_delimiters_stripped(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        # A repo-controlled kind/filename with a comma / :: must not inject a
        # second annotation property.
        f = Finding(
            "kubeconform", "a,line=99.yaml", 1, "error", "schema/Foo,line=7", "m"
        )
        findings.emit_annotations([f])
        line = capsys.readouterr().out.strip()
        # The injected commas are stripped, so "line=99"/"line=7" are inert text
        # inside the file/title values, NOT new comma-delimited properties.
        assert ",line=99" not in line
        assert ",line=7" not in line
        # The one real line property (from f.line=1) survives as a property.
        assert "line=1::" in line


class TestSummaryTruncation:
    def test_truncates_over_cap(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        summary = tmp_path / "s.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        many = [
            _f("warning", rule=f"R{i}") for i in range(findings._MAX_SUMMARY_ROWS + 5)
        ]
        findings.append_job_summary("t", many)
        text = summary.read_text(encoding="utf-8")
        assert "5 more findings truncated" in text
