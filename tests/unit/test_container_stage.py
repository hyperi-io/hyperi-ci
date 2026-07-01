# Project:   HyperI CI
# File:      tests/unit/test_container_stage.py
# Purpose:   Tests for container stage gate, mode resolution, validate vs push
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.container import stage as stage_module
from hyperi_ci.container.detect import Decision
from hyperi_ci.container.stage import (
    _normalise_enabled,
    _read_version,
    _resolve_mode,
    run,
    should_build_container,
)


class TestShouldBuildContainer:
    """Resolve-before-buildx gate (issue #33).

    The container job must decide app-vs-library BEFORE booting Docker
    Buildx, so a library (scalo, a crate — no GHCR deployment) never
    pulls buildkit from Docker Hub and never logs in to GHCR. This gate
    is the filesystem decision, isolated from `detect`'s heuristics
    (covered in test_container_detect.py) by mocking `detect`.
    """

    def _cfg(self, enabled: object) -> CIConfig:
        return CIConfig(
            language="rust",
            _raw={"publish": {"container": {"enabled": enabled}}},
        )

    def test_library_does_not_build(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            stage_module,
            "detect",
            lambda **_: Decision(build=False, reason="rust project is library-only"),
        )
        build, reason = should_build_container(self._cfg("auto"), language="rust")
        assert build is False
        assert "library" in reason

    def test_app_with_signal_builds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            stage_module,
            "detect",
            lambda **_: Decision(build=True, reason="Dockerfile found", mode="custom"),
        )
        build, _ = should_build_container(self._cfg("auto"), language="rust")
        assert build is True

    def test_enabled_false_never_builds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # enabled:false short-circuits — detect must not even be consulted.
        def _boom(**_):  # pragma: no cover - must not be called
            raise AssertionError("detect() should not run when enabled:false")

        monkeypatch.setattr(stage_module, "detect", _boom)
        build, _ = should_build_container(self._cfg("false"), language="rust")
        assert build is False

    def test_enabled_true_forces_build(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # enabled:true means the container is required — gate opens even
        # if detect found no signal (the build step then errors loudly).
        monkeypatch.setattr(
            stage_module,
            "detect",
            lambda **_: Decision(build=False, reason="no signal"),
        )
        build, _ = should_build_container(self._cfg("true"), language="rust")
        assert build is True


class TestResolveOnlyEnv:
    """`HYPERCI_CONTAINER_RESOLVE_ONLY` makes `run` emit the decision to
    GITHUB_OUTPUT and return 0 WITHOUT any Docker work — the workflow
    gates buildx/login/build on it (issue #33)."""

    def test_library_writes_build_false_and_skips_build(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        out = tmp_path / "gh_output"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out))
        monkeypatch.setenv("HYPERCI_CONTAINER_RESOLVE_ONLY", "1")
        monkeypatch.setattr(
            stage_module,
            "detect",
            lambda **_: Decision(build=False, reason="rust project is library-only"),
        )

        def _no_build(**_):  # pragma: no cover - must not run
            raise AssertionError("resolve-only must not build")

        monkeypatch.setattr(stage_module, "_build_custom", _no_build)
        rc = run(CIConfig(language="rust", _raw={}), language="rust")
        assert rc == 0
        assert "build=false" in out.read_text()

    def test_app_writes_build_true_and_skips_build(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        out = tmp_path / "gh_output"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out))
        monkeypatch.setenv("HYPERCI_CONTAINER_RESOLVE_ONLY", "1")
        monkeypatch.setattr(
            stage_module,
            "detect",
            lambda **_: Decision(build=True, reason="Dockerfile found", mode="custom"),
        )

        def _no_build(**_):  # pragma: no cover - must not run
            raise AssertionError("resolve-only must not build")

        monkeypatch.setattr(stage_module, "_build_custom", _no_build)
        rc = run(CIConfig(language="rust", _raw={}), language="rust")
        assert rc == 0
        assert "build=true" in out.read_text()


# --- _read_version (issue #27) -------------------------------------------


class TestReadVersion:
    """Container version derivation precedence.

    Regression for issue #27: the Container job tagged a published image
    with a stale version (a committed ``VERSION`` file left by an aborted
    ``--bump-patch``) while every other job used the Plan job's predicted
    ``next-version``. The fix threads that prediction in via
    ``HYPERCI_VERSION`` and has the stage prefer it.
    """

    def test_prefers_hyperci_version_over_stale_version_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "VERSION").write_text("1.2.4\n")  # stale committed value
        monkeypatch.setenv("HYPERCI_VERSION", "1.3.0")
        assert _read_version() == "1.3.0"

    def test_strips_leading_v_from_hyperci_version(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HYPERCI_VERSION", "v1.3.0")
        assert _read_version() == "1.3.0"

    def test_blank_hyperci_version_falls_back_to_version_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HYPERCI_VERSION", "")  # dispatch path may be empty
        (tmp_path / "VERSION").write_text("3.3.3\n")
        assert _read_version() == "3.3.3"

    def test_version_file_used_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("HYPERCI_VERSION", raising=False)
        (tmp_path / "VERSION").write_text("2.0.0\n")
        assert _read_version() == "2.0.0"

    def test_ref_name_fallback_when_no_env_no_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("HYPERCI_VERSION", raising=False)
        monkeypatch.setenv("GITHUB_REF_NAME", "v9.9.9")
        assert _read_version() == "9.9.9"


# --- _normalise_enabled --------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (True, "true"),
        (False, "false"),
        ("true", "true"),
        ("True", "true"),
        ("FALSE", "false"),
        ("auto", "auto"),
        ("Auto", "auto"),
        (None, "auto"),
        ("garbage", "auto"),
    ],
)
def test_normalise_enabled(raw: object, expected: str) -> None:
    assert _normalise_enabled(raw) == expected


# --- _resolve_mode -------------------------------------------------------


def _decision(mode: str = "") -> Decision:
    return Decision(build=True, reason="test", mode=mode)


def test_resolve_mode_explicit_overrides_decision() -> None:
    container_cfg = {"mode": "custom"}
    assert (
        _resolve_mode(
            language="rust", decision=_decision("contract"), container_cfg=container_cfg
        )
        == "custom"
    )


def test_resolve_mode_uses_decision_when_no_explicit() -> None:
    assert (
        _resolve_mode(language="rust", decision=_decision("contract"), container_cfg={})
        == "contract"
    )


def test_resolve_mode_falls_back_to_language_default() -> None:
    assert (
        _resolve_mode(language="rust", decision=_decision(), container_cfg={})
        == "contract"
    )
    assert (
        _resolve_mode(language="python", decision=_decision(), container_cfg={})
        == "template"
    )
    assert (
        _resolve_mode(language="golang", decision=_decision(), container_cfg={})
        == "custom"
    )


def test_resolve_mode_explicit_empty_string_treated_as_unset() -> None:
    container_cfg = {"mode": ""}
    assert (
        _resolve_mode(
            language="python", decision=_decision(), container_cfg=container_cfg
        )
        == "template"
    )


# --- run() top-level gate ------------------------------------------------


def _ci_config(**overrides) -> CIConfig:
    cfg = CIConfig()
    raw = {
        "publish": {
            "container": overrides.pop("container", {}),
            "target": overrides.pop("target", "oss"),
            "channel": overrides.pop("channel", "release"),
        },
    }
    raw.update(overrides)
    cfg._raw = raw
    return cfg


def test_run_skips_when_enabled_false(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = _ci_config(container={"enabled": False})
    assert run(cfg, language="rust") == 0


def test_run_skips_when_auto_and_no_signal(tmp_path: Path, monkeypatch) -> None:
    """Library project with no Dockerfile and no contract source → auto-skip."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "mylib"\nversion = "0.1.0"\n[lib]\n'
    )
    cfg = _ci_config(container={"enabled": "auto"})
    assert run(cfg, language="rust") == 0


def test_run_fails_when_strict_true_and_no_signal(tmp_path: Path, monkeypatch) -> None:
    """enabled: true is strict — fail loudly when nothing detected."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "mylib"\nversion = "0.1.0"\n[lib]\n'
    )
    cfg = _ci_config(container={"enabled": True})
    assert run(cfg, language="rust") == 1


def test_run_custom_mode_invokes_build_with_resolved_tags(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "myapp"\nversion = "0.1.0"\n'
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.rs").write_text("fn main() {}\n")
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    (tmp_path / "VERSION").write_text("1.2.3\n")

    monkeypatch.setenv("GITHUB_SHA", "abc12345abc12345abc")
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.delenv("GITHUB_REF", raising=False)
    # New (post version-first refactor): publish mode is opt-in via
    # HYPERCI_PUBLISH_MODE. The workflow's container job sets this from
    # setup.outputs.will-publish; tests must set it explicitly.
    monkeypatch.setenv("HYPERCI_PUBLISH_MODE", "true")

    cfg = _ci_config(container={"enabled": "auto"}, target="oss")

    fake_build = MagicMock(return_value=0)
    monkeypatch.setattr(stage_module, "build_and_push", fake_build)

    assert run(cfg, language="rust") == 0
    fake_build.assert_called_once()
    kwargs = fake_build.call_args.kwargs
    # 'myapp' is the cwd basename — but when cwd is a tmp_path, the
    # detector uses Path.cwd().name. We use a startswith check rather
    # than asserting the literal name, because pytest's tmp_path picks
    # an arbitrary directory name.
    assert kwargs["push"] is True
    assert any(":sha-abc12345" in tag for tag in kwargs["tags"])
    assert any(":latest" in tag for tag in kwargs["tags"])
    assert any(":v1.2.3" in tag for tag in kwargs["tags"])


def test_run_validate_only_on_push_to_main(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "myapp"\nversion = "0.1.0"\n'
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.rs").write_text("fn main() {}\n")
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    (tmp_path / "VERSION").write_text("1.2.3\n")

    # Simulate hyperi-ci's Build job having produced a single-arch
    # binary (push-to-main behaviour); validate path should constrain
    # buildx to that platform only.
    image_name = tmp_path.name
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / f"{image_name}-linux-amd64").write_bytes(b"\x7fELF...")

    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_SHA", "abc12345abc12345abc")

    cfg = _ci_config(container={"enabled": "auto"}, target="oss")

    fake_build = MagicMock(return_value=0)
    monkeypatch.setattr(stage_module, "build_and_push", fake_build)

    assert run(cfg, language="rust") == 0
    kwargs = fake_build.call_args.kwargs
    assert kwargs["push"] is False
    # Validate-only emits NO tags (none would land in the registry anyway)
    assert kwargs["tags"] == []
    # Multi-arch is filtered down to platforms that actually have a
    # binary in dist/.
    assert kwargs["platforms"] == ["linux/amd64"]


def test_run_validate_fails_loud_when_no_dist_binaries(
    tmp_path: Path, monkeypatch
) -> None:
    """Container validate with no dist/ binaries must fail loud, not skip silently.

    Regression test for the artefact-handoff bug: when actions/download-artifact
    finds 0 artefacts (e.g., due to upload/download version mismatch, expired
    artefacts, or Build job failure), the Container stage previously returned 0
    with a warning and never built or pushed an image — silently producing a
    "successful" CI run that did no work and pushed nothing to GHCR.

    Container is configured (publish.container.enabled != false). Missing
    binaries means the Build → Container handoff is broken — fail loud so the
    real failure surfaces in CI instead of being masked as success.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "myapp"\nversion = "0.1.0"\n'
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.rs").write_text("fn main() {}\n")
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")

    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_SHA", "abc12345abc12345abc")

    cfg = _ci_config(container={"enabled": "auto"}, target="oss")

    fake_build = MagicMock(return_value=0)
    monkeypatch.setattr(stage_module, "build_and_push", fake_build)

    # Must fail (return 1) and never call buildx — the missing artefacts
    # indicate a broken Build → Container handoff that needs surfacing.
    assert run(cfg, language="rust") == 1
    fake_build.assert_not_called()


_FAKE_MANIFEST_JSON = (
    '{"binary_name": "myapp", "base_image": "ubuntu:24.04", '
    '"runtime_packages": [], "labels": {}, "env": {}, '
    '"user": {"uid": 10001}}'
)


@pytest.mark.parametrize("artefact_dir", ["ci-tmp", "ci", ".ci"])
def test_build_contract_uses_pre_generated_artefacts(
    tmp_path: Path, monkeypatch, artefact_dir: str
) -> None:
    """Container stage MUST consume pre-generated artefacts, not subprocess the binary.

    The Build stage runs `hyperi-ci run generate` on a runner with the
    Rust toolchain's runtime libs (librdkafka, libssl, ...) installed.
    The Container stage runs on a bare runner that can't load those
    libs, so subprocess-invoking the binary there fails with
    `error while loading shared libraries: librdkafka.so.1`.

    Lookup precedence: ci-tmp/ (CI Build output) → ci/ (committed
    artefacts for local builds) → .ci/ (legacy back-compat).
    """
    project_root = tmp_path / "myapp"
    project_root.mkdir()
    monkeypatch.chdir(project_root)

    (project_root / "Cargo.toml").write_text(
        '[package]\nname = "myapp"\nversion = "0.1.0"\n[dependencies]\nscalo = "2.7"\n',
    )
    (project_root / "src").mkdir()
    (project_root / "src" / "main.rs").write_text("fn main() {}\n")
    (project_root / "VERSION").write_text("0.1.0\n")

    # The pre-generated manifest is the ONLY input the Container stage
    # needs from the contract producer. The binary itself comes from
    # dist/ but isn't invoked here.
    artefacts = project_root / artefact_dir
    artefacts.mkdir()
    (artefacts / "container-manifest.json").write_text(_FAKE_MANIFEST_JSON)

    dist = project_root / "dist"
    dist.mkdir()
    (dist / "myapp-linux-amd64").write_bytes(b"\x7fELF...")

    monkeypatch.setenv("GITHUB_SHA", "abc12345abc12345abc")
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.delenv("GITHUB_REF", raising=False)

    cfg = _ci_config(container={"enabled": "auto"}, target="oss")

    # Fail the test if Container subprocess-invokes the binary in dist/
    # — that's the path that previously hit librdkafka.so.1 errors.
    # Other subprocesses (cargo metadata, git rev-parse) are fine.
    real_run = stage_module.subprocess.run
    binary_path = str(dist / "myapp-linux-amd64")

    def _no_binary_subprocess(cmd, *args, **kwargs):
        if isinstance(cmd, (list, tuple)) and cmd and str(cmd[0]) == binary_path:
            raise AssertionError(
                f"Container stage must not subprocess the binary. cmd={cmd!r}"
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(stage_module.subprocess, "run", _no_binary_subprocess)

    fake_build = MagicMock(return_value=0)
    monkeypatch.setattr(stage_module, "build_and_push", fake_build)

    rc = run(cfg, language="rust")
    assert rc == 0, f"contract build failed: {rc}"
    fake_build.assert_called_once()


def test_build_contract_fails_loud_when_no_artefacts_present(
    tmp_path: Path, monkeypatch
) -> None:
    """Missing artefacts in ci-tmp/, ci/, .ci/ MUST fail loud — never subprocess.

    Regression: pre-fix, the Container stage fell back to invoking the
    binary directly, which only worked on a runner with the Rust
    toolchain's runtime libs installed. We now reject that path
    entirely; the Build stage is responsible for producing artefacts.
    """
    project_root = tmp_path / "myapp"
    project_root.mkdir()
    monkeypatch.chdir(project_root)

    (project_root / "Cargo.toml").write_text(
        '[package]\nname = "myapp"\nversion = "0.1.0"\n[dependencies]\nscalo = "2.7"\n',
    )
    (project_root / "src").mkdir()
    (project_root / "src" / "main.rs").write_text("fn main() {}\n")
    (project_root / "VERSION").write_text("0.1.0\n")

    # Binary present but NO ci-tmp/, ci/, .ci/ — exactly the failure
    # mode that previously masqueraded as success.
    dist = project_root / "dist"
    dist.mkdir()
    (dist / "myapp-linux-amd64").write_bytes(b"\x7fELF...")

    monkeypatch.setenv("GITHUB_SHA", "abc12345abc12345abc")
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.delenv("GITHUB_REF", raising=False)

    cfg = _ci_config(container={"enabled": "auto"}, target="oss")

    fake_build = MagicMock(return_value=0)
    monkeypatch.setattr(stage_module, "build_and_push", fake_build)

    rc = run(cfg, language="rust")
    assert rc == 1, f"expected hard fail, got {rc}"
    fake_build.assert_not_called()


def test_run_legacy_target_both_routes_to_ghcr_only(
    tmp_path: Path, monkeypatch
) -> None:
    """Legacy ``target: both`` is accepted for back-compat but only
    routes to GHCR — JFrog publishing was removed in v2.1.4.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "myapp"\nversion = "0.1.0"\n'
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.rs").write_text("fn main() {}\n")
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    (tmp_path / "VERSION").write_text("1.2.3\n")

    monkeypatch.setenv("GITHUB_SHA", "abc12345abc12345abc")
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.delenv("GITHUB_REF", raising=False)
    monkeypatch.setenv("HYPERCI_PUBLISH_MODE", "true")

    cfg = _ci_config(container={"enabled": "auto"}, target="both")

    fake_build = MagicMock(return_value=0)
    monkeypatch.setattr(stage_module, "build_and_push", fake_build)

    assert run(cfg, language="rust") == 0
    tags = fake_build.call_args.kwargs["tags"]
    assert any("ghcr.io/hyperi-io" in t for t in tags)
    assert not any("jfrog" in t for t in tags)
