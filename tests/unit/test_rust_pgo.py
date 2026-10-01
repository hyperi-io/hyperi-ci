# Project:   HyperI CI
# File:      tests/unit/test_rust_pgo.py
# Purpose:   Unit tests for PGO/BOLT build orchestration
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

import json
import os
import subprocess
import sys
from pathlib import Path
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
    monkeypatch.setattr(pgo, "_BOLT_LINKER_DIR", tmp_path / "bolt-linker")
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
    via the bolt-NN package -- no unversioned symlinks. cargo-pgo's BOLT flow
    invokes BOTH `llvm-bolt` and `merge-fdata` unversioned, so the shim must
    cover both and they must come from the SAME LLVM version for internal
    consistency.

    Shimming writes ~/.local/bin into ``os.environ["PATH"]``, so a test that
    reaches it reassigns PATH through monkeypatch and lets pytest restore it.
    """

    # BOLT toolchain = llvm-bolt + merge-fdata + ld.lld (all three must
    # resolve from the same LLVM version for cargo-pgo's bolt flow to
    # work -- ld.lld is the linker BOLT requires for --emit-relocs metadata).
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
        ld.lld, BOLT-instrumented link will fail -- better to return False
        here and surface a clear 'BOLT skipped' warning.
        """
        # Two of three present -- missing ld.lld
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
        """v21 has only llvm-bolt; v22 has full trio -- must pick v22 consistently."""
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
    """BOLT steps link with lld through `CARGO_TARGET_<TRIPLE>_RUSTFLAGS`.

    mold segfaults on `--emit-relocs` and GNU BFD rejects it. The strip
    override lives in `_bolt_profile_env`, which every compile carries
    (TestEveryCompileSharesTheProfile).
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

    def test_sets_no_profile_settings(self) -> None:
        """A profile setting here would rename symbols in the BOLT steps alone."""
        from hyperi_ci.languages.rust.pgo import _bolt_build_env

        for triple in ("x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu"):
            for no_split in (False, True):
                env = _bolt_build_env(triple, no_split=no_split)
                assert not [k for k in env if k.startswith("CARGO_PROFILE_")], env

    def test_arm64_linux_triple_produces_correct_env_key(self) -> None:
        from hyperi_ci.languages.rust.pgo import _bolt_build_env

        env = _bolt_build_env("aarch64-unknown-linux-gnu")
        assert "CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_RUSTFLAGS" in env

    def test_triple_with_dots_is_sanitised_to_underscores(self) -> None:
        """Some triples have dots (e.g. Apple targets) -- must become underscores."""
        from hyperi_ci.languages.rust.pgo import _bolt_build_env

        # Cargo's env var convention replaces BOTH - and . with _
        env = _bolt_build_env("aarch64-apple-darwin")
        assert "CARGO_TARGET_AARCH64_APPLE_DARWIN_RUSTFLAGS" in env


def _bolt_cargo_commands(tmp_path, target: str) -> list[list[str]]:
    """Every `cargo pgo` argv the full PGO + BOLT pipeline builds for `target`."""
    return [argv for argv, _env in _bolt_cargo_calls(tmp_path, target)]


def _bolt_cargo_calls(
    tmp_path, target: str
) -> list[tuple[list[str], dict[str, str] | None]]:
    """Every `cargo pgo` argv and extra env the full PGO + BOLT pipeline builds."""
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
    return [(call[0][0], call[1]["extra_env"]) for call in mock_cargo.call_args_list]


_AARCH64_LINKER_KEY = "CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER"

_FAKE_LINKER = """\
#!{python}
import json
import sys
from pathlib import Path

files = {{
    arg: Path(arg[1:]).read_text(encoding="utf-8")
    for arg in sys.argv[1:]
    if arg.startswith("@")
}}
record = {{"argv0": sys.argv[0], "args": sys.argv[1:], "files": files}}
Path({record!r}).write_text(json.dumps(record), encoding="utf-8")
"""


def _fake_linker(tmp_path) -> tuple[str, Path]:
    """A linker driver that records its argv, and any @file contents, as JSON."""
    record = tmp_path / "linker-record.json"
    linker = tmp_path / "fake-bin" / "fake-cc"
    linker.parent.mkdir()
    linker.write_text(
        _FAKE_LINKER.format(python=sys.executable, record=str(record)),
        encoding="utf-8",
    )
    linker.chmod(0o755)
    return str(linker), record


def _link_through_wrapper(tmp_path, *args: str) -> dict:
    """Run the generated wrapper over the fake linker and return what it saw."""
    real, record = _fake_linker(tmp_path)
    wrapper = pgo._a53_strip_linker(real)
    subprocess.run([str(wrapper), *args], check=True)
    return json.loads(record.read_text(encoding="utf-8"))


class TestA53StripLinker:
    """issue #262: the aarch64 BOLT link runs without the erratum 843419 fix.

    rustc's aarch64-unknown-linux-gnu target spec passes
    `-Wl,--fix-cortex-a53-843419` to the linker driver, and an Ubuntu gcc adds
    `--fix-cortex-a53-843419` from its own spec too. The veneers that fix
    inserts are what llvm-bolt refuses, so the BOLT steps link through a
    wrapper that drops rustc's flag and adds `-mno-fix-cortex-a53-843419`.
    """

    def test_aarch64_bolt_env_points_the_linker_at_the_wrapper(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real, _ = _fake_linker(tmp_path)
        monkeypatch.setenv(_AARCH64_LINKER_KEY, real)

        env = pgo._bolt_build_env("aarch64-unknown-linux-gnu")

        wrapper = env[_AARCH64_LINKER_KEY]
        assert wrapper != real
        assert wrapper.startswith(str(tmp_path / "bolt-linker"))
        assert os.access(wrapper, os.X_OK)
        assert real in Path(wrapper).read_text(encoding="utf-8")

    def test_x86_64_bolt_env_leaves_the_linker_alone(self, tmp_path) -> None:
        env = pgo._bolt_build_env("x86_64-unknown-linux-gnu")

        assert not any(key.endswith("_LINKER") for key in env)
        assert not (tmp_path / "bolt-linker").exists()

    def test_the_fix_flag_is_dropped_and_everything_else_passes_in_order(
        self, tmp_path
    ) -> None:
        args = [
            "-Wl,--fix-cortex-a53-843419",
            "-fuse-ld=lld",
            "/build/dir with space/main.o",
            "-Wl,--as-needed",
            "-Wl,--fix-cortex-a53-843419",
            "-o",
            "/build/out",
        ]

        seen = _link_through_wrapper(tmp_path, *args)

        assert seen["argv0"] == str(tmp_path / "fake-bin" / "fake-cc")
        assert seen["args"] == [
            "-fuse-ld=lld",
            "/build/dir with space/main.o",
            "-Wl,--as-needed",
            "-o",
            "/build/out",
            "-mno-fix-cortex-a53-843419",
        ]

    def test_a_similar_but_different_flag_is_kept(self, tmp_path) -> None:
        seen = _link_through_wrapper(
            tmp_path, "-Wl,--fix-cortex-a53-835769", "-mfix-cortex-a53-843419"
        )
        assert seen["args"] == [
            "-Wl,--fix-cortex-a53-835769",
            "-mfix-cortex-a53-843419",
            "-mno-fix-cortex-a53-843419",
        ]

    def test_a_response_file_is_rewritten_without_the_flag(self, tmp_path) -> None:
        """rustc falls back to `@linker-arguments` past ARG_MAX, one arg a line."""
        response = tmp_path / "rustc-tmp" / "linker-arguments"
        response.parent.mkdir()
        lines = [
            "-Wl,--fix-cortex-a53-843419",
            "-fuse-ld=lld",
            "/build/dir\\ with\\ space/main.o",
            "-Wl,--gc-sections",
            "-o",
            "/build/out",
        ]
        original = "".join(f"{line}\n" for line in lines)
        response.write_text(original, encoding="utf-8")

        seen = _link_through_wrapper(tmp_path, f"@{response}")

        rewritten = f"@{response}.no-a53-fix"
        assert seen["args"] == [rewritten, "-mno-fix-cortex-a53-843419"]
        assert seen["files"][rewritten] == "".join(f"{line}\n" for line in lines[1:])
        assert response.read_text(encoding="utf-8") == original

    def test_a_response_file_without_the_flag_passes_unchanged(self, tmp_path) -> None:
        response = tmp_path / "linker-arguments"
        response.write_text("-fuse-ld=lld\n-o\n/build/out\n", encoding="utf-8")

        seen = _link_through_wrapper(tmp_path, f"@{response}")

        assert seen["args"] == [f"@{response}", "-mno-fix-cortex-a53-843419"]
        assert not (tmp_path / "linker-arguments.no-a53-fix").exists()

    def test_the_wrapper_name_follows_its_content(self, tmp_path) -> None:
        first = pgo._a53_strip_linker("/usr/bin/aarch64-linux-gnu-gcc")
        again = pgo._a53_strip_linker("/usr/bin/aarch64-linux-gnu-gcc")
        other = pgo._a53_strip_linker("/usr/bin/cc")

        assert first == again
        assert other != first
        assert sorted(p.name for p in (tmp_path / "bolt-linker").iterdir()) == sorted(
            {first.name, other.name}
        )


class TestRealAarch64Linker:
    """The wrapper execs the linker the BOLT link would have run without it."""

    _TARGET = "aarch64-unknown-linux-gnu"

    @staticmethod
    def _which(monkeypatch: pytest.MonkeyPatch, found: dict[str, str]) -> None:
        monkeypatch.setattr(pgo.shutil, "which", found.get)

    @pytest.mark.parametrize(
        ("build_env", "process_env", "on_path", "expected"),
        [
            (
                "build-linker",
                "process-linker",
                ["aarch64-linux-gnu-gcc", "cc"],
                "/opt/build-linker",
            ),
            (
                None,
                "process-linker",
                ["aarch64-linux-gnu-gcc", "cc"],
                "/opt/process-linker",
            ),
            (None, None, ["aarch64-linux-gnu-gcc", "cc"], "/opt/aarch64-linux-gnu-gcc"),
            (None, None, ["cc"], "/opt/cc"),
            ("missing", "missing", ["cc"], "/opt/cc"),
            (None, None, [], None),
        ],
    )
    def test_resolution_order(
        self,
        monkeypatch: pytest.MonkeyPatch,
        build_env: str | None,
        process_env: str | None,
        on_path: list[str],
        expected: str | None,
    ) -> None:
        found = {name: f"/opt/{name}" for name in on_path}
        for name in (build_env, process_env):
            if name and name != "missing":
                found[name] = f"/opt/{name}"
        self._which(monkeypatch, found)
        if process_env:
            monkeypatch.setenv(_AARCH64_LINKER_KEY, process_env)
        else:
            monkeypatch.delenv(_AARCH64_LINKER_KEY, raising=False)
        extra = {_AARCH64_LINKER_KEY: build_env} if build_env else None

        assert pgo._real_aarch64_linker(self._TARGET, extra) == expected

    def test_no_resolvable_linker_leaves_the_env_without_a_wrapper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._which(monkeypatch, {})
        monkeypatch.delenv(_AARCH64_LINKER_KEY, raising=False)

        env = pgo._bolt_build_env(self._TARGET)

        assert _AARCH64_LINKER_KEY not in env

    def test_the_build_env_linker_reaches_the_wrapper(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real, _ = _fake_linker(tmp_path)
        monkeypatch.delenv(_AARCH64_LINKER_KEY, raising=False)

        env = pgo._bolt_build_env(self._TARGET, extra_env={_AARCH64_LINKER_KEY: real})

        assert real in Path(env[_AARCH64_LINKER_KEY]).read_text(encoding="utf-8")


class TestBoltCommandsAfterTheA53Change:
    """No BOLT command asks llvm-bolt to drop veneers; the link never made any."""

    def test_aarch64_bolt_commands_link_through_the_wrapper(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real, _ = _fake_linker(tmp_path)
        monkeypatch.setenv(_AARCH64_LINKER_KEY, real)

        calls = _bolt_cargo_calls(tmp_path, "aarch64-unknown-linux-gnu")

        bolt_calls = [(argv, env) for argv, env in calls if argv[0] == "bolt"]
        assert len(bolt_calls) == 2
        for argv, env in bolt_calls:
            assert "--bolt-args" not in argv
            assert not any("843419" in arg for arg in argv)
            assert env is not None
            assert env[_AARCH64_LINKER_KEY].startswith(str(tmp_path / "bolt-linker"))

    def test_no_x86_64_command_carries_a_bolt_flag_or_a_linker(self, tmp_path) -> None:
        calls = _bolt_cargo_calls(tmp_path, "x86_64-unknown-linux-gnu")

        assert [argv for argv, _ in calls if argv[0] == "bolt"]
        for argv, env in calls:
            assert "--bolt-args" not in argv
            assert not any("843419" in arg for arg in argv)
            assert not any(key.endswith("_LINKER") for key in env or {})


class TestBoltOptimizeArgsOverride:
    """issue #262: a validate-only dispatch replaces the BOLT optimise flags.

    Bisecting a BOLT fault needs one flag set per run, and the flag set was a
    code constant. build.py owns the refusal on a run that ships; these cover
    what the override does once it is allowed.
    """

    _AARCH64 = "aarch64-unknown-linux-gnu"
    _X86_64 = "x86_64-unknown-linux-gnu"

    @pytest.fixture(autouse=True)
    def _unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(pgo.BOLT_OPTIMIZE_ARGS_ENV, raising=False)

    @pytest.mark.parametrize("raw", [None, "", "   ", "\n\t"])
    def test_unset_or_blank_is_no_override(
        self, monkeypatch: pytest.MonkeyPatch, raw: str | None
    ) -> None:
        if raw is not None:
            monkeypatch.setenv(pgo.BOLT_OPTIMIZE_ARGS_ENV, raw)
        assert pgo.bolt_optimize_args_override() is None

    def test_unset_passes_no_bolt_args(self) -> None:
        """Unset keeps cargo-pgo's own llvm-bolt flags, aarch64 included."""
        assert pgo._bolt_optimize_args() == []

    def test_tokens_split_on_any_whitespace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            pgo.BOLT_OPTIMIZE_ARGS_ENV, "  -relocs\t-lite=1\n--split-functions=2 "
        )
        assert pgo.bolt_optimize_args_override() == (
            "-relocs",
            "-lite=1",
            "--split-functions=2",
        )

    @pytest.mark.parametrize(
        "bad",
        [
            "relocs",
            "-relocs; rm -rf /",
            "-relocs|tee",
            "-o=$(id)",
            "-data=`id`",
            "-print-only='main'",
            '-print-only="main"',
            "-relocs&",
            "-x>out",
            "-data=/tmp/profile",
            "-",
            "---",
            "-a\\b",
        ],
    )
    def test_a_token_that_is_not_one_dash_option_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, bad: str
    ) -> None:
        monkeypatch.setenv(pgo.BOLT_OPTIMIZE_ARGS_ENV, f"-relocs {bad}")
        with pytest.raises(ValueError, match="bolt-optimize-args rejects"):
            pgo.bolt_optimize_args_override()

    def test_the_override_is_passed_as_given(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(pgo.BOLT_OPTIMIZE_ARGS_ENV, "-relocs -lite=1")
        assert pgo._bolt_optimize_args() == ["--bolt-args", "-relocs -lite=1"]

    @pytest.mark.parametrize(
        "spelling",
        [
            "-drop-cortex-a53-843419-veneers=false",
            "--drop-cortex-a53-843419-veneers=0",
            "--drop-cortex-a53-843419-veneers",
        ],
    )
    def test_an_override_naming_the_veneer_option_keeps_it(
        self, monkeypatch: pytest.MonkeyPatch, spelling: str
    ) -> None:
        monkeypatch.setenv(pgo.BOLT_OPTIMIZE_ARGS_ENV, f"-relocs {spelling}")
        assert pgo._bolt_optimize_args() == ["--bolt-args", f"-relocs {spelling}"]

    @pytest.mark.parametrize("target", [_AARCH64, _X86_64])
    def test_the_override_reaches_only_the_bolt_optimize_command(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path, target: str
    ) -> None:
        monkeypatch.setenv(pgo.BOLT_OPTIMIZE_ARGS_ENV, "-relocs -lite=1")
        monkeypatch.setenv(_AARCH64_LINKER_KEY, _fake_linker(tmp_path)[0])
        cmds = _bolt_cargo_commands(tmp_path, target)
        optimize = next(c for c in cmds if c[:2] == ["bolt", "optimize"])
        assert optimize[optimize.index("--bolt-args") + 1] == "-relocs -lite=1"
        # cargo-pgo's own flag, so it goes before the `--` that starts the args
        # forwarded to cargo.
        assert optimize.index("--bolt-args") < optimize.index("--")
        build_cmd = next(c for c in cmds if c[:2] == ["bolt", "build"])
        assert "--bolt-args" not in build_cmd


class TestBoltFlagCopiesTrackTheCargoPgoPin:
    """The copied BOLT defaults have to be re-read when `tools.cargo-pgo` moves.

    `--bolt-args` replaces cargo-pgo's own default flags rather than extending
    them, so pgo.py restates the optimise set as the start of a flag bisect. A
    pin bump that leaves the copy alone sends a bisect off from an older default
    set, and every other gate stays green.
    """

    def test_the_copies_were_read_from_the_pinned_version(self) -> None:
        pinned = tool_version("cargo-pgo")
        assert pgo._CARGO_PGO_FLAGS_VERIFIED_AGAINST == pinned, (
            f"tools.cargo-pgo is now {pinned}, and _CARGO_PGO_OPTIMIZE_BOLT_ARGS in "
            "src/hyperi_ci/languages/rust/pgo.py is a copy of cargo-pgo's own "
            "default BOLT optimise flags, the starting set for a "
            "HYPERCI_BOLT_OPTIMIZE_ARGS bisect. Re-read src/bolt/optimize.rs at the "
            f"{pinned} tag of https://github.com/Kobzol/cargo-pgo, update the "
            "tuple if the defaults changed, then set "
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

        monkeypatch.setenv("HYPERCI_BOLT_EXTRA_RUSTFLAGS", "")  # disables the retry
        calls: list[bool] = []

        def fake_attempt(*_a: object, no_split: bool, **_k: object) -> int:
            calls.append(no_split)
            return 1

        monkeypatch.setattr(pgo, "_attempt_bolt", fake_attempt)
        rc = pgo._run_bolt("t", [], "bin", self._profile(), tmp_path, None)
        assert rc == 1
        assert calls == [False]  # no retry


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
        # first positional argument -- binary path appended + properly quoted.
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
        monkeypatch.setattr(
            pgo.shutil,
            "which",
            lambda name: "/usr/bin/rustup" if name == "rustup" else None,
        )
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

    def test_the_sysroot_copy_wins_over_one_already_on_path(
        self, tmp_path, monkeypatch
    ) -> None:
        """Which arch we are on must not decide which llvm-profdata merges.

        A self-hosted image may publish an unversioned `llvm-profdata` while
        the GitHub-hosted arm64 image publishes none, so taking PATH first
        silently merges with a different LLVM per runner.
        """
        bin_dir = self._sysroot(tmp_path, monkeypatch, profdata=True)
        monkeypatch.setattr(pgo.shutil, "which", lambda _name: "/usr/bin/llvm-profdata")
        assert pgo._ensure_llvm_profdata_available() is True
        assert os.environ["PATH"].split(os.pathsep)[0] == str(bin_dir)

    def test_path_is_the_fallback_when_the_component_cannot_be_added(
        self, tmp_path, monkeypatch
    ) -> None:
        self._sysroot(tmp_path, monkeypatch, profdata=False)
        monkeypatch.setattr(
            pgo, "run_cmd", lambda cmd, **_k: subprocess.CompletedProcess(cmd, 1)
        )
        monkeypatch.setattr(pgo.shutil, "which", lambda _name: "/usr/bin/llvm-profdata")
        assert pgo._ensure_llvm_profdata_available() is True

    def test_without_rustup_the_path_copy_is_still_found(
        self, tmp_path, monkeypatch
    ) -> None:
        """rustc present, rustup absent: the component cannot be added, PATH can."""
        self._sysroot(tmp_path, monkeypatch, profdata=False)

        def no_rustup(cmd, **_kwargs):
            raise FileNotFoundError(2, "No such file or directory", cmd[0])

        monkeypatch.setattr(pgo, "run_cmd", no_rustup)
        monkeypatch.setattr(
            pgo.shutil,
            "which",
            lambda name: "/usr/bin/llvm-profdata" if name == "llvm-profdata" else None,
        )
        assert pgo._ensure_llvm_profdata_available() is True

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
        # Only called once (instrument) -- pipeline aborted
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
        # Workload runs TWICE -- PGO and BOLT each need their own profile data,
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
    optimised binary from a fallback -- only the outcome can.
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


def _run_pgo_bolt_pipeline(
    tmp_path,
    cargo_results,
    *,
    target: str = "x86_64-unknown-linux-gnu",
    bolt_enabled: bool = True,
    bolt_toolchain: bool = True,
    extra_env: dict[str, str] | None = None,
):
    """Run PGO (+BOLT) with a project env naming sccache; return cargo calls.

    ``cargo_results`` is the exit code of each `cargo pgo` call in order.
    """
    bin_dir = tmp_path / "target" / target / "release"
    bin_dir.mkdir(parents=True)
    (bin_dir / "my-bin").touch()
    (bin_dir / "my-bin-bolt-instrumented").touch()
    with (
        patch.object(pgo, "_ensure_cargo_pgo_installed", return_value=True),
        patch.object(pgo, "_ensure_ld_lld_available", return_value=True),
        patch.object(pgo, "_ensure_llvm_profdata_available", return_value=True),
        patch.object(pgo, "_ensure_llvm_bolt_available", return_value=bolt_toolchain),
        patch.object(pgo, "_run_workload", return_value=0),
        patch.object(pgo, "_run_cargo_pgo", side_effect=cargo_results) as cargo,
    ):
        rc = run_pgo_build(
            target=target,
            profile=_make_profile(bolt_enabled=bolt_enabled),
            binary_name="my-bin",
            cwd=tmp_path,
            extra_env=extra_env or {"RUSTC_WRAPPER": "sccache"},
        )
    assert rc == 0
    return [(c.args[0], c.kwargs["extra_env"]) for c in cargo.call_args_list]


def _step(args: list[str]) -> str:
    return " ".join(args[: 2 if args[0] == "bolt" else 1])


class TestProfileUseStepsSkipSccache:
    """Every cargo-pgo step that compiles with a profile runs without sccache (#436)."""

    def test_only_the_instrumented_pgo_build_keeps_the_wrapper(self, tmp_path) -> None:
        calls = _run_pgo_bolt_pipeline(tmp_path, [0, 0, 0, 0])
        wrappers = {_step(args): env.get("RUSTC_WRAPPER") for args, env in calls}
        assert wrappers == {
            "build": "sccache",
            "optimize": "",
            "bolt build": "",
            "bolt optimize": "",
        }

    def test_the_no_split_retry_also_skips_it(self, tmp_path) -> None:
        # The first bolt build fails, so the no-split pass builds again.
        calls = _run_pgo_bolt_pipeline(tmp_path, [0, 0, 1, 0, 0])
        assert [_step(args) for args, _ in calls] == [
            "build",
            "optimize",
            "bolt build",
            "bolt build",
            "bolt optimize",
        ]
        assert [env.get("RUSTC_WRAPPER") for _, env in calls[1:]] == [""] * 4

    def test_the_empty_wrapper_beats_the_runner_env(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("RUSTC_WRAPPER", "sccache")
        with patch.object(pgo.subprocess, "run") as run:
            run.return_value.returncode = 0
            pgo._run_cargo_pgo(
                ["optimize"], cwd=tmp_path, extra_env=pgo._PROFILE_USE_ENV
            )
        assert run.call_args.kwargs["env"]["RUSTC_WRAPPER"] == ""


class TestBoltWithPgoPairing:
    """`bolt build` and `bolt optimize` agree on --with-pgo (#437).

    cargo-pgo applies the BOLT profile to the layout `bolt build` recorded it
    on, so the flag on one without the other mismatches the two.
    """

    @pytest.mark.parametrize(
        "cargo_results", [[0, 0, 0, 0], [0, 0, 1, 0, 0]], ids=["first", "no-split"]
    )
    def test_both_bolt_steps_build_on_the_pgo_layout(
        self, tmp_path, cargo_results
    ) -> None:
        calls = _run_pgo_bolt_pipeline(tmp_path, cargo_results)
        bolt = [args for args, _ in calls if args[0] == "bolt"]
        assert {args[1] for args in bolt} == {"build", "optimize"}
        for args in bolt:
            assert "--with-pgo" in args[: args.index("--")], args


def _profile_settings(env: dict[str, str]) -> dict[str, str]:
    return {
        key: value for key, value in env.items() if key.startswith("CARGO_PROFILE_")
    }


class TestEveryCompileSharesTheProfile:
    """Every compile that shares a PGO profile shares the cargo profile.

    Cargo hashes profile settings, strip included, into `-C metadata` and so
    into every symbol name, and the PGO profile is keyed on those names.
    """

    _PROJECT_ENV = {"RUSTC_WRAPPER": "sccache", "CARGO_PROFILE_RELEASE_LTO": "fat"}

    @pytest.mark.parametrize(
        ("cargo_results", "steps"),
        [
            ([0, 0, 0, 0], ["build", "optimize", "bolt build", "bolt optimize"]),
            (
                [0, 0, 1, 0, 0],
                ["build", "optimize", "bolt build", "bolt build", "bolt optimize"],
            ),
        ],
        ids=["first", "no-split"],
    )
    def test_bolt_pipeline_compiles_with_one_profile(
        self, tmp_path, cargo_results, steps
    ) -> None:
        calls = _run_pgo_bolt_pipeline(
            tmp_path, cargo_results, extra_env=self._PROJECT_ENV
        )
        assert [_step(args) for args, _ in calls] == steps
        settings = [_profile_settings(env) for _, env in calls]
        assert settings == [settings[0]] * len(calls)
        assert settings[0] == {
            "CARGO_PROFILE_RELEASE_LTO": "fat",
            "CARGO_PROFILE_RELEASE_STRIP": "none",
        }

    def test_mold_retry_keeps_the_shared_profile(self, tmp_path) -> None:
        # The aarch64 instrumented build fails once, so it relinks with mold.
        with patch.object(pgo.shutil, "which", side_effect=lambda n: f"/usr/bin/{n}"):
            calls = _run_pgo_bolt_pipeline(
                tmp_path,
                [1, 0, 0, 0, 0],
                target="aarch64-unknown-linux-gnu",
                extra_env=self._PROJECT_ENV,
            )
        assert [_step(args) for args, _ in calls][:2] == ["build", "build"]
        settings = [_profile_settings(env) for _, env in calls]
        assert settings == [settings[0]] * len(calls)
        assert settings[0]["CARGO_PROFILE_RELEASE_STRIP"] == "none"

    @pytest.mark.parametrize(
        ("bolt_enabled", "bolt_toolchain"),
        [(False, True), (True, False)],
        ids=["bolt-off", "no-toolchain"],
    )
    def test_pgo_only_steps_keep_the_project_profile(
        self, tmp_path, bolt_enabled, bolt_toolchain
    ) -> None:
        calls = _run_pgo_bolt_pipeline(
            tmp_path,
            [0, 0],
            bolt_enabled=bolt_enabled,
            bolt_toolchain=bolt_toolchain,
        )
        assert calls == [
            (calls[0][0], {"RUSTC_WRAPPER": "sccache"}),
            (calls[1][0], {"RUSTC_WRAPPER": ""}),
        ]
        assert [_step(args) for args, _ in calls] == ["build", "optimize"]
