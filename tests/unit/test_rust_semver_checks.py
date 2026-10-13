# Project:   HyperI CI
# File:      tests/unit/test_rust_semver_checks.py
# Purpose:   Tests for the public-API break check
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Public-API break check tests.

The case that matters most is the skip: a binary-only crate must be reported
as having nothing to check, never as having been checked and found clean.

The exit codes are the second half of that. Only 100 is a verdict; 101 is
cargo's error code and means the comparison never happened, so reporting it as
a breaking change is a flatly wrong diagnosis on a first release.
"""

import subprocess
from pathlib import Path

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.languages.rust import semver_checks


def _config(mode: str = "warn") -> CIConfig:
    return CIConfig(
        _raw={"quality": {"rust": {"semver_checks": mode}}},
    )


class TestItSkipsWhatItCannotCheck:
    def test_disabled_does_not_run(self, tmp_path: Path) -> None:
        assert semver_checks.run(_config("disabled"), project_root=tmp_path) == 0

    def test_a_binary_only_crate_is_skipped(self, tmp_path: Path) -> None:
        """No lib target means no public API -- and that must not read as a pass."""
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "app"\nversion = "0.1.0"\n', encoding="utf-8"
        )
        src = tmp_path / "src"
        src.mkdir()
        (src / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
        assert semver_checks.run(_config(), project_root=tmp_path) == 0

    def test_no_manifest_is_skipped(self, tmp_path: Path) -> None:
        assert semver_checks.run(_config(), project_root=tmp_path) == 0


@pytest.fixture
def lib_crate(tmp_path: Path) -> Path:
    """A crate with a lib target, so the check reaches the tool."""
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "lib"\nversion = "0.2.0"\n', encoding="utf-8"
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_text("pub fn f() {}\n", encoding="utf-8")
    return tmp_path


def _fake_tool(
    monkeypatch: pytest.MonkeyPatch, code: int, output: str = ""
) -> list[str]:
    """Put the tool on PATH with a fixed exit code; return what it says.

    The module logs through loguru, which writes to the stderr it captured at
    import, so capsys never sees it. Collecting the helpers tests the same
    thing more directly: what this module chose to report.
    """
    said: list[str] = []
    monkeypatch.setattr(semver_checks.shutil, "which", lambda _: "/usr/bin/x")
    monkeypatch.setattr(
        semver_checks,
        "run_cmd",
        lambda *a, **k: subprocess.CompletedProcess(
            args=[], returncode=code, stdout=output, stderr=""
        ),
    )
    for name in ("info", "warn", "error", "success"):
        monkeypatch.setattr(semver_checks, name, said.append)
    return said


class TestOnlyOneExitCodeIsAVerdict:
    """Measured against 0.50.0: 0 compatible, 100 violation, 101 cargo error."""

    def test_zero_is_compatible(
        self, lib_crate: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_tool(monkeypatch, 0)
        assert semver_checks.run(_config("blocking"), project_root=lib_crate) == 0

    def test_a_hundred_blocks_and_names_the_major_bump(
        self, lib_crate: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said = _fake_tool(monkeypatch, 100, "semver requires new major version")
        assert semver_checks.run(_config("blocking"), project_root=lib_crate) == 1
        assert any("major bump" in line for line in said)

    def test_an_unreached_verdict_does_not_read_as_a_break(
        self, lib_crate: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """101 blocks too -- but never claims the API changed."""
        said = _fake_tool(monkeypatch, 101, "error: failed to build rustdoc")
        assert semver_checks.run(_config("blocking"), project_root=lib_crate) == 1
        assert any("could not reach a verdict" in line for line in said)
        assert not any("major bump" in line for line in said)

    def test_warn_mode_never_fails_whatever_the_code(
        self, lib_crate: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for code in (100, 101):
            _fake_tool(monkeypatch, code, "x")
            assert semver_checks.run(_config(), project_root=lib_crate) == 0


def _rustdoc_failure(noise: int) -> str:
    """Cargo's shape: root cause first, cascade and rustdoc notes after it."""
    return "\n".join(
        [
            "error: linker `cc` not found",
            "  = note: No such file or directory (os error 2)",
            *(f"   Checking dep{n} v0.1.0" for n in range(noise)),
            "error: could not compile `quote` (build script) due to 1 previous error",
            "error: could not compile `libc` (build script) due to 1 previous error",
            "error: failed to build rustdoc for crate lib v0.2.0",
        ]
    )


class TestAnUnreachedVerdictNamesItsCause:
    """Issue #678: the root cause sat above the 20-line tail and was never shown."""

    def test_the_warning_names_the_first_error(
        self, lib_crate: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said = _fake_tool(monkeypatch, 101, _rustdoc_failure(noise=40))
        assert semver_checks.run(_config(), project_root=lib_crate) == 0
        verdict = [line for line in said if "could not reach a verdict" in line]
        assert len(verdict) == 1
        assert "First error: error: linker `cc` not found" in verdict[0]

    def test_a_root_cause_above_the_tail_is_still_logged(
        self, lib_crate: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said = _fake_tool(monkeypatch, 101, _rustdoc_failure(noise=40))
        semver_checks.run(_config(), project_root=lib_crate)
        assert any(line.strip() == "error: linker `cc` not found" for line in said)

    def test_an_error_already_in_the_tail_is_not_repeated(
        self, lib_crate: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said = _fake_tool(monkeypatch, 101, _rustdoc_failure(noise=0))
        semver_checks.run(_config(), project_root=lib_crate)
        assert "    first errors:" not in said
        linker = [
            line for line in said if line.strip() == "error: linker `cc` not found"
        ]
        assert len(linker) == 1

    def test_no_error_line_leaves_the_message_alone(
        self, lib_crate: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said = _fake_tool(monkeypatch, 101, "registry timed out")
        semver_checks.run(_config(), project_root=lib_crate)
        assert not any("First error" in line for line in said)

    def test_a_break_verdict_does_not_carry_a_first_error(
        self, lib_crate: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said = _fake_tool(monkeypatch, 100, "error[E0001]: nonsense\nmajor needed")
        semver_checks.run(_config(), project_root=lib_crate)
        assert not any("First error" in line for line in said)


class TestFirstErrors:
    def test_keeps_order_drops_duplicates_and_caps(self) -> None:
        lines = ["  error: a", "error[E1]: b", "error: a", "warning: w", "error: c"]
        lines.append("error: d")
        assert semver_checks._first_errors(lines) == [
            "error: a",
            "error[E1]: b",
            "error: c",
        ]

    def test_empty_output_has_none(self) -> None:
        assert semver_checks._first_errors([]) == []

    def test_a_word_containing_error_is_not_one(self) -> None:
        assert semver_checks._first_errors(["errors: 0", "no error: here"]) == []


class TestMissingToolAnnotates:
    def test_the_annotation_text_is_unchanged(
        self,
        lib_crate: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(semver_checks.shutil, "which", lambda _: None)
        monkeypatch.setattr(semver_checks, "is_ci", lambda: True)
        monkeypatch.setattr("hyperi_ci.common.is_github_actions", lambda: True)
        assert semver_checks.run(_config(), project_root=lib_crate) == 0
        assert capsys.readouterr().out == (
            "::warning title=hyperi-ci semver-checks skipped::"
            "cargo-semver-checks is not installed on this runner, so the public "
            "API was NOT checked. This release is unverified for breaking "
            "changes.\n"
        )


class TestAFirstReleaseHasNothingToBreak:
    def test_an_unpublished_crate_is_skipped_not_failed(
        self, lib_crate: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Blocking must not stop a crate's first release over a missing baseline."""
        said = _fake_tool(monkeypatch, 101, "lib not found in registry (crates.io)")
        assert semver_checks.run(_config("blocking"), project_root=lib_crate) == 0
        assert any("no baseline" in line for line in said)

    # The live path belongs to the fixture fleet: the tool resolves its
    # baseline from crates.io, so driving it here is a network wait.
