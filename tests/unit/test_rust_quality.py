# Project:   HyperI CI
# File:      tests/unit/test_rust_quality.py
# Purpose:   Tests for Rust quality checks, specifically feature_matrix
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from typing import Any

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.languages.rust.quality import _run_feature_matrix


def _make_config(fm: dict[str, Any] | None) -> CIConfig:
    raw: dict[str, Any] = {"quality": {"rust": {}}}
    if fm is not None:
        raw["quality"]["rust"]["feature_matrix"] = fm
    return CIConfig(_raw=raw)


class TestFeatureMatrixOptOut:
    """Opt-out validation — must always include a reason."""

    def test_opt_out_without_reason_fails(self) -> None:
        config = _make_config({"enabled": False})
        assert _run_feature_matrix(config) is False

    def test_opt_out_with_empty_reason_fails(self) -> None:
        config = _make_config({"enabled": False, "reason": "   "})
        assert _run_feature_matrix(config) is False

    def test_opt_out_with_reason_passes(self) -> None:
        config = _make_config({"enabled": False, "reason": "tracked in #42"})
        assert _run_feature_matrix(config) is True


class TestFeatureMatrixCommandConstruction:
    """Verify the cargo hack command is built correctly from config."""

    def test_default_invocation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default config: --each-feature --no-dev-deps check --lib + no-default-features pass."""
        captured_cmds: list[list[str]] = []

        def fake_which(name: str) -> str | None:
            return f"/usr/bin/{name}"

        def fake_run_tool(
            tool_name: str, cmd: list[str], mode: str, use_uvx: bool = False
        ) -> bool:
            captured_cmds.append(cmd)
            return True

        monkeypatch.setattr("hyperi_ci.languages.rust.quality.shutil.which", fake_which)
        monkeypatch.setattr("hyperi_ci.languages.rust.quality._run_tool", fake_run_tool)

        config = _make_config(None)
        assert _run_feature_matrix(config) is True

        assert len(captured_cmds) == 2
        # First: no-default-features pass
        assert captured_cmds[0] == ["cargo", "check", "--no-default-features", "--lib"]
        # Second: each-feature pass
        assert captured_cmds[1] == [
            "cargo",
            "hack",
            "--each-feature",
            "--no-dev-deps",
            "check",
            "--lib",
        ]

    def test_disable_no_default_features_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured_cmds: list[list[str]] = []
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality.shutil.which", lambda n: f"/usr/bin/{n}"
        )
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality._run_tool",
            lambda name, cmd, mode, use_uvx=False: captured_cmds.append(cmd) or True,
        )

        config = _make_config({"also_check_no_default_features": False})
        assert _run_feature_matrix(config) is True
        assert len(captured_cmds) == 1
        assert "--no-default-features" not in captured_cmds[0]

    def test_exclude_features(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured_cmds: list[list[str]] = []
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality.shutil.which", lambda n: f"/usr/bin/{n}"
        )
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality._run_tool",
            lambda name, cmd, mode, use_uvx=False: captured_cmds.append(cmd) or True,
        )

        config = _make_config({"exclude": ["_internal", "_testing"]})
        assert _run_feature_matrix(config) is True

        each_feature_cmd = captured_cmds[1]
        assert "--exclude-features" in each_feature_cmd
        idx = each_feature_cmd.index("--exclude-features")
        assert each_feature_cmd[idx + 1] == "_internal,_testing"

    def test_mutually_exclusive_pairs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured_cmds: list[list[str]] = []
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality.shutil.which", lambda n: f"/usr/bin/{n}"
        )
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality._run_tool",
            lambda name, cmd, mode, use_uvx=False: captured_cmds.append(cmd) or True,
        )

        config = _make_config(
            {
                "mutually_exclusive": [
                    ["native-tls", "rustls"],
                    ["tokio", "async-std"],
                ]
            }
        )
        assert _run_feature_matrix(config) is True

        each_feature_cmd = captured_cmds[1]
        # Should appear twice — once per pair
        assert each_feature_cmd.count("--mutually-exclusive-features") == 2
        flat = " ".join(each_feature_cmd)
        assert "native-tls,rustls" in flat
        assert "tokio,async-std" in flat

    def test_extra_args_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured_cmds: list[list[str]] = []
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality.shutil.which", lambda n: f"/usr/bin/{n}"
        )
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality._run_tool",
            lambda name, cmd, mode, use_uvx=False: captured_cmds.append(cmd) or True,
        )

        config = _make_config({"extra_args": ["--workspace", "--verbose"]})
        assert _run_feature_matrix(config) is True
        each_feature_cmd = captured_cmds[1]
        assert each_feature_cmd[-2:] == ["--workspace", "--verbose"]


class TestFeatureMatrixFailurePropagation:
    """When cargo hack returns non-zero, _run_feature_matrix returns False."""

    def test_returns_false_on_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality.shutil.which", lambda n: f"/usr/bin/{n}"
        )
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality._run_tool",
            lambda name, cmd, mode, use_uvx=False: False,
        )
        config = _make_config(None)
        assert _run_feature_matrix(config) is False
