# Project:   HyperI CI
# File:      tests/unit/test_rust_quality.py
# Purpose:   Tests for Rust quality checks, specifically feature_matrix
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

import subprocess
from pathlib import Path
from typing import Any

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.languages.rust.quality import (
    _deny_warnings,
    _feature_set_findings,
    _run_feature_matrix,
    _run_matrix_pass,
    _run_rustdoc_hint,
)


def _make_config(fm: dict[str, Any] | None) -> CIConfig:
    raw: dict[str, Any] = {"quality": {"rust": {}}}
    if fm is not None:
        raw["quality"]["rust"]["feature_matrix"] = fm
    return CIConfig(_raw=raw)


class TestFeatureMatrixOptOut:
    """Opt-out validation -- must always include a reason."""

    def test_opt_out_without_reason_fails(self) -> None:
        config = _make_config({"enabled": False})
        assert _run_feature_matrix(config) is False

    def test_opt_out_with_empty_reason_fails(self) -> None:
        config = _make_config({"enabled": False, "reason": "   "})
        assert _run_feature_matrix(config) is False

    def test_opt_out_with_reason_passes(self) -> None:
        config = _make_config({"enabled": False, "reason": "tracked in #42"})
        assert _run_feature_matrix(config) is True


@pytest.fixture
def _force_lib_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mark "lib target present" for tests that assume the original
    behaviour (cargo invocations include ``--lib``). Bin-only behaviour
    is covered by dedicated tests below.

    Used as ``@pytest.mark.usefixtures("_force_lib_target")`` on classes
    that pre-date the ``_has_lib_target`` gate.
    """
    monkeypatch.setattr(
        "hyperi_ci.languages.rust.quality._has_lib_target", lambda *a, **kw: True
    )


@pytest.mark.usefixtures("_force_lib_target")
class TestFeatureMatrixCommandConstruction:
    """Verify the cargo hack command is built correctly from config."""

    def test_default_invocation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default config: --each-feature --no-dev-deps check --lib + no-default-features pass."""
        captured_cmds: list[list[str]] = []

        def fake_which(name: str) -> str | None:
            return f"/usr/bin/{name}"

        def fake_run_pass(
            tool_name: str, cmd: list[str], label: str, warnings_mode: str
        ) -> bool:
            captured_cmds.append(cmd)
            return True

        monkeypatch.setattr("hyperi_ci.languages.rust.quality.shutil.which", fake_which)
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality._run_matrix_pass", fake_run_pass
        )

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
            "hyperi_ci.languages.rust.quality._run_matrix_pass",
            lambda name, cmd, label, mode: captured_cmds.append(cmd) or True,
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
            "hyperi_ci.languages.rust.quality._run_matrix_pass",
            lambda name, cmd, label, mode: captured_cmds.append(cmd) or True,
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
            "hyperi_ci.languages.rust.quality._run_matrix_pass",
            lambda name, cmd, label, mode: captured_cmds.append(cmd) or True,
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
        # Should appear twice -- once per pair
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
            "hyperi_ci.languages.rust.quality._run_matrix_pass",
            lambda name, cmd, label, mode: captured_cmds.append(cmd) or True,
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
            "hyperi_ci.languages.rust.quality._run_matrix_pass",
            lambda name, cmd, label, mode: False,
        )
        config = _make_config(None)
        assert _run_feature_matrix(config) is False


@pytest.mark.usefixtures("_force_lib_target")
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
            stdout = "Documenting scalo v2.5.1\nFinished\n"
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

        # Exactly one summary line -- not spam
        assert len(warnings_emitted) == 1
        msg = warnings_emitted[0]
        # Contains correct count (2 actual warnings, summary line subtracted)
        assert "2 doc warning" in msg
        # References the standards URLs
        assert "doc.rust-lang.org/rustdoc" in msg
        assert "api-guidelines" in msg


class TestHasLibTarget:
    """`_has_lib_target` correctly identifies bin-only vs library projects."""

    def test_no_cargo_toml_returns_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Override the autouse fixture by re-importing the real fn
        from hyperi_ci.languages.rust.quality import _has_lib_target

        assert _has_lib_target(tmp_path) is False

    def test_bin_only_project_returns_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hyperi_ci.languages.rust.quality import _has_lib_target

        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "myapp"\nversion = "0.1.0"\n'
        )
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.rs").write_text("fn main() {}\n")

        # Mock cargo metadata to return only a bin target
        class FakeResult:
            returncode = 0
            stdout = '{"packages":[{"name":"myapp","targets":[{"kind":["bin"],"name":"myapp"}]}]}'
            stderr = ""

        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality.subprocess.run",
            lambda *a, **kw: FakeResult(),
        )
        assert _has_lib_target(tmp_path) is False

    def test_library_project_returns_true(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hyperi_ci.languages.rust.quality import _has_lib_target

        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "mylib"\nversion = "0.1.0"\n'
        )

        class FakeResult:
            returncode = 0
            stdout = '{"packages":[{"name":"mylib","targets":[{"kind":["lib"],"name":"mylib"}]}]}'
            stderr = ""

        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality.subprocess.run",
            lambda *a, **kw: FakeResult(),
        )
        assert _has_lib_target(tmp_path) is True

    def test_mixed_lib_and_bin_returns_true(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hyperi_ci.languages.rust.quality import _has_lib_target

        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "myproj"\nversion = "0.1.0"\n'
        )

        class FakeResult:
            returncode = 0
            stdout = (
                '{"packages":[{"name":"myproj","targets":['
                '{"kind":["lib"],"name":"myproj"},'
                '{"kind":["bin"],"name":"myproj"}'
                "]}]}"
            )
            stderr = ""

        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality.subprocess.run",
            lambda *a, **kw: FakeResult(),
        )
        assert _has_lib_target(tmp_path) is True

    def test_falls_back_to_filesystem_when_cargo_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hyperi_ci.languages.rust.quality import _has_lib_target

        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "mylib"\nversion = "0.1.0"\n'
        )
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "lib.rs").write_text("// lib\n")

        def fake_run(*args, **kwargs):
            raise FileNotFoundError("cargo not on PATH")

        monkeypatch.setattr("hyperi_ci.languages.rust.quality.subprocess.run", fake_run)
        assert _has_lib_target(tmp_path) is True

    def test_filesystem_fallback_returns_false_for_bin_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hyperi_ci.languages.rust.quality import _has_lib_target

        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "myapp"\nversion = "0.1.0"\n'
        )
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.rs").write_text("fn main() {}\n")

        def fake_run(*args, **kwargs):
            raise FileNotFoundError("cargo not on PATH")

        monkeypatch.setattr("hyperi_ci.languages.rust.quality.subprocess.run", fake_run)
        assert _has_lib_target(tmp_path) is False


class TestFeatureMatrixBinOnlyProject:
    """Bin-only Rust crates should run feature_matrix with --bins, not --lib."""

    def test_default_invocation_uses_bins_when_no_lib(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured_cmds: list[list[str]] = []
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality.shutil.which", lambda n: f"/usr/bin/{n}"
        )
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality._run_matrix_pass",
            lambda name, cmd, label, mode: captured_cmds.append(cmd) or True,
        )
        # Override the autouse "force lib" fixture
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality._has_lib_target", lambda *a, **kw: False
        )

        config = _make_config(None)
        assert _run_feature_matrix(config) is True

        assert captured_cmds[0] == ["cargo", "check", "--no-default-features", "--bins"]
        assert captured_cmds[1] == [
            "cargo",
            "hack",
            "--each-feature",
            "--no-dev-deps",
            "check",
            "--bins",
        ]


class TestFeatureMatrixMixedWorkspace:
    """issue #238: a workspace mixing lib and bin-only members is scoped per member.

    `cargo hack` runs per member, so a workspace-wide `--lib` fails outright on
    a bin-only member with "no library targets found". That is why
    ci-test-rust-workspace had feature_matrix switched off.
    """

    @staticmethod
    def _capture(monkeypatch: pytest.MonkeyPatch, lib_map: dict[str, bool]) -> list:
        captured: list[list[str]] = []
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality.shutil.which", lambda n: f"/usr/bin/{n}"
        )
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality._run_matrix_pass",
            lambda name, cmd, label, mode: captured.append(cmd) or True,
        )
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality._package_lib_map",
            lambda *a, **kw: lib_map,
        )
        return captured

    def test_each_member_gets_the_flag_its_targets_allow(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = self._capture(monkeypatch, {"the-lib": True, "the-app": False})

        assert _run_feature_matrix(_make_config(None)) is True

        # Sorted by package name, so the-app precedes the-lib.
        assert captured[0] == [
            "cargo",
            "check",
            "--no-default-features",
            "-p",
            "the-app",
            "--bins",
        ]
        assert captured[1] == [
            "cargo",
            "check",
            "--no-default-features",
            "-p",
            "the-lib",
            "--lib",
        ]
        hack = [cmd for cmd in captured if cmd[1] == "hack"]
        assert [cmd[-2:] for cmd in hack] == [
            ["the-app", "--bins"],
            ["the-lib", "--lib"],
        ]

    def test_a_uniform_workspace_stays_on_one_invocation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No split where every member agrees: same cost as before the fix."""
        captured = self._capture(monkeypatch, {"one": True, "two": True})
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality._has_lib_target", lambda *a, **kw: True
        )

        assert _run_feature_matrix(_make_config(None)) is True

        assert len(captured) == 2
        assert not any("-p" in cmd for cmd in captured)

    def test_no_metadata_falls_back_to_the_workspace_answer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty map means cargo metadata failed, not that there are no libs."""
        captured = self._capture(monkeypatch, {})
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality._has_lib_target", lambda *a, **kw: True
        )

        assert _run_feature_matrix(_make_config(None)) is True

        assert len(captured) == 2
        assert captured[0] == ["cargo", "check", "--no-default-features", "--lib"]


# --- deny.toml advisory-ignore sharing (issue #42) -----------------------
from hyperi_ci.languages.rust.quality import (  # noqa: E402
    _deny_toml_advisory_ignores,
    _merge_deny_advisory_ignores,
)
from hyperi_ci.quality.ignores import IgnoreEntry  # noqa: E402


class TestDenyTomlAdvisoryIgnores:
    def test_no_deny_toml_returns_empty(self, tmp_path: Path) -> None:
        assert _deny_toml_advisory_ignores(tmp_path) == []

    def test_string_and_table_forms(self, tmp_path: Path) -> None:
        (tmp_path / "deny.toml").write_text(
            "[advisories]\n"
            "ignore = [\n"
            '    "RUSTSEC-2024-0436",\n'
            '    { id = "RUSTSEC-2021-0127", reason = "tracked upstream" },\n'
            "]\n"
        )
        ids = _deny_toml_advisory_ignores(tmp_path)
        assert ids == ["RUSTSEC-2024-0436", "RUSTSEC-2021-0127"]

    def test_non_advisory_entries_filtered(self, tmp_path: Path) -> None:
        # deny.toml ignore lists can also carry crate names / licence IDs
        # that mean nothing to cargo-audit / osv-scanner.
        (tmp_path / "deny.toml").write_text(
            '[advisories]\nignore = ["RUSTSEC-2024-0436", "some-crate", "Apache-2.0"]\n'
        )
        assert _deny_toml_advisory_ignores(tmp_path) == ["RUSTSEC-2024-0436"]

    def test_malformed_deny_toml_returns_empty(self, tmp_path: Path) -> None:
        (tmp_path / "deny.toml").write_text("this is = = not valid toml [[[\n")
        assert _deny_toml_advisory_ignores(tmp_path) == []

    def test_no_advisories_section(self, tmp_path: Path) -> None:
        (tmp_path / "deny.toml").write_text('[licenses]\nallow = ["MIT"]\n')
        assert _deny_toml_advisory_ignores(tmp_path) == []


class TestMergeDenyAdvisoryIgnores:
    def test_appends_new_ids(self) -> None:
        merged = _merge_deny_advisory_ignores([], "cargo-audit", ["RUSTSEC-2024-0436"])
        assert [e.id for e in merged] == ["RUSTSEC-2024-0436"]
        assert merged[0].tool == "cargo-audit"
        assert "deny.toml" in merged[0].reason

    def test_dedupes_against_existing(self) -> None:
        existing = [
            IgnoreEntry(tool="cargo-audit", id="RUSTSEC-2024-0436", reason="dup")
        ]
        merged = _merge_deny_advisory_ignores(
            existing, "cargo-audit", ["RUSTSEC-2024-0436", "RUSTSEC-2021-0127"]
        )
        ids = [e.id for e in merged]
        assert ids == ["RUSTSEC-2024-0436", "RUSTSEC-2021-0127"]
        # the pre-existing quality.ignore entry is kept verbatim (its reason)
        assert merged[0].reason == "dup"


# --- feature sets that build with warnings (issue #333) -------------------

# Captured from cargo-hack 0.6.45 on cargo 1.98.1, stdout and stderr on one pipe:
# a crate where feature `a` alone and feature `c` alone each leave a helper dead.
_HACK_WARNINGS_LOCAL = """\
info: --no-dev-deps modifies real `Cargo.toml` while cargo-hack is running and restores it when finished
info: running `cargo check --lib --all-features` on fm-probe (1/6)
    Checking fm-probe v0.1.0 (/work/fm-probe)
    Finished `dev` profile [unoptimized + debuginfo] target(s) in 0.05s

info: running `cargo check --lib --no-default-features --features a` on fm-probe (3/6)
    Checking fm-probe v0.1.0 (/work/fm-probe)
warning: function `helper` is never used
 --> src/lib.rs:4:4
  |
4 | fn helper() -> u32 {
  |    ^^^^^^
  |
  = note: `#[warn(dead_code)]` (part of `#[warn(unused)]`) on by default

warning: `fm-probe` (lib) generated 1 warning
    Finished `dev` profile [unoptimized + debuginfo] target(s) in 0.04s

info: running `cargo check --lib --no-default-features --features b` on fm-probe (4/6)
    Checking fm-probe v0.1.0 (/work/fm-probe)
    Finished `dev` profile [unoptimized + debuginfo] target(s) in 0.05s

info: running `cargo check --lib --no-default-features --features c` on fm-probe (5/6)
    Checking fm-probe v0.1.0 (/work/fm-probe)
warning: function `other` is never used
 --> src/lib.rs:9:4
  |
9 | fn other() -> u32 {
  |    ^^^^^
  |
  = note: `#[warn(dead_code)]` (part of `#[warn(unused)]`) on by default

warning: `fm-probe` (lib) generated 1 warning
    Finished `dev` profile [unoptimized + debuginfo] target(s) in 0.04s
"""

# The same run under GITHUB_ACTIONS=true, where cargo-hack opens a log group.
_HACK_WARNINGS_GHA = """\
info: --no-dev-deps modifies real `Cargo.toml` while cargo-hack is running and restores it when finished
::group::running `cargo check --lib --no-default-features --features a` on fm-probe (3/6)
warning: function `helper` is never used
 --> src/lib.rs:4:4
  |
4 | fn helper() -> u32 {
  |    ^^^^^^
  |
  = note: `#[warn(dead_code)]` (part of `#[warn(unused)]`) on by default

warning: `fm-probe` (lib) generated 1 warning
    Finished `dev` profile [unoptimized + debuginfo] target(s) in 0.01s
::endgroup::
::group::running `cargo check --lib --no-default-features --features b` on fm-probe (4/6)
    Finished `dev` profile [unoptimized + debuginfo] target(s) in 0.01s
::endgroup::
::group::running `cargo check --lib --no-default-features --features c` on fm-probe (5/6)
warning: function `other` is never used
 --> src/lib.rs:9:4
  |
9 | fn other() -> u32 {
  |    ^^^^^
  |
  = note: `#[warn(dead_code)]` (part of `#[warn(unused)]`) on by default

warning: `fm-probe` (lib) generated 1 warning
    Finished `dev` profile [unoptimized + debuginfo] target(s) in 0.01s
::endgroup::
"""

# The same crate with warnings denied and --keep-going.
_HACK_DENIED = """\
info: running `cargo check --config target.'cfg(all())'.rustflags=["-Dwarnings"] --lib --no-default-features --features a` on fm-probe (3/6)
    Checking fm-probe v0.1.0 (/work/fm-probe)
error: function `helper` is never used
 --> src/lib.rs:4:4
  |
4 | fn helper() -> u32 {
  |    ^^^^^^
  |
  = note: `-D dead-code` implied by `-D warnings`
  = help: to override `-D warnings` add `#[expect(dead_code)]` or `#[allow(dead_code)]`

error: could not compile `fm-probe` (lib) due to 1 previous error
error: process didn't exit successfully: `cargo check --config target.'cfg(all())'.rustflags=["-Dwarnings"] --lib --manifest-path Cargo.toml --no-default-features --features a` (exit status: 101)

info: running `cargo check --config target.'cfg(all())'.rustflags=["-Dwarnings"] --lib --no-default-features --features b` on fm-probe (4/6)
    Checking fm-probe v0.1.0 (/work/fm-probe)
    Finished `dev` profile [unoptimized + debuginfo] target(s) in 0.05s

info: running `cargo check --config target.'cfg(all())'.rustflags=["-Dwarnings"] --lib --no-default-features --features c` on fm-probe (5/6)
    Checking fm-probe v0.1.0 (/work/fm-probe)
error: function `other` is never used
 --> src/lib.rs:9:4
  |
9 | fn other() -> u32 {
  |    ^^^^^
  |
  = note: `-D dead-code` implied by `-D warnings`
  = help: to override `-D warnings` add `#[expect(dead_code)]` or `#[allow(dead_code)]`

error: could not compile `fm-probe` (lib) due to 1 previous error

error: failed to run 2 commands
"""

_PLAIN_CHECK_WARNS = """\
    Checking fm-probe v0.1.0 (/work/fm-probe)
warning: function `helper` is never used
 --> src/lib.rs:4:4
  |
warning: `fm-probe` (lib) generated 1 warning
    Finished `dev` profile [unoptimized + debuginfo] target(s) in 0.04s
"""

_HACK_CMD = ["cargo", "hack", "--each-feature", "--no-dev-deps", "check", "--lib"]
_DENY = "target.'cfg(all())'.rustflags=[\"-Dwarnings\"]"


class TestFeatureSetFindings:
    """The parser names each feature set from real cargo-hack output."""

    @pytest.mark.parametrize("output", [_HACK_WARNINGS_LOCAL, _HACK_WARNINGS_GHA])
    def test_each_warned_set_is_named_with_its_first_warning(self, output: str) -> None:
        findings = _feature_set_findings(output, "warning", "unnamed")

        assert [(f.label, f.message) for f in findings] == [
            (
                "--features a on fm-probe",
                "function `helper` is never used (src/lib.rs:4:4)",
            ),
            (
                "--features c on fm-probe",
                "function `other` is never used (src/lib.rs:9:4)",
            ),
        ]

    def test_denied_sets_are_named_as_errors(self) -> None:
        findings = _feature_set_findings(_HACK_DENIED, "error", "unnamed")

        assert [(f.label, f.message) for f in findings] == [
            (
                "--features a on fm-probe",
                "function `helper` is never used (src/lib.rs:4:4)",
            ),
            (
                "--features c on fm-probe",
                "function `other` is never used (src/lib.rs:9:4)",
            ),
        ]

    def test_errors_are_not_counted_as_warnings(self) -> None:
        assert _feature_set_findings(_HACK_DENIED, "warning", "unnamed") == []

    def test_plain_check_output_takes_the_first_label(self) -> None:
        findings = _feature_set_findings(
            _PLAIN_CHECK_WARNS, "warning", "--no-default-features"
        )

        assert [f.label for f in findings] == ["--no-default-features"]

    def test_all_features_and_bare_runs_are_labelled(self) -> None:
        output = (
            "info: running `cargo check --lib --all-features` on x (1/2)\n"
            "warning: unused import: `std::fmt`\n"
            "warning: `x` (lib) generated 1 warning\n"
            "info: running `cargo check --lib --no-default-features` on x (2/2)\n"
            "warning: `x` (lib) generated 1 warning\n"
        )
        findings = _feature_set_findings(output, "warning", "unnamed")

        assert [(f.label, f.message) for f in findings] == [
            ("--all-features on x", "unused import: `std::fmt`"),
            ("--no-default-features on x", ""),
        ]


class TestDenyWarnings:
    """Blocking mode denies warnings without discarding anyone's rustflags."""

    def test_no_env_flags_adds_a_target_entry_after_check(self) -> None:
        cmd, env = _deny_warnings(_HACK_CMD, {})

        assert cmd == [*_HACK_CMD[:5], "--config", _DENY, "--lib"]
        assert env == {}

    def test_existing_rustflags_are_kept(self) -> None:
        cmd, env = _deny_warnings(_HACK_CMD, {"RUSTFLAGS": " -C target-cpu=native "})

        assert cmd == _HACK_CMD
        assert env == {"RUSTFLAGS": "-C target-cpu=native -D warnings"}

    def test_empty_rustflags_still_wins_over_config(self) -> None:
        _, env = _deny_warnings(_HACK_CMD, {"RUSTFLAGS": ""})

        assert env == {"RUSTFLAGS": "-D warnings"}

    def test_encoded_rustflags_take_the_deny_when_set(self) -> None:
        cmd, env = _deny_warnings(
            _HACK_CMD,
            {"CARGO_ENCODED_RUSTFLAGS": "-C\x1ftarget-cpu=native", "RUSTFLAGS": "x"},
        )

        assert cmd == _HACK_CMD
        assert env == {
            "CARGO_ENCODED_RUSTFLAGS": "-C\x1ftarget-cpu=native\x1f-D\x1fwarnings"
        }


def _fake_cargo(
    monkeypatch: pytest.MonkeyPatch, returncode: int, output: str
) -> list[dict[str, Any]]:
    """Record every subprocess.run call and answer it with ``output``."""
    calls: list[dict[str, Any]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append({"cmd": cmd, **kwargs})
        return subprocess.CompletedProcess(cmd, returncode, stdout=output)

    monkeypatch.setattr("hyperi_ci.languages.rust.quality.subprocess.run", fake_run)
    monkeypatch.setattr(
        "hyperi_ci.languages.rust.quality.shutil.which", lambda n: f"/usr/bin/{n}"
    )
    return calls


class TestRunMatrixPass:
    """What each warnings mode runs, and what it reports."""

    def test_warn_names_each_set_and_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _fake_cargo(monkeypatch, 0, _HACK_WARNINGS_GHA)
        announced: list[str] = []
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality.announce",
            lambda msg, title: announced.append(msg),
        )
        monkeypatch.setenv("RUSTFLAGS", "-C target-cpu=native")

        assert _run_matrix_pass("fm", list(_HACK_CMD), "unnamed", "warn") is True

        assert announced == [
            "feature_matrix: --features a on fm-probe builds with warnings: "
            "function `helper` is never used (src/lib.rs:4:4)",
            "feature_matrix: --features c on fm-probe builds with warnings: "
            "function `other` is never used (src/lib.rs:9:4)",
        ]
        assert calls[0]["cmd"] == _HACK_CMD
        assert calls[0]["env"] is None
        assert calls[0]["stderr"] is subprocess.STDOUT

    def test_blocking_denies_merges_rustflags_and_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _fake_cargo(monkeypatch, 101, _HACK_DENIED)
        errors: list[str] = []
        monkeypatch.setattr("hyperi_ci.languages.rust.quality.error", errors.append)
        monkeypatch.setattr("hyperi_ci.languages.rust.quality.info", lambda msg: None)
        monkeypatch.setenv("RUSTFLAGS", "-C target-cpu=native")
        monkeypatch.delenv("CARGO_ENCODED_RUSTFLAGS", raising=False)

        assert _run_matrix_pass("fm", list(_HACK_CMD), "unnamed", "blocking") is False

        assert calls[0]["cmd"] == ["cargo", "hack", "--keep-going", *_HACK_CMD[2:]]
        assert calls[0]["env"]["RUSTFLAGS"] == "-C target-cpu=native -D warnings"
        assert errors == [
            "  fm: failed",
            "  fm: --features a on fm-probe: "
            "function `helper` is never used (src/lib.rs:4:4)",
            "  fm: --features c on fm-probe: "
            "function `other` is never used (src/lib.rs:9:4)",
        ]

    def test_blocking_without_env_flags_uses_the_config_entry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _fake_cargo(monkeypatch, 0, "")
        monkeypatch.delenv("RUSTFLAGS", raising=False)
        monkeypatch.delenv("CARGO_ENCODED_RUSTFLAGS", raising=False)
        cmd = ["cargo", "check", "--no-default-features", "--lib"]

        assert _run_matrix_pass("fm", cmd, "--no-default-features", "blocking")

        assert calls[0]["cmd"] == [
            "cargo",
            "check",
            "--config",
            _DENY,
            "--no-default-features",
            "--lib",
        ]
        assert calls[0]["env"] is None

    def test_a_compile_error_fails_in_warn_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_cargo(monkeypatch, 101, _HACK_DENIED)

        assert _run_matrix_pass("fm", list(_HACK_CMD), "unnamed", "warn") is False

    def test_disabled_runs_exactly_what_it_did_before(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _fake_cargo(monkeypatch, 0, _HACK_WARNINGS_GHA)
        ran: list[tuple[list[str], str]] = []
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality._run_tool",
            lambda name, cmd, mode, use_uvx=False: ran.append((cmd, mode)) or True,
        )
        monkeypatch.setenv("RUSTFLAGS", "-C target-cpu=native")

        assert _run_matrix_pass("fm", list(_HACK_CMD), "unnamed", "disabled")

        assert ran == [(_HACK_CMD, "blocking")]
        assert calls == []


@pytest.mark.usefixtures("_force_lib_target")
class TestFeatureMatrixWarningsMode:
    """``quality.rust.feature_matrix.warnings`` resolves like every quality mode."""

    @staticmethod
    def _modes(monkeypatch: pytest.MonkeyPatch, fm: dict[str, Any] | None) -> set:
        modes: set[str] = set()
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality.shutil.which", lambda n: f"/usr/bin/{n}"
        )
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality._package_lib_map", lambda *a, **kw: {}
        )
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.quality._run_matrix_pass",
            lambda name, cmd, label, mode: modes.add(mode) or True,
        )
        assert _run_feature_matrix(_make_config(fm)) is True
        return modes

    def test_unset_is_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HYPERCI_QUALITY_STRICT", raising=False)
        assert self._modes(monkeypatch, None) == {"warn"}

    @pytest.mark.parametrize("mode", ["blocking", "warn", "disabled"])
    def test_configured_mode_is_used(
        self, monkeypatch: pytest.MonkeyPatch, mode: str
    ) -> None:
        monkeypatch.delenv("HYPERCI_QUALITY_STRICT", raising=False)
        assert self._modes(monkeypatch, {"warnings": mode}) == {mode}

    def test_strict_upgrades_warn_to_blocking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HYPERCI_QUALITY_STRICT", "1")
        assert self._modes(monkeypatch, None) == {"blocking"}

    def test_strict_leaves_disabled_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HYPERCI_QUALITY_STRICT", "1")
        assert self._modes(monkeypatch, {"warnings": "disabled"}) == {"disabled"}

    def test_a_typo_falls_back_to_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HYPERCI_QUALITY_STRICT", raising=False)
        assert self._modes(monkeypatch, {"warnings": "block"}) == {"warn"}
