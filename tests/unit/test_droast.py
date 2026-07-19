# Project:   HyperI CI
# File:      tests/unit/test_droast.py
# Purpose:   Tests for the droast Dockerfile advisory
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for hyperi_ci.quality.droast - advisory only, never gates."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.quality import droast

_SARIF = json.dumps(
    {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "droast",
                        "rules": [{"id": "DF070", "helpUri": "https://x/DF070"}],
                    }
                },
                "results": [
                    {
                        "ruleId": "DF070",
                        "level": "warning",
                        "message": {"text": "COPY . . before install busts the cache"},
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
        ],
    }
)


def _cfg(raw: dict | None = None) -> CIConfig:
    return CIConfig(_raw=raw or {})


def _stub(
    monkeypatch: pytest.MonkeyPatch, stdout: str, *, exe: str | None = "/usr/bin/droast"
) -> None:
    monkeypatch.setattr(droast, "find_tool", lambda *a, **k: exe)
    monkeypatch.setattr(
        droast, "run_cmd", lambda *a, **k: SimpleNamespace(stdout=stdout, returncode=0)
    )


class TestRun:
    def test_disabled_short_circuits(self) -> None:
        assert droast.run(_cfg({"quality": {"droast": "disabled"}})) == 0

    def test_no_dockerfile_skips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assert droast.run(_cfg()) == 0

    def test_missing_tool_info_skips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "Dockerfile").write_text("FROM x\n", encoding="utf-8")
        _stub(monkeypatch, "", exe=None)
        assert droast.run(_cfg()) == 0

    def test_findings_never_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "Dockerfile").write_text("FROM x\nCOPY . .\n", encoding="utf-8")
        _stub(monkeypatch, _SARIF)
        # Advisory: even with findings it returns 0.
        assert droast.run(_cfg()) == 0

    def test_uses_shipped_config_when_no_repo_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "Dockerfile").write_text("FROM x\n", encoding="utf-8")
        captured: dict = {}

        def _capture(cmd, **kw):  # noqa: ANN001, ANN003
            captured["cmd"] = cmd
            return SimpleNamespace(stdout="", returncode=0)

        monkeypatch.setattr(droast, "find_tool", lambda *a, **k: "/usr/bin/droast")
        monkeypatch.setattr(droast, "run_cmd", _capture)
        droast.run(_cfg())
        assert "--config" in captured["cmd"]

    def test_repo_toml_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "Dockerfile").write_text("FROM x\n", encoding="utf-8")
        (tmp_path / "droast.toml").write_text(
            "min-severity = 'info'\n", encoding="utf-8"
        )
        captured: dict = {}

        def _capture(cmd, **kw):  # noqa: ANN001, ANN003
            captured["cmd"] = cmd
            return SimpleNamespace(stdout="", returncode=0)

        monkeypatch.setattr(droast, "find_tool", lambda *a, **k: "/usr/bin/droast")
        monkeypatch.setattr(droast, "run_cmd", _capture)
        droast.run(_cfg())
        # Repo's own droast.toml is auto-discovered; we must NOT force --config.
        assert "--config" not in captured["cmd"]

    def test_oserror_is_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "Dockerfile").write_text("FROM x\n", encoding="utf-8")

        def _boom(*a, **k):  # noqa: ANN002, ANN003
            raise OSError("exec failed")

        monkeypatch.setattr(droast, "find_tool", lambda *a, **k: "/usr/bin/droast")
        monkeypatch.setattr(droast, "run_cmd", _boom)
        assert droast.run(_cfg()) == 0
