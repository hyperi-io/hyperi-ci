# Project:   HyperI CI
# File:      tests/unit/test_rust_build.py
# Purpose:   Unit tests for Rust workspace feature detection and the Tier 2 summary
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.languages.rust import build
from hyperi_ci.languages.rust.optimize import (
    OptimizationOutcome,
    OptimizationProfile,
    cargo_feature_args,
    tier2_shortfall,
    validate_profile,
)
from hyperi_ci.languages.rust.pgo import BOLT_NOTE_SECTION


def _metadata_runner(manifest_paths: list[Path], returncode: int = 0):
    """Stand in for `cargo metadata --no-deps` over the given member manifests."""
    payload = json.dumps(
        {"packages": [{"manifest_path": str(p), "targets": []} for p in manifest_paths]}
    )

    def fake_run(cmd, *_args, **_kwargs):
        return subprocess.CompletedProcess(cmd, returncode, stdout=payload, stderr="")

    return fake_run


class TestDetectCargoFeatures:
    """Feature detection unions the root manifest with every workspace member."""

    def test_virtual_workspace_member_feature_is_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["crates/archiver"]\nresolver = "2"\n'
        )
        member = tmp_path / "crates" / "archiver"
        member.mkdir(parents=True)
        member_manifest = member / "Cargo.toml"
        member_manifest.write_text(
            '[package]\nname = "archiver"\n\n'
            '[features]\njemalloc = ["dep:tikv-jemallocator"]\n'
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            build.subprocess, "run", _metadata_runner([member_manifest])
        )

        assert build._detect_cargo_features() == {"jemalloc"}

    def test_plain_package_root_still_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "Cargo.toml"
        root.write_text(
            '[package]\nname = "app"\n\n[features]\njemalloc = []\nmimalloc = []\n'
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(build.subprocess, "run", _metadata_runner([root]))

        assert build._detect_cargo_features() == {"jemalloc", "mimalloc"}

    def test_metadata_failure_falls_back_to_root_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "app"\n\n[features]\njemalloc = []\n'
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(build.subprocess, "run", _metadata_runner([], returncode=1))

        assert build._detect_cargo_features() == {"jemalloc"}


class TestWorkspaceFeaturesReachTheCargoLine:
    """A member's allocator feature has to survive as far as the cargo line.

    Detection reads the members, validation decides whether the allocator
    survives, and `cargo_feature_args()` renders it. dfe-archiver shipped
    without jemalloc because the first step stopped at the root manifest.
    """

    @staticmethod
    def _virtual_workspace(tmp_path: Path) -> Path:
        (tmp_path / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["crates/archiver"]\nresolver = "2"\n'
        )
        member = tmp_path / "crates" / "archiver"
        member.mkdir(parents=True)
        member_manifest = member / "Cargo.toml"
        member_manifest.write_text(
            '[package]\nname = "archiver"\n\n'
            '[features]\njemalloc = ["dep:tikv-jemallocator"]\n'
        )
        return member_manifest

    def test_member_declared_allocator_is_rendered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        member_manifest = self._virtual_workspace(tmp_path)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            build.subprocess, "run", _metadata_runner([member_manifest])
        )

        profile = validate_profile(
            OptimizationProfile(channel="release", allocator="jemalloc"),
            cargo_features=build._detect_cargo_features(),
            target="x86_64-unknown-linux-gnu",
        )

        assert profile.allocator == "jemalloc"
        assert cargo_feature_args(profile, "db-clickhouse") == [
            "--features",
            "db-clickhouse,jemalloc",
        ]

    def test_root_only_detection_drops_the_allocator(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The dfe-archiver symptom: no member read, so the cargo line loses it."""
        self._virtual_workspace(tmp_path)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(build.subprocess, "run", _metadata_runner([]))

        profile = validate_profile(
            OptimizationProfile(channel="release", allocator="jemalloc"),
            cargo_features=build._detect_cargo_features(),
            target="x86_64-unknown-linux-gnu",
        )

        assert profile.allocator == "system"
        assert cargo_feature_args(profile, "db-clickhouse") == [
            "--features",
            "db-clickhouse",
        ]


class TestTier2Summary:
    """A Tier 2 build reports, once per target, what actually ran."""

    _TARGETS = "x86_64-unknown-linux-gnu,aarch64-unknown-linux-gnu"

    @staticmethod
    def _config(optimize: dict) -> CIConfig:
        return CIConfig(
            language="rust", _raw={"build": {"rust": {"optimize": optimize}}}
        )

    @staticmethod
    def _patch_build(
        monkeypatch: pytest.MonkeyPatch, on_build
    ) -> list[OptimizationOutcome]:
        """Neutralise everything in run() that shells out; collect the summaries."""
        logged: list[OptimizationOutcome] = []
        monkeypatch.setattr(build.shutil, "which", lambda _cmd: "/usr/bin/cargo")
        monkeypatch.setattr(build, "is_macos", lambda: False)
        monkeypatch.setattr(
            build, "_get_native_target", lambda: "x86_64-unknown-linux-gnu"
        )
        monkeypatch.setattr(build, "_detect_binary_names", lambda: ["app"])
        monkeypatch.setattr(build, "_detect_cargo_features", lambda: {"jemalloc"})
        monkeypatch.setattr(build, "_resolve_build_channel", lambda _cfg: "release")
        monkeypatch.setattr(build, "_detect_version", lambda: "v1.0.0")
        monkeypatch.setattr(build, "_package_binaries", lambda *_args, **_kwargs: 0)
        # Packaging is stubbed, so there is no packaged file to inspect.
        monkeypatch.setattr(build, "_verify_bolt_shipped", lambda *_args: 0)
        monkeypatch.setattr(build, "_build_for_target", on_build)
        monkeypatch.setattr(build, "log_outcome", logged.append)
        return logged

    def test_arm64_bolt_skip_is_reported_per_arch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def on_build(
            target,
            _features,
            _all,
            _extra=None,
            profile=None,
            *,
            outcome: OptimizationOutcome,
        ):
            outcome.pgo_applied = True
            outcome.bolt_applied = target.startswith("x86_64")
            return 0

        logged = self._patch_build(monkeypatch, on_build)
        config = self._config(
            {
                "pgo": {"enabled": True, "workload_cmd": "bash scripts/w.sh"},
                "bolt": {"enabled": True},
                "strict": False,
            }
        )

        rc = build.run(config, {"RUST_BUILD_TARGETS": self._TARGETS})

        assert rc == 0
        assert [o.describe() for o in logged] == [
            "optimised: pgo=yes bolt=yes allocator=jemalloc",
            "optimised: pgo=yes bolt=no allocator=jemalloc",
        ]

    def test_undeclared_allocator_shows_in_the_summary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def on_build(
            _target,
            _features,
            _all,
            _extra=None,
            profile=None,
            *,
            outcome: OptimizationOutcome,
        ):
            outcome.pgo_applied = True
            outcome.bolt_applied = True
            return 0

        logged = self._patch_build(monkeypatch, on_build)
        monkeypatch.setattr(build, "_detect_cargo_features", set)
        config = self._config(
            {
                "pgo": {"enabled": True, "workload_cmd": "bash scripts/w.sh"},
                "bolt": {"enabled": True},
            }
        )

        rc = build.run(config, {"RUST_BUILD_TARGETS": "x86_64-unknown-linux-gnu"})

        assert rc == 0
        assert [o.describe() for o in logged] == [
            "optimised: pgo=yes bolt=yes allocator=system"
        ]

    def test_tier_1_build_emits_no_summary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def on_build(*_args, **_kwargs):
            return 0

        logged = self._patch_build(monkeypatch, on_build)

        rc = build.run(self._config({}), {"RUST_BUILD_TARGETS": self._TARGETS})

        assert rc == 0
        assert logged == []

    def test_a_bolt_target_whose_packaged_file_is_pgo_only_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, make_elf
    ) -> None:
        # The log said BOLT; the file that ships says otherwise. Fail the build.
        def on_build(*_args, outcome: OptimizationOutcome, **_kwargs):
            outcome.pgo_applied = True
            outcome.bolt_applied = True
            return 0

        def package(*_args, **_kwargs):
            (tmp_path / "dist").mkdir()
            make_elf(tmp_path / "dist" / "app-linux-amd64", [".text"])
            return 0

        real_verify = build._verify_bolt_shipped
        self._patch_build(monkeypatch, on_build)
        monkeypatch.setattr(build, "_package_binaries", package)
        monkeypatch.setattr(build, "_verify_bolt_shipped", real_verify)
        monkeypatch.chdir(tmp_path)
        config = self._config(
            {
                "pgo": {"enabled": True, "workload_cmd": "bash scripts/w.sh"},
                "bolt": {"enabled": True},
            }
        )

        rc = build.run(config, {"RUST_BUILD_TARGETS": "x86_64-unknown-linux-gnu"})

        assert rc == 1


class TestTier2SkipFailsARelease:
    """issue #133: a release that asked for Tier 2 does not ship without it."""

    def test_a_bolt_skip_fails_the_release_and_names_the_stage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def on_build(target, *_args, outcome: OptimizationOutcome, **_kwargs):
            outcome.pgo_applied = True
            outcome.bolt_applied = target.startswith("x86_64")
            return 0

        errors: list[str] = []
        TestTier2Summary._patch_build(monkeypatch, on_build)
        monkeypatch.setattr(build, "error", errors.append)
        config = TestTier2Summary._config(
            {
                "pgo": {"enabled": True, "workload_cmd": "bash scripts/w.sh"},
                "bolt": {"enabled": True},
            }
        )

        rc = build.run(config, {"RUST_BUILD_TARGETS": TestTier2Summary._TARGETS})

        assert rc == 1
        assert any("aarch64" in e and "BOLT" in e for e in errors), errors

    def test_shortfall_names_each_missing_stage(self) -> None:
        profile = OptimizationProfile(
            channel="release", pgo_enabled=True, bolt_enabled=True
        )
        assert tier2_shortfall(profile, OptimizationOutcome()) == ["PGO", "BOLT"]
        done = OptimizationOutcome(pgo_applied=True, bolt_applied=True)
        assert tier2_shortfall(profile, done) == []

    def test_a_stage_never_asked_for_is_not_a_shortfall(self) -> None:
        profile = OptimizationProfile(channel="release", pgo_enabled=True)
        assert tier2_shortfall(profile, OptimizationOutcome(pgo_applied=True)) == []


class TestOptimizeTierOnAValidateRun:
    """issue #257: a dispatch builds the release tier without publishing."""

    _TIER2 = {
        "pgo": {"enabled": True, "workload_cmd": "bash scripts/w.sh"},
        "bolt": {"enabled": True},
    }

    @staticmethod
    def _validate_run(monkeypatch: pytest.MonkeyPatch, tier: str | None) -> None:
        """Env of a run that publishes nothing: no channel, no tag, no skip."""
        for name in (
            "HYPERCI_CHANNEL",
            "GITHUB_REF_TYPE",
            "RUST_VERSION",
            "CI_COMMIT_TAG",
            "HYPERCI_SKIP_OPTIMIZE",
        ):
            monkeypatch.delenv(name, raising=False)
        if tier is None:
            monkeypatch.delenv("HYPERCI_OPTIMIZE_TIER", raising=False)
        else:
            monkeypatch.setenv("HYPERCI_OPTIMIZE_TIER", tier)

    @staticmethod
    def _on_build(*_args, outcome: OptimizationOutcome, **_kwargs) -> int:
        outcome.pgo_applied = True
        outcome.bolt_applied = True
        return 0

    def _run(self, monkeypatch: pytest.MonkeyPatch) -> tuple[int, list]:
        real_channel = build._resolve_build_channel
        logged = TestTier2Summary._patch_build(monkeypatch, self._on_build)
        monkeypatch.setattr(build, "_resolve_build_channel", real_channel)
        config = TestTier2Summary._config(self._TIER2)
        rc = build.run(config, {"RUST_BUILD_TARGETS": "x86_64-unknown-linux-gnu"})
        return rc, logged

    def test_the_release_tier_runs_pgo_and_bolt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._validate_run(monkeypatch, "release")
        rc, logged = self._run(monkeypatch)
        assert rc == 0
        assert [o.describe() for o in logged] == [
            "optimised: pgo=yes bolt=yes allocator=jemalloc"
        ]

    def test_without_the_ask_a_validate_run_stays_tier_1(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._validate_run(monkeypatch, None)
        rc, logged = self._run(monkeypatch)
        assert rc == 0
        assert logged == []

    def test_an_unknown_tier_fails_the_build(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A typo must not build Tier 1 quietly, and the dispatch-supplied value
        # reaches a workflow command escaped.
        errors: list[str] = []
        self._validate_run(monkeypatch, "relase%25\n::warning::planted")
        monkeypatch.setattr(build, "error", errors.append)
        monkeypatch.setattr(build, "is_ci", lambda: True)
        rc, _logged = self._run(monkeypatch)
        assert rc == 1
        assert errors and "optimize-tier" in errors[0]
        out = capsys.readouterr().out
        assert "::error title=hyperi-ci optimize-tier refused::" in out
        assert "\n::warning::planted" not in out
        assert "relase%2525" in out


class TestNativeTargetFollowsTheMachine:
    """An arm64 Linux runner must not read its own target as a cross build.

    `_get_native_target` decides `cross` in `_build_for_target`, which skips
    PGO, which the Tier 2 strict check then refuses to ship.
    """

    @pytest.mark.parametrize(
        ("platform_name", "machine", "expected"),
        [
            ("linux", "x86_64", "x86_64-unknown-linux-gnu"),
            ("linux", "aarch64", "aarch64-unknown-linux-gnu"),
            ("linux", "arm64", "aarch64-unknown-linux-gnu"),
            ("darwin", "arm64", "aarch64-apple-darwin"),
            ("darwin", "x86_64", "x86_64-apple-darwin"),
        ],
    )
    def test_the_triple_matches_the_host(
        self,
        monkeypatch: pytest.MonkeyPatch,
        platform_name: str,
        machine: str,
        expected: str,
    ) -> None:
        import platform as platform_module

        monkeypatch.setattr(build.sys, "platform", platform_name)
        monkeypatch.setattr(platform_module, "machine", lambda: machine)
        assert build._get_native_target() == expected

    def test_an_arm64_host_builds_its_own_target_natively(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The regression: aarch64 on aarch64 read as cross and lost PGO."""
        import platform as platform_module

        monkeypatch.setattr(build.sys, "platform", "linux")
        monkeypatch.setattr(platform_module, "machine", lambda: "aarch64")
        assert build._get_native_target() == "aarch64-unknown-linux-gnu"


class TestPgoAndCrossCompilation:
    """The PGO path returns before the cross setup, so the two cannot combine."""

    _NATIVE = "x86_64-unknown-linux-gnu"
    _FOREIGN = "aarch64-unknown-linux-gnu"

    @staticmethod
    def _patch(monkeypatch: pytest.MonkeyPatch) -> list[str]:
        warnings: list[str] = []
        monkeypatch.setattr(build, "_ensure_target_installed", lambda _t: True)
        monkeypatch.setattr(
            build, "_get_native_target", lambda: TestPgoAndCrossCompilation._NATIVE
        )
        monkeypatch.setattr(build, "is_linux", lambda: True)
        monkeypatch.setattr(build, "_detect_binary_names", lambda: ["app"])
        monkeypatch.setattr(build, "_ensure_cross_toolchain", lambda _t: None)
        monkeypatch.setattr(build, "_setup_cross_sysroot", lambda _a, _t: None)
        monkeypatch.setattr(build, "_clean_stale_sys_crates", lambda _t: None)
        monkeypatch.setattr(build, "_cross_env", lambda _t, sysroot=None: {})
        monkeypatch.setattr(build, "warn", warnings.append)
        monkeypatch.setattr(
            build.subprocess,
            "run",
            lambda *_a, **_kw: subprocess.CompletedProcess([], 0),
        )
        return warnings

    def test_a_cross_target_builds_plain_and_says_so(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        warnings = self._patch(monkeypatch)
        called: list[str] = []
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.pgo.run_pgo_build",
            lambda **kwargs: called.append(kwargs["target"]) or 0,
        )

        rc = build._build_for_target(
            self._FOREIGN,
            "",
            False,
            profile=OptimizationProfile(channel="release", pgo_enabled=True),
        )

        assert rc == 0
        assert called == []
        assert any("not wired for a cross build" in w for w in warnings), warnings

    def test_a_native_target_still_takes_the_pgo_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch(monkeypatch)
        called: list[str] = []
        monkeypatch.setattr(
            "hyperi_ci.languages.rust.pgo.run_pgo_build",
            lambda **kwargs: called.append(kwargs["target"]) or 0,
        )

        rc = build._build_for_target(
            self._NATIVE,
            "",
            False,
            profile=OptimizationProfile(channel="release", pgo_enabled=True),
        )

        assert rc == 0
        assert called == [self._NATIVE]


class TestVerifyBoltShipped:
    """issue #136: the packaged file, not the log, proves BOLT shipped."""

    TARGET = "x86_64-unknown-linux-gnu"

    def test_passes_when_the_packaged_file_carries_the_note(
        self, tmp_path: Path, make_elf
    ) -> None:
        make_elf(tmp_path / "app-linux-amd64", [".text", BOLT_NOTE_SECTION])
        assert build._verify_bolt_shipped([self.TARGET], "app", tmp_path) == 0

    def test_fails_when_the_packaged_file_is_pgo_only(
        self, tmp_path: Path, make_elf
    ) -> None:
        make_elf(tmp_path / "app-linux-amd64", [".text"])
        assert build._verify_bolt_shipped([self.TARGET], "app", tmp_path) == 1

    def test_fails_when_the_packaged_file_is_missing(self, tmp_path: Path) -> None:
        assert build._verify_bolt_shipped([self.TARGET], "app", tmp_path) == 1

    def test_no_bolt_targets_checks_nothing(self, tmp_path: Path) -> None:
        assert build._verify_bolt_shipped([], "app", tmp_path) == 0
