# Project:   HyperI CI
# File:      tests/unit/test_rust_isolate_members.py
# Purpose:   build.rust.isolate_members gives named members a cargo invocation each
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from pathlib import Path
from typing import Any

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.languages.rust import quality as rust_quality
from hyperi_ci.languages.rust import test as rust_test
from hyperi_ci.languages.rust._manifest import PackageScope, package_scopes

MANIFEST = "hyperi_ci.languages.rust._manifest"
QUALITY = "hyperi_ci.languages.rust.quality"
TEST = "hyperi_ci.languages.rust.test"

ROOT_PACKAGE_WORKSPACE = """\
[package]
name = "root"
version = "0.1.0"
edition = "2024"

[workspace]
members = ["crates/heavy", "crates/light"]
"""

VIRTUAL_WORKSPACE = """\
[workspace]
members = ["crates/heavy", "crates/light"]
"""

DEFAULT_MEMBERS = ROOT_PACKAGE_WORKSPACE + 'default-members = [".", "crates/heavy"]\n'

SINGLE_CRATE = """\
[package]
name = "root"
version = "0.1.0"
edition = "2024"
"""

REST = PackageScope(
    args=("--workspace", "--exclude", "heavy"),
    label="--exclude heavy",
    has_lib=True,
    packages=("root", "light"),
)
HEAVY = PackageScope(
    args=("-p", "heavy"), label="-p heavy", has_lib=True, packages=("heavy",)
)


def _package(name: str, *kinds: str) -> dict[str, Any]:
    return {"name": name, "targets": [{"kind": [kind]} for kind in kinds]}


METADATA = {
    "packages": [
        _package("root", "lib", "bin"),
        _package("heavy", "lib"),
        _package("light", "lib"),
    ]
}


def _config(isolate: object = None, **raw: Any) -> CIConfig:
    data: dict[str, Any] = dict(raw)
    if isolate is not None:
        data["build"] = {"rust": {"isolate_members": isolate}}
    return CIConfig(_raw=data)


def _write_manifest(root: Path, text: str) -> None:
    (root / "Cargo.toml").write_text(text, encoding="utf-8", newline="\n")


class _Said:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.announced: list[str] = []

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def announce(self, msg: str, _title: str, **_kw: Any) -> None:
        self.announced.append(msg)


@pytest.fixture
def said(monkeypatch: pytest.MonkeyPatch) -> _Said:
    rec = _Said()
    monkeypatch.setattr(f"{MANIFEST}.warn", rec.warn)
    monkeypatch.setattr(f"{MANIFEST}.announce", rec.announce)
    return rec


@pytest.fixture
def metadata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[int]:
    """Serve METADATA as cargo metadata, and count the calls."""
    monkeypatch.chdir(tmp_path)
    calls: list[int] = []

    def fake(*_a: Any) -> dict[str, Any]:
        calls.append(1)
        return METADATA

    monkeypatch.setattr(f"{MANIFEST}.cargo_metadata", fake)
    return calls


class TestPackageScopes:
    @pytest.mark.parametrize("workspace", [True, False])
    def test_unset_is_one_scope_with_todays_switches(
        self, metadata: list[int], workspace: bool
    ) -> None:
        _write_manifest(Path.cwd(), ROOT_PACKAGE_WORKSPACE)
        scopes = package_scopes(_config(), workspace=workspace)
        assert scopes == [PackageScope(args=("--workspace",) if workspace else ())]
        assert metadata == [], "cargo metadata must not run when nothing is isolated"

    def test_empty_list_is_unset(self, metadata: list[int]) -> None:
        _write_manifest(Path.cwd(), ROOT_PACKAGE_WORKSPACE)
        assert package_scopes(_config([]), workspace=True) == [
            PackageScope(args=("--workspace",))
        ]

    def test_root_package_workspace_splits(self, metadata: list[int]) -> None:
        _write_manifest(Path.cwd(), ROOT_PACKAGE_WORKSPACE)
        assert package_scopes(_config(["heavy"]), workspace=True) == [REST, HEAVY]

    def test_virtual_workspace_splits_with_an_explicit_workspace(
        self, metadata: list[int]
    ) -> None:
        """`--exclude` needs `--workspace`, which a bare virtual command implies."""
        _write_manifest(Path.cwd(), VIRTUAL_WORKSPACE)
        assert package_scopes(_config(["heavy"]), workspace=False) == [REST, HEAVY]

    def test_several_members_are_excluded_together(self, metadata: list[int]) -> None:
        _write_manifest(Path.cwd(), ROOT_PACKAGE_WORKSPACE)
        scopes = package_scopes(_config(["heavy", "light", "heavy"]), workspace=True)
        assert [s.args for s in scopes] == [
            ("--workspace", "--exclude", "heavy", "--exclude", "light"),
            ("-p", "heavy"),
            ("-p", "light"),
        ]

    def test_isolating_every_member_leaves_no_workspace_run(
        self, metadata: list[int]
    ) -> None:
        _write_manifest(Path.cwd(), ROOT_PACKAGE_WORKSPACE)
        scopes = package_scopes(_config(["root", "heavy", "light"]), workspace=True)
        assert [s.args for s in scopes] == [
            ("-p", "root"),
            ("-p", "heavy"),
            ("-p", "light"),
        ]

    def test_lib_targets_are_read_per_scope(
        self, monkeypatch: pytest.MonkeyPatch, metadata: list[int]
    ) -> None:
        _write_manifest(Path.cwd(), ROOT_PACKAGE_WORKSPACE)
        bins_only = {
            "packages": [
                _package("root", "bin"),
                _package("heavy", "lib"),
                _package("light", "bin"),
            ]
        }
        monkeypatch.setattr(f"{MANIFEST}.cargo_metadata", lambda *_a: bins_only)
        rest, heavy = package_scopes(_config(["heavy"]), workspace=True)
        assert rest.has_lib is False
        assert heavy.has_lib is True

    def test_unknown_name_is_announced_and_ignored(
        self, metadata: list[int], said: _Said
    ) -> None:
        _write_manifest(Path.cwd(), ROOT_PACKAGE_WORKSPACE)
        scopes = package_scopes(_config(["heavy", "typo"]), workspace=True)
        assert scopes == [REST, HEAVY]
        assert len(said.announced) == 1
        assert "typo" in said.announced[0]
        assert "heavy, light, root" in said.announced[0]

    def test_only_unknown_names_leave_the_pass_whole(
        self, metadata: list[int], said: _Said
    ) -> None:
        _write_manifest(Path.cwd(), ROOT_PACKAGE_WORKSPACE)
        scopes = package_scopes(_config(["typo"]), workspace=True)
        assert scopes == [PackageScope(args=("--workspace",))]
        assert len(said.announced) == 1

    def test_default_members_keeps_its_own_scope(
        self, metadata: list[int], said: _Said
    ) -> None:
        _write_manifest(Path.cwd(), DEFAULT_MEMBERS)
        assert package_scopes(_config(["heavy"]), workspace=False) == [PackageScope()]
        assert "default-members" in said.warnings[0]

    def test_single_crate_is_not_split(self, metadata: list[int], said: _Said) -> None:
        _write_manifest(Path.cwd(), SINGLE_CRATE)
        assert package_scopes(_config(["root"]), workspace=False) == [PackageScope()]
        assert "not a cargo workspace" in said.warnings[0]
        assert metadata == []

    def test_metadata_failure_leaves_the_pass_whole(
        self, monkeypatch: pytest.MonkeyPatch, metadata: list[int], said: _Said
    ) -> None:
        _write_manifest(Path.cwd(), ROOT_PACKAGE_WORKSPACE)
        monkeypatch.setattr(f"{MANIFEST}.cargo_metadata", lambda *_a: None)
        scopes = package_scopes(_config(["heavy"]), workspace=True)
        assert scopes == [PackageScope(args=("--workspace",))]
        assert "cargo metadata failed" in said.warnings[0]

    @pytest.mark.parametrize("value", ["heavy", [""], [1], {"heavy": True}])
    def test_malformed_value_is_warned_and_ignored(
        self, metadata: list[int], said: _Said, value: object
    ) -> None:
        _write_manifest(Path.cwd(), ROOT_PACKAGE_WORKSPACE)
        scopes = package_scopes(_config(value), workspace=True)
        assert scopes == [PackageScope(args=("--workspace",))]
        assert "not a list of package names" in said.warnings[0]


class _Tools:
    """Records every command the quality stage hands to a tool, in order."""

    def __init__(self) -> None:
        self.commands: dict[str, list[str]] = {}
        self.docs: list[list[str]] = []

    def run_tool(
        self, tool_name: str, cmd: list[str], mode: str, use_uvx: bool = False
    ) -> bool:
        self.commands[tool_name] = cmd
        return True

    def matrix_pass(
        self, tool_name: str, cmd: list[str], _first_label: str, mode: str
    ) -> bool:
        return self.run_tool(tool_name, cmd, mode)

    def doc(self, cmd: list[str], **_kw: Any) -> Any:
        # subprocess is one module, so the manifest backup's git call lands here too.
        if cmd[:2] == ["cargo", "doc"]:
            self.docs.append(cmd)
        return type("Result", (), {"stdout": "", "stderr": "", "returncode": 1})()


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch, metadata: list[int]) -> _Tools:
    Path("deny.toml").write_text("", encoding="utf-8")
    rec = _Tools()
    monkeypatch.setattr(f"{QUALITY}._run_tool", rec.run_tool)
    monkeypatch.setattr(f"{QUALITY}._run_matrix_pass", rec.matrix_pass)
    monkeypatch.setattr(f"{QUALITY}.subprocess.run", rec.doc)
    monkeypatch.setattr(f"{QUALITY}.shutil.which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(f"{QUALITY}._has_lib_target", lambda *_a: True)
    monkeypatch.setattr(f"{QUALITY}._package_lib_map", lambda *_a: {})
    monkeypatch.setattr(f"{QUALITY}.osv_scanner.run", lambda *_a, **_k: True)
    monkeypatch.setattr(f"{QUALITY}.cargo_flags.run", lambda *_a: 0)
    return rec


CLIPPY_DENY = ["--", "-D", "warnings", "-D", "clippy::dbg_macro"]


class TestQualitySplits:
    def test_clippy_runs_the_rest_then_each_isolated_member(
        self, tools: _Tools
    ) -> None:
        _write_manifest(Path.cwd(), ROOT_PACKAGE_WORKSPACE)
        assert rust_quality.run(_config(["heavy"])) == 0
        cmds = tools.commands
        assert cmds["clippy src (all, --exclude heavy)"] == [
            "cargo",
            "clippy",
            "--workspace",
            "--exclude",
            "heavy",
            "--lib",
            "--bins",
            "--all-features",
            *CLIPPY_DENY,
        ]
        assert cmds["clippy src (all, -p heavy)"] == [
            "cargo",
            "clippy",
            "-p",
            "heavy",
            "--lib",
            "--bins",
            "--all-features",
            *CLIPPY_DENY,
        ]
        assert cmds["clippy tests (all, --exclude heavy)"][:7] == [
            "cargo",
            "clippy",
            "--workspace",
            "--exclude",
            "heavy",
            "--tests",
            "--benches",
        ]
        assert cmds["clippy tests (all, -p heavy)"][:6] == [
            "cargo",
            "clippy",
            "-p",
            "heavy",
            "--tests",
            "--benches",
        ]
        order = list(cmds)
        assert order.index("clippy src (all, --exclude heavy)") < order.index(
            "clippy src (all, -p heavy)"
        )
        assert "clippy src (all)" not in cmds

    def test_deny_and_the_cargo_hack_pass_stay_whole(self, tools: _Tools) -> None:
        """cargo deny compiles nothing, and cargo-hack runs one cargo per package."""
        _write_manifest(Path.cwd(), ROOT_PACKAGE_WORKSPACE)
        rust_quality.run(_config(["heavy"]))
        assert tools.commands["cargo deny"] == ["cargo", "deny", "--workspace", "check"]
        assert tools.commands["feature_matrix (each-feature)"][:6] == [
            "cargo",
            "hack",
            "--each-feature",
            "--no-dev-deps",
            "clippy",
            "--workspace",
        ]
        assert "--exclude" not in tools.commands["feature_matrix (each-feature)"]

    def test_no_default_features_pass_splits(self, tools: _Tools) -> None:
        _write_manifest(Path.cwd(), ROOT_PACKAGE_WORKSPACE)
        rust_quality.run(_config(["heavy"]))
        rest = tools.commands["feature_matrix (no-default-features, --exclude heavy)"]
        heavy = tools.commands["feature_matrix (no-default-features, -p heavy)"]
        assert rest[:7] == [
            "cargo",
            "clippy",
            "--no-default-features",
            "--workspace",
            "--exclude",
            "heavy",
            "--lib",
        ]
        assert heavy[:6] == [
            "cargo",
            "clippy",
            "--no-default-features",
            "-p",
            "heavy",
            "--lib",
        ]

    def test_repo_scope_in_extra_args_is_not_split(self, tools: _Tools) -> None:
        _write_manifest(Path.cwd(), ROOT_PACKAGE_WORKSPACE)
        config = _config(
            ["heavy"], quality={"rust": {"feature_matrix": {"extra_args": ["-p", "x"]}}}
        )
        rust_quality.run(config)
        assert tools.commands["feature_matrix (no-default-features)"][:4] == [
            "cargo",
            "clippy",
            "--no-default-features",
            "--lib",
        ]

    def test_rustdoc_runs_once_per_scope(self, tools: _Tools) -> None:
        _write_manifest(Path.cwd(), ROOT_PACKAGE_WORKSPACE)
        rust_quality.run(_config(["heavy"]))
        assert [cmd[:6] for cmd in tools.docs] == [
            ["cargo", "doc", "--workspace", "--exclude", "heavy", "--no-deps"],
            ["cargo", "doc", "-p", "heavy", "--no-deps", "--lib"],
        ]

    def test_unset_leaves_every_command_as_it_was(self, tools: _Tools) -> None:
        _write_manifest(Path.cwd(), ROOT_PACKAGE_WORKSPACE)
        rust_quality.run(_config())
        assert all("--exclude" not in cmd for cmd in tools.commands.values())
        assert tools.commands["clippy src (all)"][:3] == [
            "cargo",
            "clippy",
            "--workspace",
        ]
        assert tools.docs == [
            ["cargo", "doc", "--workspace", "--no-deps", "--lib", "--all-features"]
        ]


class TestRustdocAcrossScopes:
    def test_counts_are_summed_and_a_bin_only_member_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said: list[str] = []
        ran: list[list[str]] = []
        stderr = (
            "warning: unresolved link to `Foo`\n"
            "warning: `root` (lib doc) generated 1 warning\n"
        )

        def doc(cmd: list[str], **_kw: Any) -> Any:
            ran.append(cmd)
            return type("Result", (), {"stdout": "", "stderr": stderr})()

        monkeypatch.setattr(f"{QUALITY}.subprocess.run", doc)
        monkeypatch.setattr(f"{QUALITY}.shutil.which", lambda n: f"/usr/bin/{n}")
        monkeypatch.setattr(f"{QUALITY}._has_lib_target", lambda *_a: True)
        monkeypatch.setattr(f"{QUALITY}.warn", said.append)
        scopes = [REST, HEAVY, PackageScope(args=("-p", "tool"), has_lib=False)]
        rust_quality._run_rustdoc_hint(_config(), scopes=scopes)
        assert len(ran) == 2
        assert "2 doc warning(s)" in said[0]


class _Streams:
    def __init__(self) -> None:
        self.streamed: list[list[str]] = []
        self.ran: list[list[str]] = []

    def stream(self, cmd: list[str], **_kw: Any) -> Any:
        self.streamed.append(cmd)
        return 0, ""

    def run(self, cmd: list[str], **_kw: Any) -> Any:
        self.ran.append(cmd)
        return type("Result", (), {"returncode": 0})()


@pytest.fixture
def streams(monkeypatch: pytest.MonkeyPatch, metadata: list[int]) -> _Streams:
    rec = _Streams()
    monkeypatch.setattr(f"{TEST}.stream_cmd", rec.stream)
    monkeypatch.setattr(f"{TEST}.run_cmd", rec.run)
    monkeypatch.setattr(f"{TEST}.announce_tier", lambda *_a: None)
    return rec


def _only_tool(monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    monkeypatch.setattr(
        f"{TEST}.shutil.which", lambda name: "/usr/bin/x" if name == tool else None
    )
    monkeypatch.setattr(f"{TEST}._has_nextest", lambda: tool == "cargo-nextest")


def _test_config(
    isolate: object = None, coverage: bool = False, **test: Any
) -> CIConfig:
    return _config(isolate, test={"coverage": coverage, **test})


class TestTestSplits:
    @pytest.mark.parametrize(
        ("tool", "prefix"),
        [
            ("cargo-nextest", ["cargo", "nextest", "run"]),
            ("nothing", ["cargo", "test"]),
        ],
    )
    def test_runner_runs_the_rest_then_the_member(
        self,
        monkeypatch: pytest.MonkeyPatch,
        streams: _Streams,
        tool: str,
        prefix: list[str],
    ) -> None:
        _write_manifest(Path.cwd(), ROOT_PACKAGE_WORKSPACE)
        _only_tool(monkeypatch, tool)
        assert rust_test.run(_test_config(["heavy"]), extra_env={}) == 0
        assert streams.streamed == [
            [*prefix, "--workspace", "--exclude", "heavy", "--all-features"],
            [*prefix, "-p", "heavy", "--all-features"],
        ]

    def test_rust_tier_subset_splits(
        self, monkeypatch: pytest.MonkeyPatch, streams: _Streams
    ) -> None:
        _write_manifest(Path.cwd(), ROOT_PACKAGE_WORKSPACE)
        _only_tool(monkeypatch, "cargo-nextest")
        config = _test_config(["heavy"], rust={"tier": "unit"})
        assert rust_test.run(config, extra_env={}) == 0
        assert [cmd[3:] for cmd in streams.streamed] == [
            ["--workspace", "--exclude", "heavy", "--all-features", "--lib"],
            ["-p", "heavy", "--all-features", "--lib"],
        ]

    def test_a_failing_rest_stops_before_the_member(
        self, monkeypatch: pytest.MonkeyPatch, streams: _Streams
    ) -> None:
        _write_manifest(Path.cwd(), ROOT_PACKAGE_WORKSPACE)
        _only_tool(monkeypatch, "cargo-nextest")
        monkeypatch.setattr(f"{TEST}.stream_cmd", lambda cmd, **_k: (101, ""))
        assert rust_test.run(_test_config(["heavy"]), extra_env={}) == 101

    def test_llvm_cov_merges_the_runs_into_one_report(
        self, monkeypatch: pytest.MonkeyPatch, streams: _Streams
    ) -> None:
        _write_manifest(Path.cwd(), ROOT_PACKAGE_WORKSPACE)
        _only_tool(monkeypatch, "cargo-llvm-cov")
        monkeypatch.setattr(f"{TEST}._has_nextest", lambda: True)
        assert rust_test.run(_test_config(["heavy"], coverage=True), extra_env={}) == 0
        assert streams.streamed == [
            [
                "cargo",
                "llvm-cov",
                "nextest",
                "--no-report",
                "--workspace",
                "--exclude-from-test",
                "heavy",
                "--all-features",
            ],
            [
                "cargo",
                "llvm-cov",
                "nextest",
                "--no-report",
                "-p",
                "heavy",
                "--all-features",
            ],
        ]
        every_package = ["-p", "root", "-p", "light", "-p", "heavy"]
        assert streams.ran == [
            ["cargo", "llvm-cov", "clean", "--workspace"],
            [
                "cargo",
                "llvm-cov",
                "report",
                "--lcov",
                "--output-path",
                "test-results/lcov.info",
                *every_package,
            ],
            [
                "cargo",
                "llvm-cov",
                "report",
                "--html",
                "--output-dir",
                "test-results/coverage-html",
                *every_package,
            ],
        ]

    def test_tarpaulin_writes_the_member_report_apart(
        self, monkeypatch: pytest.MonkeyPatch, streams: _Streams
    ) -> None:
        _write_manifest(Path.cwd(), ROOT_PACKAGE_WORKSPACE)
        _only_tool(monkeypatch, "cargo-tarpaulin")
        assert rust_test.run(_test_config(["heavy"], coverage=True), extra_env={}) == 0
        out_dirs = [cmd[cmd.index("--output-dir") + 1] for cmd in streams.streamed]
        assert out_dirs == ["test-results", "test-results/heavy"]
        assert streams.streamed[0][-4:] == [
            "--workspace",
            "--exclude",
            "heavy",
            "--all-features",
        ]
        assert streams.streamed[1][-3:] == ["-p", "heavy", "--all-features"]

    @pytest.mark.parametrize("tool", ["cargo-nextest", "cargo-llvm-cov"])
    def test_unset_is_one_invocation(
        self, monkeypatch: pytest.MonkeyPatch, streams: _Streams, tool: str
    ) -> None:
        _write_manifest(Path.cwd(), ROOT_PACKAGE_WORKSPACE)
        _only_tool(monkeypatch, tool)
        config = _test_config(coverage=tool == "cargo-llvm-cov")
        assert rust_test.run(config, extra_env={}) == 0
        assert len(streams.streamed) == 1
        assert "--no-report" not in streams.streamed[0]
        assert "--exclude" not in streams.streamed[0]
