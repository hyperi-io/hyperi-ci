# Project:   HyperI CI
# File:      tests/unit/test_rust_pgo.py
# Purpose:   Unit tests for PGO/BOLT build orchestration
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from hyperi_ci.languages.rust.optimize import OptimizationProfile
from hyperi_ci.languages.rust.pgo import (
    _ensure_cargo_pgo_installed,
    _ensure_llvm_bolt_available,
    _instrumented_binary_path,
    _run_workload,
    run_pgo_build,
)


def _make_profile(
    *,
    allocator: str = "jemalloc",
    pgo_enabled: bool = True,
    pgo_workload_cmd: str | None = "bash scripts/pgo-workload.sh",
    pgo_duration_secs: int = 300,
    bolt_enabled: bool = False,
) -> OptimizationProfile:
    return OptimizationProfile(
        channel="release",
        allocator=allocator,
        lto="fat",
        pgo_enabled=pgo_enabled,
        pgo_workload_cmd=pgo_workload_cmd,
        pgo_duration_secs=pgo_duration_secs,
        bolt_enabled=bolt_enabled,
    )


class TestCargoPgoInstallGate:
    """cargo-pgo auto-install logic."""

    def test_already_installed_returns_true_no_install(self) -> None:
        with (
            patch(
                "hyperi_ci.languages.rust.pgo.shutil.which",
                return_value="/bin/cargo-pgo",
            ),
            patch("hyperi_ci.languages.rust.pgo.subprocess.run") as mock_run,
        ):
            assert _ensure_cargo_pgo_installed() is True
            mock_run.assert_not_called()

    def test_not_installed_triggers_install_command(self) -> None:
        which_responses = iter(
            [None, "/bin/cargo-pgo"]
        )  # before install, after install
        with (
            patch(
                "hyperi_ci.languages.rust.pgo.shutil.which",
                side_effect=lambda _: next(which_responses),
            ),
            patch(
                "hyperi_ci.languages.rust.pgo.subprocess.run",
                return_value=MagicMock(returncode=0),
            ) as mock_run,
        ):
            result = _ensure_cargo_pgo_installed()
        assert result is True
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd == ["cargo", "install", "cargo-pgo", "--locked"]

    def test_install_failure_returns_false(self) -> None:
        with (
            patch("hyperi_ci.languages.rust.pgo.shutil.which", return_value=None),
            patch(
                "hyperi_ci.languages.rust.pgo.subprocess.run",
                return_value=MagicMock(returncode=1),
            ),
        ):
            assert _ensure_cargo_pgo_installed() is False


class TestBoltAvailabilityCheck:
    """llvm-bolt discovery with versioned-binary fallback shim.

    Ubuntu ships only version-suffixed llvm-bolt-NN via the bolt-NN package
    — no unversioned /usr/bin/llvm-bolt. _ensure_llvm_bolt_available() must
    detect the versioned binary and expose it on PATH as `llvm-bolt` so
    cargo-pgo's BOLT subcommand can invoke it.
    """

    def test_llvm_bolt_unversioned_present(self) -> None:
        with patch(
            "hyperi_ci.languages.rust.pgo.shutil.which",
            return_value="/usr/bin/llvm-bolt",
        ):
            assert _ensure_llvm_bolt_available() is True

    def test_llvm_bolt_missing(self) -> None:
        with patch("hyperi_ci.languages.rust.pgo.shutil.which", return_value=None):
            assert _ensure_llvm_bolt_available() is False

    def test_llvm_bolt_versioned_fallback_creates_shim(
        self, tmp_path, monkeypatch
    ) -> None:
        """When only /usr/bin/llvm-bolt-21 exists, create ~/.local/bin/llvm-bolt shim."""
        # Fake versioned binary on disk (the shim target)
        fake_bolt_21 = tmp_path / "llvm-bolt-21"
        fake_bolt_21.touch()

        # Redirect Path.home() to a clean tmp dir so the shim lands there
        home = tmp_path / "home"
        monkeypatch.setattr("hyperi_ci.languages.rust.pgo.Path.home", lambda: home)

        def fake_which(name: str) -> str | None:
            if name == "llvm-bolt":
                return None  # no unversioned binary
            if name == "llvm-bolt-21":
                return str(fake_bolt_21)
            return None

        with patch("hyperi_ci.languages.rust.pgo.shutil.which", fake_which):
            assert _ensure_llvm_bolt_available() is True

        shim = home / ".local" / "bin" / "llvm-bolt"
        assert shim.is_symlink()
        assert shim.resolve() == fake_bolt_21.resolve()
        assert str(home / ".local" / "bin") in os.environ["PATH"].split(os.pathsep)


class TestWorkloadExecution:
    """Workload command runs with HYPERCI_PGO_INSTRUMENTED_BINARY env and timeout."""

    def test_workload_sets_env_var_with_binary_path(self, tmp_path) -> None:
        binary = tmp_path / "my-bin"
        with patch(
            "hyperi_ci.languages.rust.pgo.subprocess.run",
            return_value=MagicMock(returncode=0),
        ) as mock_run:
            rc = _run_workload(
                "echo hi", duration_secs=10, instrumented_binary=binary, cwd=tmp_path
            )
        assert rc == 0
        kwargs = mock_run.call_args.kwargs
        assert kwargs["env"]["HYPERCI_PGO_INSTRUMENTED_BINARY"] == str(binary)

    def test_workload_appends_binary_path_as_first_arg(self, tmp_path) -> None:
        """The binary path is appended to workload_cmd as $1 (Unix-idiomatic).

        Consumer workload scripts take the binary path as their first
        positional argument. This matches the contract documented in
        docs/PGO-WORKLOAD-GUIDE.md and the shape of every template in
        templates/pgo-workload/.
        """
        binary = tmp_path / "my-bin with spaces"  # exercises shell quoting
        with patch(
            "hyperi_ci.languages.rust.pgo.subprocess.run",
            return_value=MagicMock(returncode=0),
        ) as mock_run:
            _run_workload(
                "bash scripts/pgo-workload.sh",
                duration_secs=10,
                instrumented_binary=binary,
                cwd=tmp_path,
            )
        # subprocess.run was called with the full shell command as its
        # first positional argument — binary path appended + properly quoted.
        call_cmd = mock_run.call_args.args[0]
        assert call_cmd.startswith("bash scripts/pgo-workload.sh ")
        assert str(binary) in call_cmd
        # shlex.quote should have wrapped the path with spaces in quotes
        assert "'" in call_cmd or '"' in call_cmd

    def test_workload_passes_cwd(self, tmp_path) -> None:
        binary = tmp_path / "bin"
        with patch(
            "hyperi_ci.languages.rust.pgo.subprocess.run",
            return_value=MagicMock(returncode=0),
        ) as mock_run:
            _run_workload(
                "echo hi", duration_secs=5, instrumented_binary=binary, cwd=tmp_path
            )
        assert mock_run.call_args.kwargs["cwd"] == tmp_path

    def test_workload_enforces_grace_timeout(self, tmp_path) -> None:
        binary = tmp_path / "bin"
        with patch(
            "hyperi_ci.languages.rust.pgo.subprocess.run",
            return_value=MagicMock(returncode=0),
        ) as mock_run:
            _run_workload(
                "x", duration_secs=100, instrumented_binary=binary, cwd=tmp_path
            )
        # duration + 600s absolute grace (covers testcontainers spin-up,
        # cargo-building feature-gated drivers, readiness waits, cleanup)
        assert mock_run.call_args.kwargs["timeout"] == 700

    def test_workload_empty_command_returns_error(self, tmp_path) -> None:
        binary = tmp_path / "bin"
        rc = _run_workload(
            "", duration_secs=10, instrumented_binary=binary, cwd=tmp_path
        )
        assert rc == 1

    def test_workload_failure_returns_nonzero(self, tmp_path) -> None:
        binary = tmp_path / "bin"
        with patch(
            "hyperi_ci.languages.rust.pgo.subprocess.run",
            return_value=MagicMock(returncode=42),
        ):
            rc = _run_workload(
                "false", duration_secs=10, instrumented_binary=binary, cwd=tmp_path
            )
        assert rc == 42


class TestInstrumentedBinaryPath:
    """The path-to-built-binary helper used after cargo pgo build."""

    def test_linux_x86_64_path(self, tmp_path) -> None:
        p = _instrumented_binary_path(
            tmp_path, "x86_64-unknown-linux-gnu", "my-bin", variant="pgo"
        )
        assert (
            p == tmp_path / "target" / "x86_64-unknown-linux-gnu" / "release" / "my-bin"
        )

    def test_aarch64_linux_path(self, tmp_path) -> None:
        p = _instrumented_binary_path(
            tmp_path, "aarch64-unknown-linux-gnu", "dfe-receiver", variant="pgo"
        )
        assert p.name == "dfe-receiver"
        assert "aarch64-unknown-linux-gnu" in str(p)


class TestRunPgoBuildOrchestration:
    """Full PGO pipeline: instrument → workload → optimise (→ BOLT)."""

    def test_falls_back_to_plain_build_when_cargo_pgo_install_fails(
        self, tmp_path
    ) -> None:
        profile = _make_profile()
        with (
            patch(
                "hyperi_ci.languages.rust.pgo._ensure_cargo_pgo_installed",
                return_value=False,
            ),
            patch(
                "hyperi_ci.languages.rust.pgo._run_plain_release_build",
                return_value=0,
            ) as mock_plain,
        ):
            rc = run_pgo_build(
                target="x86_64-unknown-linux-gnu",
                profile=profile,
                binary_name="my-bin",
                cwd=tmp_path,
            )
        # Graceful fallback: produce a plain-release binary so the overall
        # build still ships (Tier 1 optimisations still applied).
        assert rc == 0
        mock_plain.assert_called_once()

    def test_instrument_build_failure_aborts_pipeline(self, tmp_path) -> None:
        profile = _make_profile()
        with (
            patch(
                "hyperi_ci.languages.rust.pgo._ensure_cargo_pgo_installed",
                return_value=True,
            ),
            patch(
                "hyperi_ci.languages.rust.pgo._run_cargo_pgo",
                return_value=1,
            ) as mock_cargo,
        ):
            rc = run_pgo_build(
                target="x86_64-unknown-linux-gnu",
                profile=profile,
                binary_name="my-bin",
                cwd=tmp_path,
            )
        assert rc == 1
        # Only called once (instrument) — pipeline aborted
        assert mock_cargo.call_count == 1

    def test_workload_failure_aborts_pipeline(self, tmp_path) -> None:
        # Create the expected instrumented binary so path check passes
        bin_dir = tmp_path / "target" / "x86_64-unknown-linux-gnu" / "release"
        bin_dir.mkdir(parents=True)
        (bin_dir / "my-bin").touch()

        profile = _make_profile()
        with (
            patch(
                "hyperi_ci.languages.rust.pgo._ensure_cargo_pgo_installed",
                return_value=True,
            ),
            patch("hyperi_ci.languages.rust.pgo._run_cargo_pgo", return_value=0),
            patch("hyperi_ci.languages.rust.pgo._run_workload", return_value=3),
        ):
            rc = run_pgo_build(
                target="x86_64-unknown-linux-gnu",
                profile=profile,
                binary_name="my-bin",
                cwd=tmp_path,
            )
        # Hard fail: bad profile data is worse than no PGO
        assert rc == 3

    def test_full_pipeline_runs_instrument_workload_optimise(self, tmp_path) -> None:
        bin_dir = tmp_path / "target" / "x86_64-unknown-linux-gnu" / "release"
        bin_dir.mkdir(parents=True)
        (bin_dir / "my-bin").touch()

        profile = _make_profile()
        with (
            patch(
                "hyperi_ci.languages.rust.pgo._ensure_cargo_pgo_installed",
                return_value=True,
            ),
            patch(
                "hyperi_ci.languages.rust.pgo._run_cargo_pgo",
                return_value=0,
            ) as mock_cargo,
            patch(
                "hyperi_ci.languages.rust.pgo._run_workload",
                return_value=0,
            ) as mock_workload,
        ):
            rc = run_pgo_build(
                target="x86_64-unknown-linux-gnu",
                profile=profile,
                binary_name="my-bin",
                cwd=tmp_path,
            )
        assert rc == 0
        # Two cargo-pgo calls: build + optimize
        assert mock_cargo.call_count == 2
        # Workload ran once
        assert mock_workload.call_count == 1
        # Inspect commands
        build_args = mock_cargo.call_args_list[0][0][0]
        optimize_args = mock_cargo.call_args_list[1][0][0]
        assert build_args[0] == "build"
        assert optimize_args[0] == "optimize"

    def test_bolt_runs_after_pgo_on_linux(self, tmp_path) -> None:
        bin_dir = tmp_path / "target" / "x86_64-unknown-linux-gnu" / "release"
        bin_dir.mkdir(parents=True)
        (bin_dir / "my-bin").touch()

        profile = _make_profile(bolt_enabled=True)
        with (
            patch(
                "hyperi_ci.languages.rust.pgo._ensure_cargo_pgo_installed",
                return_value=True,
            ),
            patch(
                "hyperi_ci.languages.rust.pgo._ensure_llvm_bolt_available",
                return_value=True,
            ),
            patch(
                "hyperi_ci.languages.rust.pgo._run_cargo_pgo",
                return_value=0,
            ) as mock_cargo,
            patch("hyperi_ci.languages.rust.pgo._run_workload", return_value=0),
        ):
            rc = run_pgo_build(
                target="x86_64-unknown-linux-gnu",
                profile=profile,
                binary_name="my-bin",
                cwd=tmp_path,
            )
        assert rc == 0
        # Four cargo-pgo calls: build + optimize + bolt build + bolt optimize
        assert mock_cargo.call_count == 4
        last_call_args = mock_cargo.call_args_list[-1][0][0]
        assert last_call_args[0] == "bolt"
        assert last_call_args[1] == "optimize"

    def test_bolt_skipped_when_llvm_bolt_missing(self, tmp_path) -> None:
        bin_dir = tmp_path / "target" / "x86_64-unknown-linux-gnu" / "release"
        bin_dir.mkdir(parents=True)
        (bin_dir / "my-bin").touch()

        profile = _make_profile(bolt_enabled=True)
        with (
            patch(
                "hyperi_ci.languages.rust.pgo._ensure_cargo_pgo_installed",
                return_value=True,
            ),
            patch(
                "hyperi_ci.languages.rust.pgo._ensure_llvm_bolt_available",
                return_value=False,
            ),
            patch(
                "hyperi_ci.languages.rust.pgo._run_cargo_pgo",
                return_value=0,
            ) as mock_cargo,
            patch("hyperi_ci.languages.rust.pgo._run_workload", return_value=0),
        ):
            rc = run_pgo_build(
                target="x86_64-unknown-linux-gnu",
                profile=profile,
                binary_name="my-bin",
                cwd=tmp_path,
            )
        assert rc == 0
        # PGO succeeded, BOLT skipped → 2 cargo-pgo calls (build, optimize)
        assert mock_cargo.call_count == 2

    def test_features_included_in_cargo_pgo_args(self, tmp_path) -> None:
        bin_dir = tmp_path / "target" / "x86_64-unknown-linux-gnu" / "release"
        bin_dir.mkdir(parents=True)
        (bin_dir / "my-bin").touch()

        profile = _make_profile(allocator="jemalloc")
        with (
            patch(
                "hyperi_ci.languages.rust.pgo._ensure_cargo_pgo_installed",
                return_value=True,
            ),
            patch(
                "hyperi_ci.languages.rust.pgo._run_cargo_pgo",
                return_value=0,
            ) as mock_cargo,
            patch("hyperi_ci.languages.rust.pgo._run_workload", return_value=0),
        ):
            run_pgo_build(
                target="x86_64-unknown-linux-gnu",
                profile=profile,
                binary_name="my-bin",
                cwd=tmp_path,
            )
        # Check --features jemalloc appears in both cargo pgo calls
        for call in mock_cargo.call_args_list:
            args = call[0][0]
            assert "--features" in args
            assert "jemalloc" in args

    def test_system_allocator_no_features_flag(self, tmp_path) -> None:
        bin_dir = tmp_path / "target" / "x86_64-unknown-linux-gnu" / "release"
        bin_dir.mkdir(parents=True)
        (bin_dir / "my-bin").touch()

        profile = _make_profile(allocator="system")
        with (
            patch(
                "hyperi_ci.languages.rust.pgo._ensure_cargo_pgo_installed",
                return_value=True,
            ),
            patch(
                "hyperi_ci.languages.rust.pgo._run_cargo_pgo",
                return_value=0,
            ) as mock_cargo,
            patch("hyperi_ci.languages.rust.pgo._run_workload", return_value=0),
        ):
            run_pgo_build(
                target="x86_64-unknown-linux-gnu",
                profile=profile,
                binary_name="my-bin",
                cwd=tmp_path,
            )
        # No --features flag when system allocator
        for call in mock_cargo.call_args_list:
            args = call[0][0]
            assert "--features" not in args


class TestMissingInstrumentedBinary:
    """If instrument build succeeds but binary is missing, abort."""

    def test_missing_binary_is_error(self, tmp_path) -> None:
        profile = _make_profile()
        with (
            patch(
                "hyperi_ci.languages.rust.pgo._ensure_cargo_pgo_installed",
                return_value=True,
            ),
            patch(
                "hyperi_ci.languages.rust.pgo._run_cargo_pgo",
                return_value=0,
            ),
        ):
            rc = run_pgo_build(
                target="x86_64-unknown-linux-gnu",
                profile=profile,
                binary_name="my-bin",
                cwd=tmp_path,
            )
        assert rc == 1
