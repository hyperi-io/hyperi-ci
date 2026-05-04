# Project:   HyperI CI
# File:      tests/unit/deployment/test_stage.py
# Purpose:   Generate stage handler tests (tier dispatch + drift check)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for `hyperi_ci.deployment.stage.run` and `check_drift`."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from hyperi_ci.deployment.cli import EXIT_NOT_IMPLEMENTED
from hyperi_ci.deployment.stage import (
    EXIT_OK,
    EXIT_PRODUCER_FAILED,
    EXIT_PRODUCER_MISSING,
    check_drift,
    run,
)


def _valid_contract_dict() -> dict:
    return {
        "app_name": "demo",
        "metrics_port": 9090,
        "health": {
            "liveness_path": "/healthz",
            "readiness_path": "/readyz",
            "metrics_path": "/metrics",
        },
        "env_prefix": "DEMO",
        "metric_prefix": "demo",
        "config_mount_path": "/etc/demo/demo.yaml",
    }


def _write_tier3_repo(root: Path) -> Path:
    """Create a Tier 3 layout: ci/deployment-contract.json present."""
    ci = root / "ci"
    ci.mkdir(parents=True, exist_ok=True)
    contract = ci / "deployment-contract.json"
    contract.write_text(json.dumps(_valid_contract_dict()), encoding="utf-8")
    return contract


def _write_tier1_repo(root: Path, *, with_binary: bool = False) -> Path:
    """Create a Tier 1 layout: Cargo.toml with hyperi-rustlib + optional binary."""
    (root / "Cargo.toml").write_text(
        '[package]\nname = "demo-app"\n'
        '[[bin]]\nname = "demo-app"\npath = "src/main.rs"\n'
        '[dependencies]\nhyperi-rustlib = "2.5"\n',
        encoding="utf-8",
    )
    if with_binary:
        target = root / "target" / "release"
        target.mkdir(parents=True)
        binary = target / "demo-app"
        # A minimal POSIX shell script masquerading as the binary —
        # real binaries are ELF, but for invocation testing the only
        # thing that matters is that it's executable and exits 0/!=0
        # under our control.
        binary.write_text(
            '#!/usr/bin/env bash\necho "fake rust binary $*"\nexit 0\n',
            encoding="utf-8",
        )
        os.chmod(binary, binary.stat().st_mode | stat.S_IXUSR)
        return binary
    return root / "target" / "release" / "demo-app"


def _write_tier2_repo(root: Path) -> None:
    """Tier 2 layout: pyproject.toml with hyperi-pylib + scripts."""
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo-app"\n'
        'dependencies = ["hyperi-pylib>=2.24"]\n'
        "[project.scripts]\n"
        'demo-app = "demo_app.main:main"\n',
        encoding="utf-8",
    )


class TestTierNone:
    """Repos without any contract skip silently with success."""

    def test_no_contract_returns_ok(self, tmp_path: Path) -> None:
        rc = run(output_dir=tmp_path / "ci-tmp", project_dir=tmp_path)
        assert rc == EXIT_OK


class TestTier3:
    """Tier 3 (other) routes into the in-process emit_artefacts templater."""

    def test_routes_to_emit_artefacts(self, tmp_path: Path) -> None:
        contract = _write_tier3_repo(tmp_path)
        rc = run(
            output_dir=tmp_path / "ci-tmp",
            project_dir=tmp_path,
            contract_path=contract,
        )
        # Templater itself returns EXIT_NOT_IMPLEMENTED until Phase 2.
        # The generate stage propagates that verbatim.
        assert rc == EXIT_NOT_IMPLEMENTED

    def test_creates_output_dir(self, tmp_path: Path) -> None:
        _write_tier3_repo(tmp_path)
        out = tmp_path / "ci-tmp"
        run(output_dir=out, project_dir=tmp_path)
        assert out.is_dir()


class TestTier1:
    """Tier 1 (rust + rustlib) discovers and invokes the producer binary."""

    def test_no_binary_returns_producer_missing(self, tmp_path: Path) -> None:
        _write_tier1_repo(tmp_path, with_binary=False)
        rc = run(output_dir=tmp_path / "ci-tmp", project_dir=tmp_path)
        assert rc == EXIT_PRODUCER_MISSING

    def test_binary_invoked_when_present(self, tmp_path: Path) -> None:
        _write_tier1_repo(tmp_path, with_binary=True)
        rc = run(output_dir=tmp_path / "ci-tmp", project_dir=tmp_path)
        # Fake binary returns 0 → producer succeeded.
        assert rc == EXIT_OK

    def test_dist_arm64_binary_resolved_on_amd64_host(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Regression: arm64 build leg has dist/<bin>-linux-arm64 only.

        Pre-fix the resolver hardcoded `dist/<bin>-linux-amd64` and the
        arm64 Build job failed with "no Rust binary found" even though
        a perfectly valid arm64 binary sat in dist/. The fix globs
        `dist/<bin>-linux-*` as a fallback when the host-arch-specific
        binary isn't present.
        """
        _write_tier1_repo(tmp_path, with_binary=False)
        dist = tmp_path / "dist"
        dist.mkdir()
        binary = dist / "demo-app-linux-arm64"
        binary.write_text(
            '#!/usr/bin/env bash\necho "fake arm64 binary"\nexit 0\n',
            encoding="utf-8",
        )
        os.chmod(binary, binary.stat().st_mode | stat.S_IXUSR)

        # Force the host-arch detection to claim amd64 — proving the
        # glob fallback is what found the binary, not the host-specific
        # path. (In CI the arm64 runner reports aarch64 and the
        # host-specific path matches directly; this test exercises the
        # cross-arch fallback path.)
        from hyperi_ci.deployment import stage as stage_module

        monkeypatch.setattr(stage_module, "_host_linux_arch", lambda: "amd64")

        rc = run(output_dir=tmp_path / "ci-tmp", project_dir=tmp_path)
        assert rc == EXIT_OK

    def test_dist_host_arch_binary_preferred_over_target_release(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """When both ``dist/<bin>-linux-amd64`` and ``target/release/<bin>``
        exist, the dist/ artefact wins — the Build stage's published
        artefact is the canonical thing the Container stage will use.
        """
        _write_tier1_repo(tmp_path, with_binary=True)  # populates target/release
        dist = tmp_path / "dist"
        dist.mkdir()
        dist_bin = dist / "demo-app-linux-amd64"
        # dist/ binary writes a marker file the test can detect.
        dist_bin.write_text(
            "#!/usr/bin/env bash\n"
            'for i in "$@"; do\n'
            '  if [ "$prev" = "--output-dir" ]; then\n'
            '    mkdir -p "$i"\n'
            '    echo "from-dist" > "$i/source.txt"\n'
            "  fi\n"
            '  prev="$i"\n'
            "done\n",
            encoding="utf-8",
        )
        os.chmod(dist_bin, dist_bin.stat().st_mode | stat.S_IXUSR)

        from hyperi_ci.deployment import stage as stage_module

        monkeypatch.setattr(stage_module, "_host_linux_arch", lambda: "amd64")

        out = tmp_path / "ci-tmp"
        rc = run(output_dir=out, project_dir=tmp_path)
        assert rc == EXIT_OK
        assert (out / "source.txt").read_text().strip() == "from-dist"

    def test_binary_failure_propagated(self, tmp_path: Path) -> None:
        binary_path = _write_tier1_repo(tmp_path, with_binary=True)
        # Rewrite the fake binary to exit non-zero.
        binary_path.write_text(
            "#!/usr/bin/env bash\nexit 7\n",
            encoding="utf-8",
        )
        os.chmod(
            binary_path,
            binary_path.stat().st_mode | stat.S_IXUSR,
        )
        rc = run(output_dir=tmp_path / "ci-tmp", project_dir=tmp_path)
        assert rc == EXIT_PRODUCER_FAILED


class TestTier2:
    """Tier 2 (python + pylib) routes through PATH lookup."""

    def test_entry_point_not_on_path_returns_producer_missing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _write_tier2_repo(tmp_path)
        # Empty PATH so shutil.which can't find demo-app.
        monkeypatch.setenv("PATH", str(tmp_path / "no-such-bin"))
        rc = run(output_dir=tmp_path / "ci-tmp", project_dir=tmp_path)
        assert rc == EXIT_PRODUCER_MISSING

    def test_no_scripts_in_pyproject_returns_producer_missing(
        self, tmp_path: Path
    ) -> None:
        # pyproject.toml without [project.scripts] — no entry point to invoke.
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["hyperi-pylib>=2.24"]\n',
            encoding="utf-8",
        )
        rc = run(output_dir=tmp_path / "ci-tmp", project_dir=tmp_path)
        assert rc == EXIT_PRODUCER_MISSING


class TestDriftCheck:
    """Drift check compares regenerated output against committed ci/."""

    def test_no_committed_dir_is_ok(self, tmp_path: Path) -> None:
        # Empty repo (Tier NONE) → producer emits nothing → no committed
        # ci/ → drift check has nothing to compare. Should pass.
        rc = check_drift(project_dir=tmp_path)
        assert rc == EXIT_OK

    def test_drift_detected_on_modified_files(self, tmp_path: Path) -> None:
        # Set up a Tier 1 repo whose binary writes a known artefact.
        # We simulate "drift" by pre-creating ci/ with content that
        # won't match what the producer writes.
        binary = _write_tier1_repo(tmp_path, with_binary=True)
        # Fake binary writes a Dockerfile to its --output-dir argument.
        binary.write_text(
            "#!/usr/bin/env bash\n"
            'for i in "$@"; do\n'
            '  if [ "$prev" = "--output-dir" ]; then\n'
            '    mkdir -p "$i"\n'
            "    echo 'FROM ubuntu:24.04' > \"$i/Dockerfile\"\n"
            "  fi\n"
            '  prev="$i"\n'
            "done\n"
            "exit 0\n",
            encoding="utf-8",
        )
        os.chmod(binary, binary.stat().st_mode | stat.S_IXUSR)

        # Committed ci/ has a different Dockerfile — drift!
        ci_dir = tmp_path / "ci"
        ci_dir.mkdir()
        (ci_dir / "Dockerfile").write_text("FROM debian:12\n", encoding="utf-8")

        rc = check_drift(project_dir=tmp_path)
        assert rc == EXIT_PRODUCER_FAILED

    def test_no_drift_on_matching_files(self, tmp_path: Path) -> None:
        binary = _write_tier1_repo(tmp_path, with_binary=True)
        binary.write_text(
            "#!/usr/bin/env bash\n"
            'for i in "$@"; do\n'
            '  if [ "$prev" = "--output-dir" ]; then\n'
            '    mkdir -p "$i"\n'
            "    echo 'FROM ubuntu:24.04' > \"$i/Dockerfile\"\n"
            "  fi\n"
            '  prev="$i"\n'
            "done\n"
            "exit 0\n",
            encoding="utf-8",
        )
        os.chmod(binary, binary.stat().st_mode | stat.S_IXUSR)

        ci_dir = tmp_path / "ci"
        ci_dir.mkdir()
        # Committed file matches what the producer writes.
        (ci_dir / "Dockerfile").write_text("FROM ubuntu:24.04\n", encoding="utf-8")

        rc = check_drift(project_dir=tmp_path)
        assert rc == EXIT_OK


class TestDispatchIntegration:
    """The dispatch table exposes ``generate`` as a stage."""

    def test_generate_in_valid_stages(self) -> None:
        from hyperi_ci.dispatch import VALID_STAGES

        assert "generate" in VALID_STAGES

    def test_generate_handler_registered(self) -> None:
        from hyperi_ci.dispatch import _STAGE_HANDLERS

        assert "generate" in _STAGE_HANDLERS
