# Project:   HyperI CI
# File:      tests/unit/test_rust_build.py
# Purpose:   Unit tests for Rust workspace feature detection and the Tier 2 summary
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
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
