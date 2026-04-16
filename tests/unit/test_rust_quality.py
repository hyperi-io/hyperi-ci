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
from hyperi_ci.languages.rust.quality import _run_feature_matrix, _run_rustdoc_hint


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


class TestRustdocHint:
    """Non-blocking rustdoc hint emits a single concise warning."""

    def test_disabled_via_config_skips_entirely(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called = []

        def fake_run(*args: object, **kwargs: object) -> object:
            called.append(args)
            raise AssertionError("subprocess should not run when disabled")

        monkeypatch.setattr("hyperi_ci.languages.rust.quality.subprocess.run", fake_run)

        raw = {"quality": {"rust": {"rustdoc_hint": {"enabled": False}}}}
        config = CIConfig(_raw=raw)
        _run_rustdoc_hint(config)  # should not raise
        assert called == []

    def test_zero_warnings_emits_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        warnings_emitted: list[str] = []

        class FakeResult:
            stdout = "Documenting hyperi-rustlib v2.5.1\nFinished\n"
            stderr = ""

        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality.shutil.which", lambda n: f"/usr/bin/{n}"
        )
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality.subprocess.run",
            lambda *a, **kw: FakeResult(),
        )
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality.warn", warnings_emitted.append
        )

        config = _make_config(None)
        _run_rustdoc_hint(config)
        assert warnings_emitted == []

    def test_warnings_emit_single_concise_message_with_urls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        warnings_emitted: list[str] = []

        class FakeResult:
            stdout = ""
            stderr = (
                "warning: unresolved link to `Foo`\n"
                "  --> src/lib.rs:42:5\n"
                "warning: bare URL not hyperlink\n"
                "  --> src/lib.rs:50:1\n"
                "warning: `mycrate` (lib doc) generated 2 warnings\n"
            )

        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality.shutil.which", lambda n: f"/usr/bin/{n}"
        )
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality.subprocess.run",
            lambda *a, **kw: FakeResult(),
        )
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality.warn", warnings_emitted.append
        )

        config = _make_config(None)
        _run_rustdoc_hint(config)

        # Exactly one summary line — not spam
        assert len(warnings_emitted) == 1
        msg = warnings_emitted[0]
        # Contains correct count (2 actual warnings, summary line subtracted)
        assert "2 doc warning" in msg
        # References the standards URLs
        assert "doc.rust-lang.org/rustdoc" in msg
        assert "api-guidelines" in msg
        assert "hyperi-ai/standards" in msg
