# Project:   HyperI CI
# File:      tests/unit/test_rust_workspace.py
# Purpose:   Rust test and lint commands cover every root-package workspace member
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from pathlib import Path
from typing import Any

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.languages.rust import quality as rust_quality
from hyperi_ci.languages.rust import test as rust_test
from hyperi_ci.languages.rust._manifest import is_root_package_workspace
from hyperi_ci.languages.rust.test import _build_test_cmd, _run_coverage
from hyperi_ci.languages.tiering import SuiteTier

MODULE = "hyperi_ci.languages.rust.test"
QUALITY = "hyperi_ci.languages.rust.quality"

ROOT_PACKAGE_WORKSPACE = """\
[package]
name = "root"
version = "0.1.0"
edition = "2024"

[workspace]
members = ["member"]
"""

VIRTUAL_WORKSPACE = """\
[workspace]
members = ["a", "b"]
resolver = "3"
"""

SINGLE_CRATE = """\
[package]
name = "solo"
version = "0.1.0"
edition = "2024"
"""

DEFAULT_MEMBERS = ROOT_PACKAGE_WORKSPACE + 'default-members = [".", "member"]\n'

# Every shape where a bare cargo command already covers what the repo means.
UNCHANGED_SHAPES = [
    pytest.param(VIRTUAL_WORKSPACE, id="virtual"),
    pytest.param(SINGLE_CRATE, id="single-crate"),
    pytest.param(DEFAULT_MEMBERS, id="default-members"),
]

LLVM_COV_PREFIX = ["cargo", "llvm-cov", "nextest", "--lcov", "--output-path"]
TARPAULIN_PREFIX = [
    "cargo",
    "tarpaulin",
    "--out",
    "Lcov",
    "--out",
    "Html",
    "--output-dir",
    "test-results",
]


def _write_manifest(root: Path, text: str) -> None:
    (root / "Cargo.toml").write_text(text, encoding="utf-8", newline="\n")


class TestDetection:
    """Only a root `[package]` beside `[workspace]` narrows cargo's default."""

    def test_root_package_workspace(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, ROOT_PACKAGE_WORKSPACE)
        assert is_root_package_workspace(tmp_path) is True

    def test_virtual_workspace(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, VIRTUAL_WORKSPACE)
        assert is_root_package_workspace(tmp_path) is False

    def test_single_crate(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, SINGLE_CRATE)
        assert is_root_package_workspace(tmp_path) is False

    def test_default_members_is_the_repos_own_choice(self, tmp_path: Path) -> None:
        """`--workspace` would override `default-members`, so it is left alone."""
        _write_manifest(tmp_path, DEFAULT_MEMBERS)
        assert is_root_package_workspace(tmp_path) is False

    def test_no_manifest(self, tmp_path: Path) -> None:
        assert is_root_package_workspace(tmp_path) is False

    def test_malformed_manifest_leaves_the_argv_alone(self, tmp_path: Path) -> None:
        """cargo reports a broken manifest itself, on the unchanged command."""
        _write_manifest(tmp_path, "[package\nname = ")
        assert is_root_package_workspace(tmp_path) is False

    def test_undecodable_bytes_do_not_raise(self, tmp_path: Path) -> None:
        """A stray non-UTF-8 byte in a comment must not take the stage down."""
        raw = b"# caf\xe9\n" + ROOT_PACKAGE_WORKSPACE.encode("utf-8")
        (tmp_path / "Cargo.toml").write_bytes(raw)
        assert is_root_package_workspace(tmp_path) is True


class TestCommandTakesWorkspace:
    @pytest.mark.parametrize(
        ("runner", "prefix"),
        [("nextest", ["cargo", "nextest", "run"]), ("cargo", ["cargo", "test"])],
    )
    def test_core(self, runner: str, prefix: list[str]) -> None:
        cmd = _build_test_cmd("all", runner=runner, workspace=True)
        assert cmd == [*prefix, "--workspace", "--all-features"]

    def test_full_nextest(self) -> None:
        cmd = _build_test_cmd(
            "all", runner="nextest", test_tier=SuiteTier.FULL, workspace=True
        )
        assert cmd == [
            "cargo",
            "nextest",
            "run",
            "--workspace",
            "--all-features",
            "--run-ignored",
            "all",
            "--ignore-default-filter",
        ]

    def test_full_cargo_keeps_workspace_before_the_separator(self) -> None:
        cmd = _build_test_cmd(
            "all", runner="cargo", test_tier=SuiteTier.FULL, workspace=True
        )
        assert cmd == [
            "cargo",
            "test",
            "--workspace",
            "--all-features",
            "--",
            "--include-ignored",
        ]

    def test_rust_tier_subset(self) -> None:
        cmd = _build_test_cmd(
            "default", rust_tier="unit", runner="nextest", workspace=True
        )
        assert cmd == ["cargo", "nextest", "run", "--workspace", "--lib"]

    @pytest.mark.parametrize("runner", ["nextest", "cargo"])
    def test_default_is_unchanged(self, runner: str) -> None:
        assert "--workspace" not in _build_test_cmd("all", runner=runner)


class _Streams:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def stream(self, cmd: list[str], **_kw: Any) -> Any:
        self.commands.append(cmd)
        return 0, ""


@pytest.fixture
def streams(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Streams:
    monkeypatch.chdir(tmp_path)
    rec = _Streams()
    monkeypatch.setattr(f"{MODULE}.stream_cmd", rec.stream)
    monkeypatch.setattr(f"{MODULE}.announce_tier", lambda *_a: None)
    monkeypatch.setattr(f"{MODULE}.subprocess.run", lambda *_a, **_k: None)
    return rec


def _only_tool(monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    monkeypatch.setattr(
        f"{MODULE}.shutil.which", lambda name: "/usr/bin/x" if name == tool else None
    )


class TestCoverageTakesWorkspace:
    def test_llvm_cov_nextest(
        self, monkeypatch: pytest.MonkeyPatch, streams: _Streams
    ) -> None:
        _only_tool(monkeypatch, "cargo-llvm-cov")
        _run_coverage("all", runner="nextest", workspace=True)
        assert streams.commands == [
            [
                *LLVM_COV_PREFIX,
                "test-results/lcov.info",
                "--workspace",
                "--all-features",
            ]
        ]

    def test_llvm_cov_on_libtest_full(
        self, monkeypatch: pytest.MonkeyPatch, streams: _Streams
    ) -> None:
        _only_tool(monkeypatch, "cargo-llvm-cov")
        _run_coverage("all", runner="cargo", test_tier=SuiteTier.FULL, workspace=True)
        assert streams.commands[0][-4:] == [
            "--workspace",
            "--all-features",
            "--",
            "--include-ignored",
        ]

    def test_tarpaulin(
        self, monkeypatch: pytest.MonkeyPatch, streams: _Streams
    ) -> None:
        _only_tool(monkeypatch, "cargo-tarpaulin")
        _run_coverage("all", runner="cargo", workspace=True)
        assert streams.commands == [
            [*TARPAULIN_PREFIX, "--workspace", "--all-features"]
        ]


# Every command the handler can build, per runner and coverage tool, with the
# manifest deciding whether `--workspace` goes in.
_PATHS = [
    pytest.param("cargo-nextest", False, ["cargo", "nextest", "run"], id="nextest"),
    pytest.param("nothing", False, ["cargo", "test"], id="cargo-test"),
    pytest.param("cargo-llvm-cov", True, LLVM_COV_PREFIX, id="llvm-cov"),
    pytest.param("cargo-tarpaulin", True, TARPAULIN_PREFIX, id="tarpaulin"),
]


class TestRunReadsTheManifest:
    @staticmethod
    def _run(
        monkeypatch: pytest.MonkeyPatch, tool: str, coverage: bool, tier: str
    ) -> int:
        _only_tool(monkeypatch, tool)
        monkeypatch.setattr(f"{MODULE}._has_nextest", lambda: tool != "nothing")
        config = CIConfig(_raw={"test": {"coverage": coverage}})
        return rust_test.run(config, extra_env={"TEST_TIER": tier})

    @pytest.mark.parametrize("tier", ["core", "full"])
    @pytest.mark.parametrize(("tool", "coverage", "prefix"), _PATHS)
    def test_root_package_workspace_tests_every_member(
        self,
        monkeypatch: pytest.MonkeyPatch,
        streams: _Streams,
        tool: str,
        coverage: bool,
        prefix: list[str],
        tier: str,
    ) -> None:
        _write_manifest(Path.cwd(), ROOT_PACKAGE_WORKSPACE)
        assert self._run(monkeypatch, tool, coverage, tier) == 0
        assert len(streams.commands) == 1
        cmd = streams.commands[0]
        assert cmd[: len(prefix)] == prefix
        assert cmd.count("--workspace") == 1
        assert cmd.index("--workspace") < cmd.index("--all-features")

    @pytest.mark.parametrize("manifest", UNCHANGED_SHAPES)
    @pytest.mark.parametrize(("tool", "coverage", "prefix"), _PATHS)
    def test_other_shapes_keep_the_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
        streams: _Streams,
        tool: str,
        coverage: bool,
        prefix: list[str],
        manifest: str,
    ) -> None:
        _write_manifest(Path.cwd(), manifest)
        assert self._run(monkeypatch, tool, coverage, "core") == 0
        assert streams.commands[0][: len(prefix)] == prefix
        assert "--workspace" not in streams.commands[0]


CLIPPY_DENY = ["--", "-D", "warnings", "-D", "clippy::dbg_macro"]


class _Tools:
    """Records every command the quality stage hands to a tool."""

    def __init__(self) -> None:
        self.commands: dict[str, list[str]] = {}

    def run_tool(
        self, tool_name: str, cmd: list[str], mode: str, use_uvx: bool = False
    ) -> bool:
        self.commands[tool_name] = cmd
        return True

    def doc(self, cmd: list[str], **_kw: Any) -> Any:
        self.commands["cargo doc"] = cmd
        return type("Result", (), {"stdout": "", "stderr": ""})()


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Tools:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "deny.toml").write_text("", encoding="utf-8")
    rec = _Tools()
    monkeypatch.setattr(f"{QUALITY}._run_tool", rec.run_tool)
    monkeypatch.setattr(f"{QUALITY}.subprocess.run", rec.doc)
    monkeypatch.setattr(f"{QUALITY}.shutil.which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(f"{QUALITY}._has_lib_target", lambda *_a: True)
    monkeypatch.setattr(f"{QUALITY}._package_lib_map", lambda *_a: {})
    monkeypatch.setattr(f"{QUALITY}.osv_scanner.run", lambda *_a, **_k: True)
    monkeypatch.setattr(f"{QUALITY}.cargo_flags.run", lambda *_a: 0)
    return rec


def _quality_config(**feature_matrix: Any) -> CIConfig:
    return CIConfig(_raw={"quality": {"rust": {"feature_matrix": feature_matrix}}})


class TestLintReadsTheManifest:
    """clippy, cargo deny, the feature matrix and rustdoc are package-scoped."""

    def test_root_package_workspace_is_linted_whole(self, tools: _Tools) -> None:
        _write_manifest(Path.cwd(), ROOT_PACKAGE_WORKSPACE)
        assert rust_quality.run(_quality_config()) == 0
        cmds = tools.commands
        assert cmds["clippy src (all)"] == [
            "cargo",
            "clippy",
            "--workspace",
            "--lib",
            "--bins",
            "--all-features",
            *CLIPPY_DENY,
        ]
        assert cmds["clippy tests (all)"][:6] == [
            "cargo",
            "clippy",
            "--workspace",
            "--tests",
            "--benches",
            "--all-features",
        ]
        assert cmds["cargo deny"] == ["cargo", "deny", "--workspace", "check"]
        assert cmds["feature_matrix (no-default-features)"] == [
            "cargo",
            "check",
            "--no-default-features",
            "--workspace",
            "--lib",
        ]
        assert cmds["feature_matrix (each-feature)"] == [
            "cargo",
            "hack",
            "--each-feature",
            "--no-dev-deps",
            "check",
            "--workspace",
            "--lib",
        ]
        assert cmds["cargo doc"][:3] == ["cargo", "doc", "--workspace"]

    def test_fmt_and_audit_are_already_workspace_wide(self, tools: _Tools) -> None:
        _write_manifest(Path.cwd(), ROOT_PACKAGE_WORKSPACE)
        rust_quality.run(_quality_config())
        assert tools.commands["cargo fmt"] == ["cargo", "fmt", "--check"]
        assert tools.commands["cargo audit"] == ["cargo", "audit"]

    @pytest.mark.parametrize("manifest", UNCHANGED_SHAPES)
    def test_other_shapes_keep_the_default(self, tools: _Tools, manifest: str) -> None:
        _write_manifest(Path.cwd(), manifest)
        assert rust_quality.run(_quality_config()) == 0
        assert all("--workspace" not in cmd for cmd in tools.commands.values())
        assert tools.commands["clippy src (all)"] == [
            "cargo",
            "clippy",
            "--lib",
            "--bins",
            "--all-features",
            *CLIPPY_DENY,
        ]
        assert tools.commands["cargo deny"] == ["cargo", "deny", "check"]


class TestFeatureMatrixScope:
    """A scope the repo already chose is never doubled or widened."""

    def _each_feature(self, tools: _Tools, **feature_matrix: Any) -> list[str]:
        assert rust_quality._run_feature_matrix(
            _quality_config(**feature_matrix), workspace=True
        )
        return tools.commands["feature_matrix (each-feature)"]

    def test_extra_workspace_is_not_doubled(self, tools: _Tools) -> None:
        """cargo-hack rejects a second `--workspace` outright."""
        cmd = self._each_feature(tools, extra_args=["--workspace"])
        assert cmd.count("--workspace") == 1

    @pytest.mark.parametrize(
        "extra", [["-p", "one"], ["--package", "one"], ["--package=one"], ["--all"]]
    )
    def test_extra_package_scope_is_kept(self, tools: _Tools, extra: list[str]) -> None:
        cmd = self._each_feature(tools, extra_args=extra)
        assert "--workspace" not in cmd
        assert cmd[-len(extra) :] == extra

    def test_mixed_workspace_keeps_per_member_scoping(
        self, monkeypatch: pytest.MonkeyPatch, tools: _Tools
    ) -> None:
        monkeypatch.setattr(
            f"{QUALITY}._package_lib_map", lambda *_a: {"lib": True, "tool": False}
        )
        rec: list[list[str]] = []
        monkeypatch.setattr(
            f"{QUALITY}._run_tool", lambda _n, cmd, *_a, **_k: rec.append(cmd) or True
        )
        assert rust_quality._run_feature_matrix(_quality_config(), workspace=True)
        assert all("--workspace" not in cmd for cmd in rec)
        assert ["-p", "lib"] == rec[0][3:5]


class TestRustdocCountsEveryCrate:
    def test_one_summary_line_per_crate_is_not_a_finding(
        self, monkeypatch: pytest.MonkeyPatch, tools: _Tools
    ) -> None:
        said: list[str] = []
        stderr = (
            "warning: unresolved link to `Foo`\n"
            "warning: `root` (lib doc) generated 1 warning\n"
            "warning: unresolved link to `Bar`\n"
            "warning: `member` (lib doc) generated 1 warning\n"
        )
        monkeypatch.setattr(
            f"{QUALITY}.subprocess.run",
            lambda *_a, **_k: type("Result", (), {"stdout": "", "stderr": stderr})(),
        )
        monkeypatch.setattr(f"{QUALITY}.warn", said.append)
        rust_quality._run_rustdoc_hint(_quality_config(), workspace=True)
        assert len(said) == 1
        assert "2 doc warning(s)" in said[0]
