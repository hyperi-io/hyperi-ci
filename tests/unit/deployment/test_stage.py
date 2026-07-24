# Project:   HyperI CI
# File:      tests/unit/deployment/test_stage.py
# Purpose:   Generate stage handler tests (tier dispatch + drift check)
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for `hyperi_ci.deployment.stage.run` and `check_drift`."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from hyperi_ci.config import CIConfig
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
    """Create a Tier 1 layout: Cargo.toml with scalo + optional binary."""
    (root / "Cargo.toml").write_text(
        '[package]\nname = "demo-app"\n'
        '[[bin]]\nname = "demo-app"\npath = "src/main.rs"\n'
        "[dependencies]\n"
        # The `deployment` feature is what compiles contract emission
        # into generate-artefacts — without it this isn't Tier 1 at all.
        'scalo = { version = "2.5", features = ["cli-service", "deployment"] }\n',
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
    """Tier 2 layout: pyproject.toml with scalo + scripts."""
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo-app"\n'
        'dependencies = ["scalo>=2.28"]\n'
        "[project.scripts]\n"
        'demo-app = "demo_app.main:main"\n',
        encoding="utf-8",
    )


def _write_scalo_library_consumer(root: Path) -> None:
    """The culvert shape from issue #76.

    Uses scalo as a library (logging / config / secrets), declares no
    console script, and builds its container from its own Dockerfile.
    """
    (root / "pyproject.toml").write_text(
        '[project]\nname = "culvert"\ndependencies = ["scalo>=2.28"]\n',
        encoding="utf-8",
    )


def _producer_config(value: str) -> CIConfig:
    """A CIConfig carrying just `deployment.producer`."""
    return CIConfig(_raw={"deployment": {"producer": value}})


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
    """Tier 1 (rust + scalo) discovers and invokes the producer binary."""

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


class TestRustBinaryName:
    """Regression tests for `[[bin]]` selection.

    A project with multiple `[[bin]]` blocks (main app + helper bins
    like a PGO instrumentation driver) needs the package-name binary
    picked, not whatever happens to be declared first. dfe-receiver
    hit this: pgo-driver was declared first and got picked instead
    of dfe-receiver itself, so generate-artefacts ran the PGO driver
    which obviously doesn't have that subcommand.
    """

    def test_picks_package_name_when_matching_bin_present(self, tmp_path: Path) -> None:
        from hyperi_ci.deployment.manifest import rust_binary_name

        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "dfe-receiver"\nversion = "1.0.0"\n'
            '[[bin]]\nname = "pgo-driver"\npath = "src/bin/pgo-driver.rs"\n'
            '[[bin]]\nname = "dfe-receiver"\npath = "src/main.rs"\n',
            encoding="utf-8",
        )
        assert rust_binary_name(tmp_path) == "dfe-receiver"

    def test_picks_package_name_when_no_explicit_bin(self, tmp_path: Path) -> None:
        from hyperi_ci.deployment.manifest import rust_binary_name

        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "myapp"\nversion = "1.0.0"\n',
            encoding="utf-8",
        )
        assert rust_binary_name(tmp_path) == "myapp"

    def test_falls_back_to_first_bin_when_no_package_name(self, tmp_path: Path) -> None:
        from hyperi_ci.deployment.manifest import rust_binary_name

        # No [package] table — last-resort fallback to first [[bin]].
        (tmp_path / "Cargo.toml").write_text(
            '[[bin]]\nname = "first-bin"\npath = "src/a.rs"\n'
            '[[bin]]\nname = "second-bin"\npath = "src/b.rs"\n',
            encoding="utf-8",
        )
        assert rust_binary_name(tmp_path) == "first-bin"

    def test_returns_none_when_no_cargo_toml(self, tmp_path: Path) -> None:
        from hyperi_ci.deployment.manifest import rust_binary_name

        assert rust_binary_name(tmp_path) is None

    def test_workspace_root_resolves_to_first_member_with_binary(
        self, tmp_path: Path
    ) -> None:
        """Workspace-only root (no [package]) recurses into members."""
        from hyperi_ci.deployment.manifest import rust_binary_name

        (tmp_path / "Cargo.toml").write_text(
            '[workspace]\nresolver = "2"\n'
            "members = [\n"
            '    "crates/core",\n'
            '    "crates/io",\n'
            '    "crates/archiver",\n'
            "]\n",
            encoding="utf-8",
        )
        # Two leaf-only crates without [[bin]], one with.
        (tmp_path / "crates" / "core").mkdir(parents=True)
        (tmp_path / "crates" / "core" / "Cargo.toml").write_text(
            '[package]\nname = "core"\nversion = "0.1.0"\n[lib]\n',
            encoding="utf-8",
        )
        (tmp_path / "crates" / "io").mkdir(parents=True)
        (tmp_path / "crates" / "io" / "Cargo.toml").write_text(
            '[package]\nname = "io"\nversion = "0.1.0"\n[lib]\n',
            encoding="utf-8",
        )
        (tmp_path / "crates" / "archiver").mkdir(parents=True)
        (tmp_path / "crates" / "archiver" / "Cargo.toml").write_text(
            '[package]\nname = "demo-archiver"\nversion = "0.1.0"\n'
            '[[bin]]\nname = "demo-archiver"\npath = "src/main.rs"\n',
            encoding="utf-8",
        )
        # Rename project_dir so the leaf-prefer rule kicks in.
        ws = tmp_path.rename(tmp_path.parent / "demo-archiver")
        try:
            assert rust_binary_name(ws) == "demo-archiver"
        finally:
            ws.rename(tmp_path)

    def test_workspace_returns_none_when_no_member_has_binary(
        self, tmp_path: Path
    ) -> None:
        from hyperi_ci.deployment.manifest import rust_binary_name

        (tmp_path / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["crates/lib-only"]\n',
            encoding="utf-8",
        )
        (tmp_path / "crates" / "lib-only").mkdir(parents=True)
        (tmp_path / "crates" / "lib-only" / "Cargo.toml").write_text(
            "[lib]\n",  # no [package], no [[bin]]
            encoding="utf-8",
        )
        assert rust_binary_name(tmp_path) is None


class TestTier2:
    """Tier 2 (python + scalo) invokes the entry point via uv run.

    The producer entry point lives in the project's uv venv, not on
    PATH, so the stage prefers ``uv run`` and only falls back to a bare
    PATH lookup when uv is absent.
    """

    def test_uv_run_invoked_when_uv_present(self, tmp_path: Path, monkeypatch) -> None:
        _write_tier2_repo(tmp_path)
        # Fake `uv` on PATH that succeeds for any args — isolates the test
        # from a real uv/venv while exercising the uv-run producer path.
        bindir = tmp_path / "fakebin"
        bindir.mkdir()
        uv = bindir / "uv"
        uv.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        os.chmod(uv, uv.stat().st_mode | stat.S_IXUSR)
        # Prepend so the fake uv wins shutil.which while bash stays resolvable.
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        rc = run(output_dir=tmp_path / "ci-tmp", project_dir=tmp_path)
        # Fake uv exits 0 → producer succeeded.
        assert rc == EXIT_OK

    def test_entry_point_unresolvable_returns_producer_missing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _write_tier2_repo(tmp_path)
        # Empty PATH: neither uv nor the demo-app script is resolvable.
        monkeypatch.setenv("PATH", str(tmp_path / "no-such-bin"))
        rc = run(output_dir=tmp_path / "ci-tmp", project_dir=tmp_path)
        assert rc == EXIT_PRODUCER_MISSING

    def test_no_scripts_in_pyproject_skips(self, tmp_path: Path) -> None:
        # Issue #76: a scalo LIBRARY consumer (no [project.scripts]) is
        # not a producer. It used to reach _run_tier2 and exit 7, which
        # failed the Build job of every scalo-using container that
        # wasn't a ServiceApp.
        _write_scalo_library_consumer(tmp_path)
        out = tmp_path / "ci-tmp"
        rc = run(output_dir=out, project_dir=tmp_path, config=_producer_config("auto"))
        assert rc == EXIT_OK
        # Nothing generated, so nothing for the container stage to pick
        # up from ci-tmp/ — the skip must not leave a half-built dir.
        assert not out.exists()

    def test_no_scripts_with_producer_forced_returns_producer_missing(
        self, tmp_path: Path
    ) -> None:
        # `deployment.producer: true` is the operator saying "yes it is
        # a producer" — then a missing entry point IS an error worth
        # failing on, rather than a silent skip.
        _write_scalo_library_consumer(tmp_path)
        rc = run(
            output_dir=tmp_path / "ci-tmp",
            project_dir=tmp_path,
            config=_producer_config("true"),
        )
        assert rc == EXIT_PRODUCER_MISSING


class TestProducerGate:
    """`deployment.producer` overrides tier auto-detection (issue #76)."""

    def test_false_skips_a_real_producer(self, tmp_path: Path) -> None:
        # A repo that WOULD dispatch (Tier 3 contract committed) skips
        # when the operator opts out — this is the escape hatch for a
        # library consumer that ships its deployment artefacts by hand.
        _write_tier3_repo(tmp_path)
        rc = run(
            output_dir=tmp_path / "ci-tmp",
            project_dir=tmp_path,
            config=_producer_config("false"),
        )
        assert rc == EXIT_OK

    def test_false_skips_tier2(self, tmp_path: Path) -> None:
        _write_tier2_repo(tmp_path)
        rc = run(
            output_dir=tmp_path / "ci-tmp",
            project_dir=tmp_path,
            config=_producer_config("false"),
        )
        assert rc == EXIT_OK

    def test_true_without_any_marker_fails_loudly(self, tmp_path: Path) -> None:
        # Forcing a producer on a repo with no scalo dep and no contract
        # is a config error. Silently doing nothing would leave the
        # operator waiting for artefacts that never arrive.
        rc = run(
            output_dir=tmp_path / "ci-tmp",
            project_dir=tmp_path,
            config=_producer_config("true"),
        )
        assert rc == EXIT_PRODUCER_MISSING

    def test_true_forces_tier1_without_a_binary_target(self, tmp_path: Path) -> None:
        # Library-shaped Cargo.toml (no [[bin]], no src/main.rs) that
        # the operator declares IS a producer. Detection is overridden,
        # so we get as far as looking for the built binary.
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "demo-app"\n[dependencies]\nscalo = "2.9"\n',
            encoding="utf-8",
        )
        rc = run(
            output_dir=tmp_path / "ci-tmp",
            project_dir=tmp_path,
            config=_producer_config("true"),
        )
        assert rc == EXIT_PRODUCER_MISSING

    def test_unknown_value_falls_back_to_auto(self, tmp_path: Path) -> None:
        contract = _write_tier3_repo(tmp_path)
        rc = run(
            output_dir=tmp_path / "ci-tmp",
            project_dir=tmp_path,
            contract_path=contract,
            config=_producer_config("sometimes"),
        )
        # 'auto' behaviour → Tier 3 dispatch → templater's not-implemented.
        assert rc == EXIT_NOT_IMPLEMENTED


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

    def test_non_producer_with_committed_ci_is_not_drift(self, tmp_path: Path) -> None:
        # A repo that generates nothing but keeps a hand-maintained ci/
        # must not read as drift. Without the gate, the empty regen dir
        # diffs against a populated ci/ and every file looks deleted.
        _write_scalo_library_consumer(tmp_path)
        ci_dir = tmp_path / "ci"
        ci_dir.mkdir()
        (ci_dir / "Dockerfile").write_text("FROM debian:12\n", encoding="utf-8")

        rc = check_drift(project_dir=tmp_path, config=_producer_config("auto"))
        assert rc == EXIT_OK

    def test_producer_false_skips_the_drift_check(self, tmp_path: Path) -> None:
        # Opting out of generation opts out of policing what it would
        # have generated.
        _write_tier2_repo(tmp_path)
        ci_dir = tmp_path / "ci"
        ci_dir.mkdir()
        (ci_dir / "Dockerfile").write_text("FROM debian:12\n", encoding="utf-8")

        rc = check_drift(project_dir=tmp_path, config=_producer_config("false"))
        assert rc == EXIT_OK


class TestDispatchIntegration:
    """The dispatch table exposes ``generate`` as a stage."""

    def test_generate_in_valid_stages(self) -> None:
        from hyperi_ci.dispatch import VALID_STAGES

        assert "generate" in VALID_STAGES

    def test_generate_handler_registered(self) -> None:
        from hyperi_ci.dispatch import _STAGE_HANDLERS

        assert "generate" in _STAGE_HANDLERS

    def test_config_reaches_the_producer_gate(self, monkeypatch) -> None:
        # The gate is only useful if dispatch actually passes the config
        # down — it used to `del language` and call generate_run() with
        # no arguments, which is what made `deployment.producer` inert.
        from hyperi_ci.dispatch import stage_generate

        seen: dict[str, object] = {}

        def fake_run(**kwargs: object) -> int:
            seen.update(kwargs)
            return 0

        monkeypatch.setattr("hyperi_ci.deployment.stage.run", fake_run)
        config = _producer_config("false")
        assert stage_generate("python", config) == 0
        assert seen["config"] is config

    def test_project_dir_reaches_the_generate_stage(self, tmp_path: Path) -> None:
        # `hyperi-ci run generate -C <dir>` resolved the root for
        # language detection but not for tier detection, so the stage
        # read the CURRENT repo's manifests and dispatched its producer
        # against a different project.
        from hyperi_ci.dispatch import run_stage

        _write_scalo_library_consumer(tmp_path)
        assert run_stage("generate", project_dir=tmp_path) == EXIT_OK
