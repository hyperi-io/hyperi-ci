# Project:   HyperI CI
# File:      tests/unit/test_kubeconform.py
# Purpose:   Tests for the kubeconform k8s schema-validation gate
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for hyperi_ci.quality.kubeconform."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.quality import kubeconform

_SKIP = "HYPERCI_QUALITY_SKIP"


def _cfg(raw: dict | None = None) -> CIConfig:
    return CIConfig(_raw=raw or {})


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_SKIP, raising=False)
    monkeypatch.delenv("HYPERCI_QUALITY_STRICT", raising=False)


def _stub(
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    *,
    exe: str | None = "/usr/bin/kubeconform",
) -> None:
    monkeypatch.setattr(kubeconform, "_install_kubeconform", lambda: exe)
    monkeypatch.setattr(
        kubeconform,
        "run_cmd",
        lambda *a, **k: SimpleNamespace(stdout=stdout, returncode=0),
    )


class TestParse:
    def test_invalid_and_error_are_findings(self) -> None:
        payload = json.dumps(
            {
                "resources": [
                    {
                        "filename": "a.yaml",
                        "kind": "Deployment",
                        "name": "x",
                        "status": "statusInvalid",
                        "msg": "bad",
                    },
                    {
                        "filename": "b.yaml",
                        "kind": "Service",
                        "name": "y",
                        "status": "statusValid",
                        "msg": "",
                    },
                    {
                        "filename": "c.yaml",
                        "kind": "Foo",
                        "name": "z",
                        "status": "statusSkipped",
                        "msg": "",
                    },
                ]
            }
        )
        found = kubeconform._parse(payload)
        assert len(found) == 1
        assert found[0].level == "error"
        assert found[0].path == "a.yaml"
        assert "Deployment" in found[0].rule

    def test_uppercase_status(self) -> None:
        payload = json.dumps(
            {
                "resources": [
                    {"filename": "a", "kind": "X", "status": "INVALID", "msg": "m"}
                ]
            }
        )
        assert len(kubeconform._parse(payload)) == 1

    def test_blank(self) -> None:
        assert kubeconform._parse("") == []
        assert kubeconform._parse("not json") == []


class TestSchemaLocations:
    def test_default_includes_catalog(self) -> None:
        locs = kubeconform._schema_locations(_cfg())
        assert locs[0] == "default"
        assert any("datreeio" in loc for loc in locs)

    def test_extra_appended(self) -> None:
        cfg = _cfg({"quality": {"kubeconform": {"schema_locations": ["/my/crds"]}}})
        assert "/my/crds" in kubeconform._schema_locations(cfg)


class TestRun:
    def test_no_manifests_skips(self) -> None:
        assert kubeconform.run([], _cfg()) == 0

    def test_disabled(self) -> None:
        assert (
            kubeconform.run(
                [Path("a.yaml")], _cfg({"quality": {"kubeconform": "disabled"}})
            )
            == 0
        )

    def test_blocking_fails_on_invalid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub(
            monkeypatch,
            json.dumps(
                {
                    "resources": [
                        {
                            "filename": "a",
                            "kind": "X",
                            "status": "statusInvalid",
                            "msg": "m",
                        }
                    ]
                }
            ),
        )
        assert kubeconform.run([Path("a.yaml")], _cfg()) == 1

    def test_valid_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub(
            monkeypatch,
            json.dumps(
                {"resources": [{"filename": "a", "kind": "X", "status": "statusValid"}]}
            ),
        )
        assert kubeconform.run([Path("a.yaml")], _cfg()) == 0

    def test_warn_never_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub(
            monkeypatch,
            json.dumps(
                {
                    "resources": [
                        {
                            "filename": "a",
                            "kind": "X",
                            "status": "statusInvalid",
                            "msg": "m",
                        }
                    ]
                }
            ),
        )
        assert (
            kubeconform.run(
                [Path("a.yaml")], _cfg({"quality": {"kubeconform": "warn"}})
            )
            == 0
        )

    def test_missing_tool_blocks_in_ci(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(kubeconform, "_install_kubeconform", lambda: None)
        monkeypatch.setattr(kubeconform, "is_ci", lambda: True)
        assert kubeconform.run([Path("a.yaml")], _cfg()) == 1

    def test_missing_tool_warn_skips_locally(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(kubeconform, "_install_kubeconform", lambda: None)
        monkeypatch.setattr(kubeconform, "is_ci", lambda: False)
        assert kubeconform.run([Path("a.yaml")], _cfg()) == 0

    def test_tool_error_blocks_in_ci(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Non-zero exit with no parseable resources = a tool error (bad schema
        # location, unreadable input), not "all valid" - blocking gate fails.
        monkeypatch.setattr(
            kubeconform, "_install_kubeconform", lambda: "/usr/bin/kubeconform"
        )
        monkeypatch.setattr(
            kubeconform,
            "run_cmd",
            lambda *a, **k: SimpleNamespace(stdout="", returncode=2),
        )
        monkeypatch.setattr(kubeconform, "is_ci", lambda: True)
        assert kubeconform.run([Path("a.yaml")], _cfg()) == 1

    def test_exec_oserror_does_not_crash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            kubeconform, "_install_kubeconform", lambda: "/usr/bin/kubeconform"
        )

        def _boom(*a, **k):  # noqa: ANN002, ANN003
            raise OSError("exec failed")

        monkeypatch.setattr(kubeconform, "run_cmd", _boom)
        monkeypatch.setattr(kubeconform, "is_ci", lambda: False)
        assert kubeconform.run([Path("a.yaml")], _cfg()) == 0
