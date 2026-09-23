# Project:   HyperI CI
# File:      tests/unit/test_rust_pgo.py
# Purpose:   Unit tests for PGO/BOLT build orchestration
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

import os
import subprocess
from typing import Literal
from unittest.mock import MagicMock, patch

import pytest

from hyperi_ci.languages.rust import pgo
from hyperi_ci.languages.rust.optimize import (
    OptimizationOutcome,
    OptimizationProfile,
    resolve_optimization_profile,
)
from hyperi_ci.languages.rust.pgo import (
    BOLT_NOTE_SECTION,
    _ensure_cargo_pgo_installed,
    _ensure_ld_lld_available,
    _ensure_llvm_bolt_available,
    _install_bolt_output,
    _instrumented_binary_path,
    _release_dir,
    _run_workload,
    _run_workload_setup,
    cargo_pgo_version_from,
    run_pgo_build,
)
from hyperi_ci.versions import tool_version


@pytest.fixture(autouse=True)
def isolated_tool_home(tmp_path, monkeypatch):
    """Keep toolchain shims out of the real home and PATH changes out of the suite.

    The PGO pipeline symlinks versioned LLVM tools into ``~/.local/bin`` and
    prepends it to PATH, which on a developer box would rewrite what
    ``-fuse-ld=lld`` resolves to outside the test run.
    """
    monkeypatch.setattr(
        "hyperi_ci.languages.rust.pgo.Path.home", lambda: tmp_path / "home"
    )
    monkeypatch.setenv("PATH", os.environ["PATH"])


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
    """cargo-pgo auto-install logic.

    The helper prepends ~/.cargo/bin to ``os.environ["PATH"]`` and never puts
    it back (production wants cargo on PATH for the rest of the run), so every
    test reaching that branch reassigns PATH through monkeypatch and lets
    pytest restore it instead of leaking into the rest of the suite.
    """

    def test_already_installed_returns_true_no_install(self) -> None:
        with (
            patch(
                "hyperi_ci.languages.rust.pgo._installed_cargo_pgo_version",
                return_value=tool_version("cargo-pgo"),
            ),
            patch("hyperi_ci.languages.rust.pgo.subprocess.run") as mock_run,
        ):
            assert _ensure_cargo_pgo_installed() is True
            mock_run.assert_not_called()

    def test_a_different_installed_version_is_replaced_by_the_pin(
        self, monkeypatch
    ) -> None:
        # issue #137: a persistent runner home can carry an older build.
        monkeypatch.setenv("PATH", os.environ["PATH"])
        with (
            patch(
                "hyperi_ci.languages.rust.pgo._installed_cargo_pgo_version",
                return_value="0.2.9",
            ),
            patch(
                "hyperi_ci.languages.rust.pgo.shutil.which",
                return_value="/bin/cargo-pgo",
            ),
            patch(
                "hyperi_ci.languages.rust.pgo.subprocess.run",
                return_value=MagicMock(returncode=0),
            ) as mock_run,
        ):
            assert _ensure_cargo_pgo_installed() is True
        cmd = mock_run.call_args[0][0]
        assert cmd[cmd.index("--version") + 1] == tool_version("cargo-pgo")

    def test_not_installed_triggers_install_command(self, monkeypatch) -> None:
        monkeypatch.setenv("PATH", os.environ["PATH"])
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
        pinned = tool_version("cargo-pgo")
        assert cmd == ["cargo", "install", "cargo-pgo", "--version", pinned, "--locked"]

    def test_install_failure_returns_false(self, monkeypatch) -> None:
        monkeypatch.setenv("PATH", os.environ["PATH"])
        with (
            patch("hyperi_ci.languages.rust.pgo.shutil.which", return_value=None),
            patch(
                "hyperi_ci.languages.rust.pgo.subprocess.run",
                return_value=MagicMock(returncode=1),
            ),
        ):
            assert _ensure_cargo_pgo_installed() is False


class TestCargoPgoVersion:
    """issue #137: the installed version is read, not assumed."""

    @pytest.mark.parametrize(
        ("output", "expected"),
        [
            ("cargo-pgo 0.3.0\n", "0.3.0"),
            ("cargo-pgo-pgo 0.2.9", "0.2.9"),
            ("", None),
            ("error: no such command: `pgo`", None),
        ],
    )
    def test_parses_the_version(self, output: str, expected: str | None) -> None:
        assert cargo_pgo_version_from(output) == expected

    def test_the_pin_is_a_plain_semver(self) -> None:
        # `cargo install --version` takes it verbatim; a leading v would fail.
        assert cargo_pgo_version_from(tool_version("cargo-pgo")) == tool_version(
            "cargo-pgo"
        )


class TestBoltAvailabilityCheck:
    """BOLT toolchain discovery with versioned-binary fallback shim.

    Ubuntu ships only version-suffixed binaries (llvm-bolt-NN, merge-fdata-NN)
    via the bolt-NN package — no unversioned symlinks. cargo-pgo's BOLT flow
    invokes BOTH `llvm-bolt` and `merge-fdata` unversioned, so the shim must
    cover both and they must come from the SAME LLVM version for internal
    consistency.

    Shimming writes ~/.local/bin into ``os.environ["PATH"]``, so a test that
    reaches it reassigns PATH through monkeypatch and lets pytest restore it.
    """

    # BOLT toolchain = llvm-bolt + merge-fdata + ld.lld (all three must
    # resolve from the same LLVM version for cargo-pgo's bolt flow to
    # work — ld.lld is the linker BOLT requires for --emit-relocs metadata).
    _BOLT_TOOLS = ("llvm-bolt", "merge-fdata", "ld.lld")

    def test_all_tools_unversioned_present(self) -> None:
        """Fast path: all three unversioned binaries already on PATH."""
        with patch(
            "hyperi_ci.languages.rust.pgo.shutil.which",
            side_effect=lambda name: (
                f"/usr/bin/{name}" if name in self._BOLT_TOOLS else None
            ),
        ):
            assert _ensure_llvm_bolt_available() is True

    def test_neither_tool_available_returns_false(self) -> None:
        with patch("hyperi_ci.languages.rust.pgo.shutil.which", return_value=None):
            assert _ensure_llvm_bolt_available() is False

    def test_versioned_fallback_shims_all_three_binaries(
        self, tmp_path, monkeypatch
    ) -> None:
        """Only /usr/bin/*-22 present → shim llvm-bolt, merge-fdata, AND ld.lld."""
        monkeypatch.setenv("PATH", os.environ["PATH"])
        versioned_binaries = {
            f"{name}-22": tmp_path / f"{name}-22" for name in self._BOLT_TOOLS
        }
        for path in versioned_binaries.values():
            path.touch()

        home = tmp_path / "home"
        monkeypatch.setattr("hyperi_ci.languages.rust.pgo.Path.home", lambda: home)
        monkeypatch.setenv("HYPERCI_LLVM_VERSION", "22")

        def fake_which(name: str) -> str | None:
            if name in versioned_binaries:
                return str(versioned_binaries[name])
            return None

        with patch("hyperi_ci.languages.rust.pgo.shutil.which", fake_which):
            assert _ensure_llvm_bolt_available() is True

        shim_dir = home / ".local" / "bin"
        for tool in self._BOLT_TOOLS:
            shim = shim_dir / tool
            expected = versioned_binaries[f"{tool}-22"]
            assert shim.is_symlink(), f"{tool} shim not created"
            assert shim.resolve() == expected.resolve()
        assert str(shim_dir) in os.environ["PATH"].split(os.pathsep)

    def test_partial_toolchain_returns_false(self, tmp_path, monkeypatch) -> None:
        """llvm-bolt-22 + merge-fdata-22 present but ld.lld-22 missing → refuse.

        All three binaries must come from the same LLVM version. Without
        ld.lld, BOLT-instrumented link will fail — better to return False
        here and surface a clear 'BOLT skipped' warning.
        """
        # Two of three present — missing ld.lld
        fake_bolt = tmp_path / "llvm-bolt-22"
        fake_merge = tmp_path / "merge-fdata-22"
        fake_bolt.touch()
        fake_merge.touch()

        home = tmp_path / "home"
        monkeypatch.setattr("hyperi_ci.languages.rust.pgo.Path.home", lambda: home)

        def fake_which(name: str) -> str | None:
            if name == "llvm-bolt-22":
                return str(fake_bolt)
            if name == "merge-fdata-22":
                return str(fake_merge)
            return None

        with patch("hyperi_ci.languages.rust.pgo.shutil.which", fake_which):
            assert _ensure_llvm_bolt_available() is False
        # No shim created since we refused the partial toolchain
        assert not (home / ".local" / "bin" / "llvm-bolt").exists()

    def test_skips_versions_with_incomplete_toolchain(
        self, tmp_path, monkeypatch
    ) -> None:
        """v21 has only llvm-bolt; v22 has full trio — must pick v22 consistently."""
        monkeypatch.setenv("PATH", os.environ["PATH"])
        # v21: only llvm-bolt-21 (no merge-fdata-21, no ld.lld-21)
        v21_bolt = tmp_path / "llvm-bolt-21"
        v21_bolt.touch()
        # v22: all three present
        v22_binaries = {
            f"{name}-22": tmp_path / f"{name}-22" for name in self._BOLT_TOOLS
        }
        for path in v22_binaries.values():
            path.touch()

        home = tmp_path / "home"
        monkeypatch.setattr("hyperi_ci.languages.rust.pgo.Path.home", lambda: home)
        monkeypatch.delenv("HYPERCI_LLVM_VERSION", raising=False)

        lookup = {"llvm-bolt-21": str(v21_bolt)}
        lookup.update({name: str(path) for name, path in v22_binaries.items()})

        with patch(
            "hyperi_ci.languages.rust.pgo.shutil.which",
            side_effect=lambda name: lookup.get(name),
        ):
            assert _ensure_llvm_bolt_available() is True

        # All shims must point at v22 (complete), not v21 (partial)
        shim_dir = home / ".local" / "bin"
        for tool in self._BOLT_TOOLS:
            shim = shim_dir / tool
            expected = v22_binaries[f"{tool}-22"]
            assert shim.resolve() == expected.resolve()


class TestBoltBuildEnv:
    """BOLT steps need TWO env overrides to get a clean linker pass:

    1. `CARGO_TARGET_<TRIPLE>_RUSTFLAGS` with `-C link-arg=-fuse-ld=lld` —
       mold segfaults on `--emit-relocs`, GNU BFD rejects it, lld is the
       canonical BOLT-compatible linker.
    2. `CARGO_PROFILE_RELEASE_STRIP=none` — lld refuses to combine
       `--strip-all` with `--emit-relocs`, so projects with
       `[profile.release] strip = true` otherwise fail the BOLT build.
       Final binary is stripped by hyperi-ci's post-build packaging.
    """

    def test_amd64_linux_forces_lld_via_target_rustflags(self, monkeypatch) -> None:
        from hyperi_ci.languages.rust.pgo import _bolt_build_env

        # Default (no operator override) is exactly the lld flag, unchanged.
        monkeypatch.delenv("HYPERCI_BOLT_EXTRA_RUSTFLAGS", raising=False)
        env = _bolt_build_env("x86_64-unknown-linux-gnu")
        assert env["CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS"] == (
            "-C link-arg=-fuse-ld=lld"
        )

    def test_no_split_appends_default_flag_to_all_targets(self, monkeypatch) -> None:
        """no_split=True appends the default splitter-disable flag for EVERY target.

        This is what makes a working BOLT layer the default fleet-wide: the retry
        (see _run_bolt) rebuilds with the compiler cold-splitter disabled so BOLT
        can process the binary. Applies to every target, not one app.
        """
        from hyperi_ci.languages.rust.pgo import (
            _DEFAULT_BOLT_NO_SPLIT_RUSTFLAGS,
            _bolt_build_env,
        )

        monkeypatch.delenv("HYPERCI_BOLT_EXTRA_RUSTFLAGS", raising=False)
        for triple, key in (
            (
                "x86_64-unknown-linux-gnu",
                "CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS",
            ),
            (
                "aarch64-unknown-linux-gnu",
                "CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_RUSTFLAGS",
            ),
        ):
            env = _bolt_build_env(triple, no_split=True)
            assert env[key] == (
                f"-C link-arg=-fuse-ld=lld {_DEFAULT_BOLT_NO_SPLIT_RUSTFLAGS}"
            )

    def test_no_split_honours_env_override(self, monkeypatch) -> None:
        """HYPERCI_BOLT_EXTRA_RUSTFLAGS overrides the default no-split flag."""
        from hyperi_ci.languages.rust.pgo import _bolt_build_env

        monkeypatch.setenv(
            "HYPERCI_BOLT_EXTRA_RUSTFLAGS", "-Cllvm-args=-split-machine-functions=false"
        )
        env = _bolt_build_env("x86_64-unknown-linux-gnu", no_split=True)
        assert env["CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS"] == (
            "-C link-arg=-fuse-ld=lld -Cllvm-args=-split-machine-functions=false"
        )

    def test_no_split_env_empty_disables_extra(self, monkeypatch) -> None:
        """An empty override drops the extra flags even on the no-split pass."""
        from hyperi_ci.languages.rust.pgo import _bolt_build_env

        monkeypatch.setenv("HYPERCI_BOLT_EXTRA_RUSTFLAGS", "   ")
        env = _bolt_build_env("x86_64-unknown-linux-gnu", no_split=True)
        assert env["CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS"] == (
            "-C link-arg=-fuse-ld=lld"
        )

    def test_disables_strip_for_bolt_steps(self) -> None:
        """strip=true + --emit-relocs is rejected by lld — override to none."""
        from hyperi_ci.languages.rust.pgo import _bolt_build_env

        env = _bolt_build_env("x86_64-unknown-linux-gnu")
        assert env["CARGO_PROFILE_RELEASE_STRIP"] == "none"

    def test_arm64_linux_triple_produces_correct_env_key(self) -> None:
        from hyperi_ci.languages.rust.pgo import _bolt_build_env

        env = _bolt_build_env("aarch64-unknown-linux-gnu")
        assert "CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_RUSTFLAGS" in env
        assert env["CARGO_PROFILE_RELEASE_STRIP"] == "none"

    def test_triple_with_dots_is_sanitised_to_underscores(self) -> None:
        """Some triples have dots (e.g. Apple targets) — must become underscores."""
        from hyperi_ci.languages.rust.pgo import _bolt_build_env

        # Cargo's env var convention replaces BOTH - and . with _
        env = _bolt_build_env("aarch64-apple-darwin")
        assert "CARGO_TARGET_AARCH64_APPLE_DARWIN_RUSTFLAGS" in env


def _bolt_cargo_commands(tmp_path, target: str) -> list[list[str]]:
    """Every `cargo pgo` argv the full PGO + BOLT pipeline builds for `target`."""
    bin_dir = tmp_path / "target" / target / "release"
    bin_dir.mkdir(parents=True)
    (bin_dir / "my-bin").touch()
    (bin_dir / "my-bin-bolt-instrumented").touch()

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
            "hyperi_ci.languages.rust.pgo._run_cargo_pgo", return_value=0
        ) as mock_cargo,
        patch("hyperi_ci.languages.rust.pgo._run_workload", return_value=0),
    ):
        rc = run_pgo_build(
            target=target,
            profile=_make_profile(bolt_enabled=True),
            binary_name="my-bin",
            cwd=tmp_path,
        )
    assert rc == 0
    return [call[0][0] for call in mock_cargo.call_args_list]


class TestDropA53Veneers:
    """issue #240: the aarch64 BOLT steps drop Cortex-A53 erratum 843419 veneers.

    The linker's erratum workaround inserts branch veneers, and BOLT refuses a
    binary carrying them because relaying it invalidates the page offsets they
    were computed from. cargo-pgo's `--bolt-args` REPLACES its own default BOLT
    flags rather than extending them, so the defaults have to come back with the
    extra flag or the optimise step silently loses its whole flag set.
    """

    def test_the_instrument_stage_sends_its_defaults_and_the_flag(self) -> None:
        args = pgo._bolt_tool_args("aarch64-unknown-linux-gnu", "instrument")
        assert args[0] == "--bolt-args"
        assert args[1].split() == [
            *pgo._CARGO_PGO_INSTRUMENT_BOLT_ARGS,
            "--drop-cortex-a53-843419-veneers",
        ]

    def test_the_optimize_stage_sends_its_defaults_and_the_flag(self) -> None:
        args = pgo._bolt_tool_args("aarch64-unknown-linux-gnu", "optimize")
        assert args[0] == "--bolt-args"
        assert args[1].split() == [
            *pgo._CARGO_PGO_OPTIMIZE_BOLT_ARGS,
            "--drop-cortex-a53-843419-veneers",
        ]

    @pytest.mark.parametrize("stage", ["instrument", "optimize"])
    def test_x86_64_passes_nothing_through(
        self, stage: Literal["instrument", "optimize"]
    ) -> None:
        """amd64 has no erratum, so cargo-pgo keeps its own defaults untouched."""
        assert pgo._bolt_tool_args("x86_64-unknown-linux-gnu", stage) == []

    def test_both_aarch64_bolt_commands_carry_the_flag(self, tmp_path) -> None:
        cmds = _bolt_cargo_commands(tmp_path, "aarch64-unknown-linux-gnu")

        bolt_cmds = [cmd for cmd in cmds if cmd[0] == "bolt"]
        assert len(bolt_cmds) == 2
        for cmd in bolt_cmds:
            flags = cmd[cmd.index("--bolt-args") + 1].split()
            assert "--drop-cortex-a53-843419-veneers" in flags
            # cargo-pgo's own flag, so it goes before the `--` that starts
            # the args forwarded to cargo.
            assert cmd.index("--bolt-args") < cmd.index("--")

        # `cargo pgo build` / `optimize` reject --bolt-args -- it is BOLT-only.
        pgo_cmds = [cmd for cmd in cmds if cmd[0] != "bolt"]
        assert pgo_cmds
        assert all("--bolt-args" not in cmd for cmd in pgo_cmds)

    def test_no_x86_64_command_carries_the_flag(self, tmp_path) -> None:
        cmds = _bolt_cargo_commands(tmp_path, "x86_64-unknown-linux-gnu")

        assert [cmd for cmd in cmds if cmd[0] == "bolt"]
        for cmd in cmds:
            assert "--bolt-args" not in cmd
            assert not any("843419" in arg for arg in cmd)


class TestBoltFlagCopiesTrackTheCargoPgoPin:
    """The copied BOLT defaults have to be re-read when `tools.cargo-pgo` moves.

    `--bolt-args` replaces cargo-pgo's own default flags rather than extending
    them, so pgo.py restates them. A pin bump that leaves the copies alone
    overrides a newer default set with an older one and every other gate stays
    green, because the flags we pass are all still valid flags.
    """

    def test_the_copies_were_read_from_the_pinned_version(self) -> None:
        pinned = tool_version("cargo-pgo")
        assert pgo._CARGO_PGO_FLAGS_VERIFIED_AGAINST == pinned, (
            f"tools.cargo-pgo is now {pinned}, and _CARGO_PGO_INSTRUMENT_BOLT_ARGS "
            "/ _CARGO_PGO_OPTIMIZE_BOLT_ARGS in "
            "src/hyperi_ci/languages/rust/pgo.py are copies of cargo-pgo's own "
            "default BOLT flags, which --bolt-args replaces rather than extends. "
            "Re-read src/bolt/instrument.rs and src/bolt/optimize.rs at the "
            f"{pinned} tag of https://github.com/Kobzol/cargo-pgo, update the two "
            "tuples if the defaults changed, then set "
            f'_CARGO_PGO_FLAGS_VERIFIED_AGAINST = "{pinned}".'
        )


class TestRunBoltRetry:
    """_run_bolt retries once (splitter disabled) on a BOLT build failure.

    This is what makes a working BOLT layer the default: apps that already
    optimise cleanly succeed on the first attempt and never retry; an app whose
    build trips BOLT's split-function limitation self-heals on the no-split retry.
    """

    @staticmethod
    def _profile() -> OptimizationProfile:
        return _make_profile(bolt_enabled=True)

    def test_no_retry_when_first_attempt_succeeds(self, monkeypatch, tmp_path) -> None:
        from hyperi_ci.languages.rust import pgo

        monkeypatch.setattr(pgo, "_ensure_llvm_bolt_available", lambda: True)
        calls: list[bool] = []

        def fake_attempt(*_a: object, no_split: bool, **_k: object) -> int:
            calls.append(no_split)
            return 0

        monkeypatch.setattr(pgo, "_attempt_bolt", fake_attempt)
        rc = pgo._run_bolt("t", [], "bin", self._profile(), tmp_path, None)
        assert rc == 0
        assert calls == [False]  # first attempt only, no retry

    def test_retries_with_no_split_on_build_failure(
        self, monkeypatch, tmp_path
    ) -> None:
        from hyperi_ci.languages.rust import pgo

        monkeypatch.setattr(pgo, "_ensure_llvm_bolt_available", lambda: True)
        monkeypatch.delenv("HYPERCI_BOLT_EXTRA_RUSTFLAGS", raising=False)
        calls: list[bool] = []

        def fake_attempt(*_a: object, no_split: bool, **_k: object) -> int:
            calls.append(no_split)
            return 0 if no_split else 1  # fail first, succeed on the no-split retry

        monkeypatch.setattr(pgo, "_attempt_bolt", fake_attempt)
        rc = pgo._run_bolt("t", [], "bin", self._profile(), tmp_path, None)
        assert rc == 0
        assert calls == [False, True]

    def test_no_retry_when_env_disables_it(self, monkeypatch, tmp_path) -> None:
        from hyperi_ci.languages.rust import pgo

        monkeypatch.setattr(pgo, "_ensure_llvm_bolt_available", lambda: True)
        monkeypatch.setenv("HYPERCI_BOLT_EXTRA_RUSTFLAGS", "")  # disables the retry
        calls: list[bool] = []

        def fake_attempt(*_a: object, no_split: bool, **_k: object) -> int:
            calls.append(no_split)
            return 1

        monkeypatch.setattr(pgo, "_attempt_bolt", fake_attempt)
        rc = pgo._run_bolt("t", [], "bin", self._profile(), tmp_path, None)
        assert rc == 1
        assert calls == [False]  # no retry

    def test_skips_when_toolchain_missing(self, monkeypatch, tmp_path) -> None:
        from hyperi_ci.languages.rust import pgo

        monkeypatch.setattr(pgo, "_ensure_llvm_bolt_available", lambda: False)
        calls: list[bool] = []

        def fake_attempt(*_a: object, no_split: bool, **_k: object) -> int:
            calls.append(no_split)
            return 0

        monkeypatch.setattr(pgo, "_attempt_bolt", fake_attempt)
        rc = pgo._run_bolt("t", [], "bin", self._profile(), tmp_path, None)
        assert rc == 0  # non-fatal skip
        assert calls == []  # never attempted


class TestWorkloadDurationAndSetup:
    """issue #135: duration_secs reaches the workload; setup runs off its clock.

    Real shell commands, so the variable the script sees is the one asserted.
    """

    def test_the_workload_sees_the_configured_duration(self, tmp_path) -> None:
        out = tmp_path / "seen"
        # `sh -c '...' --` takes the appended binary path as $1 and ignores it.
        cmd = f"sh -c 'printf %s \"$PGO_WORKLOAD_DURATION_SECS\" > {out}' --"
        rc = _run_workload(cmd, 600, tmp_path / "bin", cwd=tmp_path)
        assert rc == 0
        assert out.read_text() == "600"

    def test_setup_runs_in_the_project_directory(self, tmp_path) -> None:
        rc = _run_workload_setup("touch built-driver", tmp_path)
        assert rc == 0
        assert (tmp_path / "built-driver").exists()

    def test_a_failed_setup_is_reported(self, tmp_path) -> None:
        assert _run_workload_setup("exit 7", tmp_path) == 7

    def test_setup_comes_from_config(self) -> None:
        profile = resolve_optimization_profile(
            "release",
            {
                "pgo": {
                    "enabled": True,
                    "workload_cmd": "bash w.sh",
                    "workload_setup_cmd": "cargo build -p driver",
                }
            },
        )
        assert profile.pgo_workload_setup_cmd == "cargo build -p driver"

    def test_a_failed_setup_stops_before_profiling(self, tmp_path) -> None:
        bin_dir = tmp_path / "target" / "x86_64-unknown-linux-gnu" / "release"
        bin_dir.mkdir(parents=True)
        (bin_dir / "my-bin").touch()
        profile = OptimizationProfile(
            channel="release",
            pgo_enabled=True,
            pgo_workload_cmd="bash w.sh",
            pgo_workload_setup_cmd="exit 3",
        )
        with (
            patch(
                "hyperi_ci.languages.rust.pgo._ensure_cargo_pgo_installed",
                return_value=True,
            ),
            patch("hyperi_ci.languages.rust.pgo._run_cargo_pgo", return_value=0),
            patch("hyperi_ci.languages.rust.pgo._run_workload") as workload,
        ):
            rc = run_pgo_build(
                target="x86_64-unknown-linux-gnu",
                profile=profile,
                binary_name="my-bin",
                cwd=tmp_path,
            )
        assert rc == 3
        workload.assert_not_called()


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
        docs/runtime/pgo-bolt.md and the shape of every template in
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


def _executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


class TestProfdataReachesCargoPgo:
    """cargo-pgo merges the profile with llvm-profdata, which is not on PATH.

    The rustup component installs it under the rustc sysroot, so a runner can
    have it and still fail. A self-hosted runner may not have it at all.
    """

    @staticmethod
    def _sysroot(tmp_path, monkeypatch, *, profdata: bool):
        bin_dir = tmp_path / "lib" / "rustlib" / "x86_64-unknown-linux-gnu" / "bin"
        bin_dir.mkdir(parents=True)
        if profdata:
            _executable(bin_dir / "llvm-profdata")
        monkeypatch.setattr(pgo.shutil, "which", lambda _name: None)
        monkeypatch.setattr(pgo, "_rustc_sysroot_bin", lambda: bin_dir)
        monkeypatch.setenv("PATH", "/usr/bin")
        return bin_dir

    def test_the_sysroot_copy_is_put_on_path(self, tmp_path, monkeypatch) -> None:
        bin_dir = self._sysroot(tmp_path, monkeypatch, profdata=True)
        assert pgo._ensure_llvm_profdata_available() is True
        assert str(bin_dir) in os.environ["PATH"].split(os.pathsep)

    def test_a_missing_component_is_installed(self, tmp_path, monkeypatch) -> None:
        bin_dir = self._sysroot(tmp_path, monkeypatch, profdata=False)
        calls: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            calls.append(cmd)
            _executable(bin_dir / "llvm-profdata")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(pgo, "run_cmd", fake_run)
        assert pgo._ensure_llvm_profdata_available() is True
        assert ["rustup", "component", "add", "llvm-tools-preview"] in calls

    def test_an_unresolvable_sysroot_is_reported(self, monkeypatch) -> None:
        monkeypatch.setattr(pgo.shutil, "which", lambda _name: None)
        monkeypatch.setattr(pgo, "_rustc_sysroot_bin", lambda: None)
        assert pgo._ensure_llvm_profdata_available() is False

    def test_the_build_refuses_before_spending_the_workload(
        self, tmp_path, monkeypatch
    ) -> None:
        """The failure this exists to stop: 300s of profiling, then no merge."""
        monkeypatch.setattr(pgo, "_ensure_cargo_pgo_installed", lambda: True)
        monkeypatch.setattr(pgo, "_ensure_ld_lld_available", lambda: True)
        monkeypatch.setattr(pgo, "_ensure_llvm_profdata_available", lambda: False)
        workload: list[str] = []
        monkeypatch.setattr(
            pgo, "_run_workload", lambda *a, **k: workload.append("ran") or 0
        )
        cargo: list[str] = []
        monkeypatch.setattr(
            pgo, "_run_cargo_pgo", lambda *a, **k: cargo.append("ran") or 0
        )

        rc = run_pgo_build(
            target="x86_64-unknown-linux-gnu",
            profile=_make_profile(),
            binary_name="app",
            cwd=tmp_path,
        )

        assert rc == 1
        assert workload == []
        assert cargo == []


class TestLinkerForThePgoSteps:
    """issue #142: lld resolves in every stage; a big aarch64 link gets mold."""

    def test_a_versioned_ld_lld_is_shimmed_unversioned(
        self, tmp_path, monkeypatch
    ) -> None:
        versioned = _executable(tmp_path / "usr-bin" / "ld.lld-19")
        monkeypatch.setenv("PATH", str(versioned.parent))
        assert _ensure_ld_lld_available() is True
        shim = tmp_path / "home" / ".local" / "bin" / "ld.lld"
        assert shim.resolve() == versioned.resolve()

    def test_no_lld_at_all_is_reported(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        assert _ensure_ld_lld_available() is False

    @staticmethod
    def _run(tmp_path, target: str, results: list[int]):
        with (
            patch(
                "hyperi_ci.languages.rust.pgo._ensure_cargo_pgo_installed",
                return_value=True,
            ),
            patch(
                "hyperi_ci.languages.rust.pgo._ensure_llvm_profdata_available",
                return_value=True,
            ),
            patch(
                "hyperi_ci.languages.rust.pgo._run_cargo_pgo", side_effect=results
            ) as cargo,
        ):
            rc = run_pgo_build(
                target=target,
                profile=_make_profile(),
                binary_name="my-bin",
                cwd=tmp_path,
            )
        return rc, cargo

    def test_a_failed_aarch64_instrument_link_retries_with_mold(
        self, tmp_path, monkeypatch
    ) -> None:
        mold = _executable(tmp_path / "tools" / "mold")
        monkeypatch.setenv("PATH", str(mold.parent))
        rc, cargo = self._run(tmp_path, "aarch64-unknown-linux-gnu", [1, 1])
        assert rc == 1
        assert cargo.call_count == 2
        retry_env = cargo.call_args_list[1].kwargs["extra_env"]
        key = "CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_RUSTFLAGS"
        assert retry_env[key] == "-C link-arg=-fuse-ld=mold"

    def test_the_mold_retry_carries_into_the_optimise_link(
        self, tmp_path, monkeypatch
    ) -> None:
        """The optimised binary is no smaller, so it needs the same linker."""
        target = "aarch64-unknown-linux-gnu"
        mold = _executable(tmp_path / "tools" / "mold")
        monkeypatch.setenv("PATH", str(mold.parent))
        bin_dir = tmp_path / "target" / target / "release"
        bin_dir.mkdir(parents=True)
        (bin_dir / "my-bin").touch()

        with (
            patch(
                "hyperi_ci.languages.rust.pgo._ensure_cargo_pgo_installed",
                return_value=True,
            ),
            patch(
                "hyperi_ci.languages.rust.pgo._ensure_llvm_profdata_available",
                return_value=True,
            ),
            patch("hyperi_ci.languages.rust.pgo._run_workload", return_value=0),
            patch(
                "hyperi_ci.languages.rust.pgo._run_cargo_pgo", side_effect=[1, 0, 0]
            ) as cargo,
        ):
            rc = run_pgo_build(
                target=target,
                profile=_make_profile(),
                binary_name="my-bin",
                cwd=tmp_path,
            )

        assert rc == 0
        optimise_call = cargo.call_args_list[2]
        assert optimise_call.args[0][0] == "optimize"
        key = "CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_RUSTFLAGS"
        assert optimise_call.kwargs["extra_env"][key] == "-C link-arg=-fuse-ld=mold"

    def test_an_x86_64_instrument_failure_is_not_retried(
        self, tmp_path, monkeypatch
    ) -> None:
        mold = _executable(tmp_path / "tools" / "mold")
        monkeypatch.setenv("PATH", str(mold.parent))
        rc, cargo = self._run(tmp_path, "x86_64-unknown-linux-gnu", [1])
        assert rc == 1
        assert cargo.call_count == 1


class TestReleaseDir:
    """issue #135: PGO and BOLT look where packaging copies from."""

    TARGET = "x86_64-unknown-linux-gnu"

    def test_defaults_to_target_under_the_project(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
        assert _release_dir(tmp_path, self.TARGET) == (
            tmp_path / "target" / self.TARGET / "release"
        )

    def test_honours_an_absolute_cargo_target_dir(self, tmp_path, monkeypatch) -> None:
        elsewhere = tmp_path / "shared-target"
        monkeypatch.setenv("CARGO_TARGET_DIR", str(elsewhere))
        assert _release_dir(tmp_path / "project", self.TARGET) == (
            elsewhere / self.TARGET / "release"
        )

    def test_a_relative_cargo_target_dir_is_under_the_project(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("CARGO_TARGET_DIR", "build-out")
        assert _release_dir(tmp_path, self.TARGET) == (
            tmp_path / "build-out" / self.TARGET / "release"
        )

    def test_the_instrumented_binary_follows_it(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("CARGO_TARGET_DIR", str(tmp_path / "t"))
        path = _instrumented_binary_path(tmp_path, self.TARGET, "app", variant="bolt")
        assert (
            path == tmp_path / "t" / self.TARGET / "release" / "app-bolt-instrumented"
        )


class TestInstallBoltOutput:
    """issue #136: BOLT's file, not the PGO-only one, is what packaging ships."""

    TARGET = "x86_64-unknown-linux-gnu"

    @pytest.fixture
    def release(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
        release = tmp_path / "target" / self.TARGET / "release"
        release.mkdir(parents=True)
        (release / "app").write_bytes(b"pgo-only cargo output")
        return release

    def test_installs_the_bolt_file_over_the_packaged_name(
        self, tmp_path, release, make_elf
    ) -> None:
        bolt = make_elf(release / "app-bolt-optimized", [".text", BOLT_NOTE_SECTION])
        assert _install_bolt_output(tmp_path, self.TARGET, "app") is True
        assert (release / "app").read_bytes() == bolt.read_bytes()

    def test_no_bolt_file_leaves_the_pgo_binary(self, tmp_path, release) -> None:
        assert _install_bolt_output(tmp_path, self.TARGET, "app") is False
        assert (release / "app").read_bytes() == b"pgo-only cargo output"

    def test_a_file_without_the_bolt_note_is_refused(
        self, tmp_path, release, make_elf
    ) -> None:
        make_elf(release / "app-bolt-optimized", [".text"])
        assert _install_bolt_output(tmp_path, self.TARGET, "app") is False
        assert (release / "app").read_bytes() == b"pgo-only cargo output"


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
        # BOLT instrument produces its own binary; the workload must run on it.
        (bin_dir / "my-bin-bolt-instrumented").touch()

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
            patch(
                "hyperi_ci.languages.rust.pgo._run_workload", return_value=0
            ) as mock_workload,
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
        # Workload runs TWICE — PGO and BOLT each need their own profile data,
        # else BOLT optimises against nothing (#29).
        assert mock_workload.call_count == 2
        bolt_workload_bin = str(mock_workload.call_args_list[1][0][2])
        assert bolt_workload_bin.endswith("my-bin-bolt-instrumented")

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

    def test_declared_features_reach_every_cargo_line(self, tmp_path) -> None:
        """A PGO+BOLT release carries `build.rust.features` on all four builds.

        The PGO path used to render the allocator alone, so a release image
        was compiled without the engines its config declared and refused
        them at load (#130).
        """
        bin_dir = tmp_path / "target" / "x86_64-unknown-linux-gnu" / "release"
        bin_dir.mkdir(parents=True)
        (bin_dir / "my-bin").touch()
        (bin_dir / "my-bin-bolt-instrumented").touch()

        declared = ("db-clickhouse", "db-mongodb", "file-tail")
        profile = _make_profile(allocator="jemalloc", bolt_enabled=True)
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
                # Exactly what the dispatcher passes for a YAML features list.
                extra_env={
                    "RUST_FEATURES": "jemalloc|db-clickhouse|db-mongodb|file-tail",
                    "RUST_ALL_FEATURES": "false",
                },
            )
        assert rc == 0
        # PGO build + PGO optimize + BOLT build + BOLT optimize
        assert mock_cargo.call_count == 4
        for call in mock_cargo.call_args_list:
            args = call[0][0]
            selected = args[args.index("--features") + 1].split(",")
            assert "jemalloc" in selected, args
            for feature in declared:
                assert feature in selected, args

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


class TestOutcomeRecording:
    """The outcome records stages that completed, not stages that were asked for.

    Every BOLT skip below returns 0, so the return code cannot distinguish an
    optimised binary from a fallback — only the outcome can.
    """

    @staticmethod
    def _seed(tmp_path, make_elf=None, *, bolt_instrumented: bool):
        bin_dir = tmp_path / "target" / "x86_64-unknown-linux-gnu" / "release"
        bin_dir.mkdir(parents=True)
        (bin_dir / "my-bin").touch()
        if bolt_instrumented:
            (bin_dir / "my-bin-bolt-instrumented").touch()
        if make_elf:
            # What `cargo pgo bolt optimize` leaves beside the cargo output.
            make_elf(bin_dir / "my-bin-bolt-optimized", [".text", BOLT_NOTE_SECTION])
        return bin_dir

    def test_bolt_applied_when_the_whole_pipeline_runs(
        self, tmp_path, make_elf
    ) -> None:
        bin_dir = self._seed(tmp_path, make_elf, bolt_instrumented=True)
        outcome = OptimizationOutcome(allocator="jemalloc")
        with (
            patch(
                "hyperi_ci.languages.rust.pgo._ensure_cargo_pgo_installed",
                return_value=True,
            ),
            patch(
                "hyperi_ci.languages.rust.pgo._ensure_llvm_bolt_available",
                return_value=True,
            ),
            patch("hyperi_ci.languages.rust.pgo._run_cargo_pgo", return_value=0),
            patch("hyperi_ci.languages.rust.pgo._run_workload", return_value=0),
        ):
            rc = run_pgo_build(
                target="x86_64-unknown-linux-gnu",
                profile=_make_profile(bolt_enabled=True),
                binary_name="my-bin",
                cwd=tmp_path,
                outcome=outcome,
            )
        assert rc == 0
        assert outcome.describe() == "optimised: pgo=yes bolt=yes allocator=jemalloc"
        # Packaging copies the unsuffixed name, so the BOLT file must now be it.
        assert (bin_dir / "my-bin").read_bytes() == (
            bin_dir / "my-bin-bolt-optimized"
        ).read_bytes()

    def test_bolt_success_without_its_output_leaves_bolt_unapplied(
        self, tmp_path
    ) -> None:
        # cargo-pgo exits 0 but no -bolt-optimized file exists: the PGO-only
        # binary ships, and the outcome must say so.
        self._seed(tmp_path, bolt_instrumented=True)
        outcome = OptimizationOutcome(allocator="jemalloc")
        with (
            patch(
                "hyperi_ci.languages.rust.pgo._ensure_cargo_pgo_installed",
                return_value=True,
            ),
            patch(
                "hyperi_ci.languages.rust.pgo._ensure_llvm_bolt_available",
                return_value=True,
            ),
            patch("hyperi_ci.languages.rust.pgo._run_cargo_pgo", return_value=0),
            patch("hyperi_ci.languages.rust.pgo._run_workload", return_value=0),
        ):
            rc = run_pgo_build(
                target="x86_64-unknown-linux-gnu",
                profile=_make_profile(bolt_enabled=True),
                binary_name="my-bin",
                cwd=tmp_path,
                outcome=outcome,
            )
        assert rc == 0
        assert outcome.describe() == "optimised: pgo=yes bolt=no allocator=jemalloc"

    def test_missing_bolt_toolchain_leaves_bolt_unapplied(self, tmp_path) -> None:
        self._seed(tmp_path, bolt_instrumented=False)
        outcome = OptimizationOutcome(allocator="jemalloc")
        with (
            patch(
                "hyperi_ci.languages.rust.pgo._ensure_cargo_pgo_installed",
                return_value=True,
            ),
            patch(
                "hyperi_ci.languages.rust.pgo._ensure_llvm_bolt_available",
                return_value=False,
            ),
            patch("hyperi_ci.languages.rust.pgo._run_cargo_pgo", return_value=0),
            patch("hyperi_ci.languages.rust.pgo._run_workload", return_value=0),
        ):
            rc = run_pgo_build(
                target="x86_64-unknown-linux-gnu",
                profile=_make_profile(bolt_enabled=True),
                binary_name="my-bin",
                cwd=tmp_path,
                outcome=outcome,
            )
        assert rc == 0
        assert outcome.describe() == "optimised: pgo=yes bolt=no allocator=jemalloc"

    def test_failed_bolt_workload_leaves_bolt_unapplied(self, tmp_path) -> None:
        self._seed(tmp_path, bolt_instrumented=True)
        outcome = OptimizationOutcome(allocator="jemalloc")
        with (
            patch(
                "hyperi_ci.languages.rust.pgo._ensure_cargo_pgo_installed",
                return_value=True,
            ),
            patch(
                "hyperi_ci.languages.rust.pgo._ensure_llvm_bolt_available",
                return_value=True,
            ),
            patch("hyperi_ci.languages.rust.pgo._run_cargo_pgo", return_value=0),
            patch("hyperi_ci.languages.rust.pgo._run_workload", side_effect=[0, 3]),
        ):
            rc = run_pgo_build(
                target="x86_64-unknown-linux-gnu",
                profile=_make_profile(bolt_enabled=True),
                binary_name="my-bin",
                cwd=tmp_path,
                outcome=outcome,
            )
        assert rc == 0
        assert outcome.describe() == "optimised: pgo=yes bolt=no allocator=jemalloc"


class TestBuildHandsFeaturesToPgo:
    """`_build_for_target` gives the PGO path the features it was handed.

    The PGO path renders its own cargo lines, so the features have to
    survive the handoff and not just the plain-build branch.
    """

    def test_declared_features_reach_the_pgo_path(self, tmp_path, monkeypatch) -> None:
        from hyperi_ci.languages.rust import build

        monkeypatch.chdir(tmp_path)
        bin_dir = tmp_path / "target" / "x86_64-unknown-linux-gnu" / "release"
        bin_dir.mkdir(parents=True)
        (bin_dir / "my-bin").touch()

        monkeypatch.setattr(build, "_ensure_target_installed", lambda _target: True)
        monkeypatch.setattr(build, "_detect_binary_names", lambda: ["my-bin"])

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
            rc = build._build_for_target(
                "x86_64-unknown-linux-gnu",
                "db-clickhouse|file-tail",
                False,
                {},
                profile=_make_profile(),
            )

        assert rc == 0
        assert mock_cargo.call_count == 2
        for call in mock_cargo.call_args_list:
            args = call[0][0]
            selected = args[args.index("--features") + 1].split(",")
            assert selected == ["db-clickhouse", "file-tail", "jemalloc"], args


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
