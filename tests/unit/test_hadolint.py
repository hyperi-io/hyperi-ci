# Project:   HyperI CI
# File:      tests/unit/test_hadolint.py
# Purpose:   Tests for the hadolint Dockerfile gate
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for hyperi_ci.quality.hadolint.

The gate contract: parse hadolint's JSON, surface findings, and fail the stage
only in ``blocking`` mode when an ERROR-severity finding exists - warning/info
are surfaced but never fatal (the recon-confirmed near-silent gate).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.quality import hadolint

_SKIP = "HYPERCI_QUALITY_SKIP"
_STRICT = "HYPERCI_QUALITY_STRICT"


def _cfg(raw: dict | None = None) -> CIConfig:
    return CIConfig(_raw=raw or {})


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_SKIP, raising=False)
    monkeypatch.delenv(_STRICT, raising=False)


def _stub_run(monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
    monkeypatch.setattr(hadolint, "_install_hadolint", lambda: "/usr/bin/hadolint")
    monkeypatch.setattr(
        hadolint,
        "run_cmd",
        lambda *a, **k: SimpleNamespace(stdout=stdout, returncode=0),
    )


class TestParse:
    def test_maps_fields_and_urls(self) -> None:
        payload = json.dumps(
            [
                {
                    "file": "Dockerfile",
                    "line": 3,
                    "level": "error",
                    "code": "SC2086",
                    "message": "quote",
                },
                {
                    "file": "Dockerfile",
                    "line": 5,
                    "level": "warning",
                    "code": "DL3008",
                    "message": "pin",
                },
            ]
        )
        found = hadolint._parse(payload)
        assert len(found) == 2
        assert found[0].level == "error"
        assert "shellcheck.net" in found[0].url
        assert found[1].rule == "DL3008"
        assert "hadolint/hadolint/wiki" in found[1].url

    def test_blank_is_empty(self) -> None:
        assert hadolint._parse("") == []
        assert hadolint._parse("not json") == []


class TestResolveMode:
    def test_default_blocking(self) -> None:
        assert hadolint._resolve_mode(_cfg()) == "blocking"

    def test_disabled(self) -> None:
        assert (
            hadolint._resolve_mode(_cfg({"quality": {"hadolint": "disabled"}}))
            == "disabled"
        )

    def test_skip_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_SKIP, "hadolint")
        assert hadolint._resolve_mode(_cfg()) == "disabled"

    def test_unknown_mode_falls_back_to_default(self) -> None:
        # A typo like `block` must NOT silently disable the gate - it warns and
        # falls back to the tool's default (blocking here), not advisory.
        assert (
            hadolint._resolve_mode(_cfg({"quality": {"hadolint": "block"}}))
            == "blocking"
        )


class TestRun:
    def test_disabled_short_circuits(self) -> None:
        assert hadolint.run(_cfg({"quality": {"hadolint": "disabled"}})) == 0

    def test_no_dockerfile_skips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assert hadolint.run(_cfg()) == 0

    def test_blocking_fails_on_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "Dockerfile").write_text(
            "FROM x\nRUN echo $UNQUOTED\n", encoding="utf-8"
        )
        _stub_run(
            monkeypatch,
            json.dumps(
                [
                    {
                        "file": "Dockerfile",
                        "line": 2,
                        "level": "error",
                        "code": "SC2086",
                        "message": "q",
                    }
                ]
            ),
        )
        assert hadolint.run(_cfg()) == 1

    def test_blocking_passes_on_warnings_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # DL3008-style warnings must NOT fail a blocking gate (near-silent day one).
        monkeypatch.chdir(tmp_path)
        (tmp_path / "Dockerfile").write_text(
            "FROM x\nRUN apt-get install y\n", encoding="utf-8"
        )
        _stub_run(
            monkeypatch,
            json.dumps(
                [
                    {
                        "file": "Dockerfile",
                        "line": 2,
                        "level": "warning",
                        "code": "DL3008",
                        "message": "pin",
                    }
                ]
            ),
        )
        assert hadolint.run(_cfg()) == 0

    def test_warn_mode_never_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "Dockerfile").write_text("FROM x\n", encoding="utf-8")
        _stub_run(
            monkeypatch,
            json.dumps(
                [
                    {
                        "file": "Dockerfile",
                        "line": 1,
                        "level": "error",
                        "code": "SC2086",
                        "message": "q",
                    }
                ]
            ),
        )
        assert hadolint.run(_cfg({"quality": {"hadolint": "warn"}})) == 0

    def test_clean_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "Dockerfile").write_text("FROM x\n", encoding="utf-8")
        _stub_run(monkeypatch, "[]")
        assert hadolint.run(_cfg()) == 0

    def test_missing_tool_blocks_in_ci(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "Dockerfile").write_text("FROM x\n", encoding="utf-8")
        monkeypatch.setattr(hadolint, "_install_hadolint", lambda: None)
        monkeypatch.setattr(hadolint, "is_ci", lambda: True)
        assert hadolint.run(_cfg()) == 1

    def test_missing_tool_warn_skips_locally(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "Dockerfile").write_text("FROM x\n", encoding="utf-8")
        monkeypatch.setattr(hadolint, "_install_hadolint", lambda: None)
        monkeypatch.setattr(hadolint, "is_ci", lambda: False)
        assert hadolint.run(_cfg()) == 0

    def test_tool_error_blocks_in_ci(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # --no-fail means non-zero exit + no parseable output = a TOOL error
        # (corrupt binary), not a clean pass. A blocking gate must not go green.
        monkeypatch.chdir(tmp_path)
        (tmp_path / "Dockerfile").write_text("FROM x\n", encoding="utf-8")
        monkeypatch.setattr(hadolint, "_install_hadolint", lambda: "/usr/bin/hadolint")
        monkeypatch.setattr(
            hadolint,
            "run_cmd",
            lambda *a, **k: SimpleNamespace(stdout="", returncode=127),
        )
        monkeypatch.setattr(hadolint, "is_ci", lambda: True)
        assert hadolint.run(_cfg()) == 1

    def test_exec_oserror_does_not_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "Dockerfile").write_text("FROM x\n", encoding="utf-8")
        monkeypatch.setattr(hadolint, "_install_hadolint", lambda: "/usr/bin/hadolint")

        def _boom(*a, **k):  # noqa: ANN002, ANN003
            raise OSError("no exec bit")

        monkeypatch.setattr(hadolint, "run_cmd", _boom)
        monkeypatch.setattr(hadolint, "is_ci", lambda: False)
        assert hadolint.run(_cfg()) == 0  # handled, not a traceback
