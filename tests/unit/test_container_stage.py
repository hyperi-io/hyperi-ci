# Project:   HyperI CI
# File:      tests/unit/test_container_stage.py
# Purpose:   Tests for container stage gate, validate vs push
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.container import stage as stage_module
from hyperi_ci.container.detect import Decision
from hyperi_ci.container.stage import (
    _read_version,
    run,
    should_build_container,
)


class TestShouldBuildContainer:
    """Resolve-before-buildx gate (issue #33).

    The container job must decide app-vs-library BEFORE booting Docker
    Buildx, so a library (scalo, a crate -- no GHCR deployment) never
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
            lambda **_: Decision(build=True, reason="Dockerfile found"),
        )
        build, _ = should_build_container(self._cfg("auto"), language="rust")
        assert build is True

    def test_enabled_false_never_builds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # enabled:false short-circuits -- detect must not even be consulted.
        def _boom(**_):  # pragma: no cover - must not be called
            raise AssertionError("detect() should not run when enabled:false")

        monkeypatch.setattr(stage_module, "detect", _boom)
        build, _ = should_build_container(self._cfg("false"), language="rust")
        assert build is False

    def test_enabled_true_forces_build(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # enabled:true means the container is required -- gate opens even
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
    GITHUB_OUTPUT and return 0 WITHOUT any Docker work -- the workflow
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
            lambda **_: Decision(build=True, reason="Dockerfile found"),
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
    """enabled: true is strict -- fail loudly when nothing detected."""
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
    # Release mode is opt-in via HYPERCI_RELEASE_MODE, which the workflow's
    # container job sets from the plan's will-release output; tests set it
    # explicitly.
    monkeypatch.setenv("HYPERCI_RELEASE_MODE", "true")

    cfg = _ci_config(container={"enabled": "auto"}, target="oss")

    fake_build = MagicMock(return_value=0)
    monkeypatch.setattr(stage_module, "build_and_push", fake_build)

    assert run(cfg, language="rust") == 0
    fake_build.assert_called_once()
    kwargs = fake_build.call_args.kwargs
    # 'myapp' is the cwd basename -- but when cwd is a tmp_path, the
    # detector uses Path.cwd().name. We use a startswith check rather
    # than asserting the literal name, because pytest's tmp_path picks
    # an arbitrary directory name.
    assert kwargs["push"] is True
    assert any(":sha-abc12345" in tag for tag in kwargs["tags"])
    assert any(":latest" in tag for tag in kwargs["tags"])
    assert any(":v1.2.3" in tag for tag in kwargs["tags"])


def test_a_release_push_exposes_the_image_by_digest(
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
    outputs = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    monkeypatch.setenv("GITHUB_SHA", "abc12345abc12345abc")
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.delenv("GITHUB_REF", raising=False)
    monkeypatch.setenv("HYPERCI_RELEASE_MODE", "true")
    digest = "sha256:" + "b" * 64

    def fake_build(**kwargs) -> int:
        kwargs["metadata_file"].write_text(
            f'{{"containerimage.digest": "{digest}"}}', encoding="utf-8"
        )
        return 0

    monkeypatch.setattr(stage_module, "build_and_push", fake_build)

    assert (
        run(_ci_config(container={"enabled": "auto"}, target="oss"), language="rust")
        == 0
    )
    lines = outputs.read_text(encoding="utf-8").splitlines()
    assert f"digest={digest}" in lines
    image = next(line for line in lines if line.startswith("image="))
    assert image.endswith(f":v1.2.3@{digest}")


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
    with a warning and never built or pushed an image -- silently producing a
    "successful" CI run that did no work and pushed nothing to GHCR.

    Container is configured (publish.container.enabled != false). Missing
    binaries means the Build → Container handoff is broken -- fail loud so the
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

    # Must fail (return 1) and never call buildx -- the missing artefacts
    # indicate a broken Build → Container handoff that needs surfacing.
    assert run(cfg, language="rust") == 1
    fake_build.assert_not_called()


def test_run_python_library_with_cli_auto_skips(tmp_path: Path, monkeypatch) -> None:
    """Python library that ships a console-script auto-skips (issue #51).

    logreducer's shape: a uv_build-backend library with
    ``[project.scripts]`` and no Dockerfile. Under ``enabled: auto`` the
    Container stage must skip silently -- a CLI is not a container
    workload -- rather than run a failing template build.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "logreducer"\nversion = "3.4.0"\n'
        '[project.scripts]\nlogreducer = "logreducer.cli:main"\n'
    )
    cfg = _ci_config(container={"enabled": "auto"})

    fake_build = MagicMock(return_value=0)
    monkeypatch.setattr(stage_module, "build_and_push", fake_build)

    assert run(cfg, language="python") == 0
    fake_build.assert_not_called()


class TestNoDockerfile:
    """The repo Dockerfile is the only build source; without one nothing crashes."""

    @staticmethod
    def _rust_app(root: Path) -> None:
        (root / "Cargo.toml").write_text(
            '[package]\nname = "myapp"\nversion = "0.1.0"\n'
            '[dependencies]\nscalo = "2.7"\n'
        )
        (root / "src").mkdir()
        (root / "src" / "main.rs").write_text("fn main() {}\n")

    @staticmethod
    def _logs(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
        seen: dict[str, list[str]] = {"warn": [], "error": []}
        monkeypatch.setattr(stage_module, "warn", seen["warn"].append)
        monkeypatch.setattr(stage_module, "error", seen["error"].append)
        return seen

    def test_auto_skips_a_runnable_app_with_a_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        self._rust_app(tmp_path)
        seen = self._logs(monkeypatch)
        fake_build = MagicMock(return_value=0)
        monkeypatch.setattr(stage_module, "build_and_push", fake_build)

        assert run(_ci_config(container={"enabled": "auto"}), language="rust") == 0
        fake_build.assert_not_called()
        assert len(seen["warn"]) == 1
        assert "builds images only from a repo Dockerfile" in seen["warn"][0]

    @pytest.mark.parametrize("mode", ["template", "contract"])
    def test_a_retired_mode_skips_rather_than_crashing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
    ) -> None:
        monkeypatch.chdir(tmp_path)
        self._rust_app(tmp_path)
        seen = self._logs(monkeypatch)
        cfg = _ci_config(container={"enabled": "auto", "mode": mode})

        assert run(cfg, language="rust") == 0
        assert seen["error"] == []

    def test_enabled_true_fails_with_one_clear_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "mysvc"\nversion = "0.1.0"\n'
        )
        seen = self._logs(monkeypatch)
        fake_build = MagicMock(return_value=0)
        monkeypatch.setattr(stage_module, "build_and_push", fake_build)

        assert run(_ci_config(container={"enabled": True}), language="python") == 1
        fake_build.assert_not_called()
        assert len(seen["error"]) == 1
        assert "no Dockerfile" in seen["error"][0]


def test_run_custom_python_dockerfile_needs_no_dist(
    tmp_path: Path, monkeypatch
) -> None:
    """A python app with its OWN Dockerfile (custom mode) that builds from
    source must not be dist-filtered on validate/dev either -- only binary
    languages (rust/go) and dist-referencing Dockerfiles keep the filter.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "mysvc"\nversion = "0.1.0"\n'
    )
    (tmp_path / "Dockerfile").write_text(
        "FROM python:3.12-slim\nCOPY . /app\nRUN pip install /app\n"
    )
    (tmp_path / "VERSION").write_text("0.1.0\n")

    monkeypatch.setenv("GITHUB_SHA", "abc12345abc12345abc")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("HYPERCI_RELEASE_MODE", "false")

    cfg = _ci_config(
        container={
            "enabled": True,
            "platforms": ["linux/amd64", "linux/arm64"],
        },
        target="oss",
    )

    fake_build = MagicMock(return_value=0)
    monkeypatch.setattr(stage_module, "build_and_push", fake_build)

    assert run(cfg, language="python") == 0
    assert fake_build.call_args.kwargs["platforms"] == ["linux/amd64"]


def test_run_custom_ts_multistage_dist_is_not_binary_backed(
    tmp_path: Path, monkeypatch
) -> None:
    """`COPY --from=builder .../dist/` is a multi-stage INTERNAL path (tsc
    output), not a CI artefact copy -- must not trigger the dist filter.
    The ci-test-ts-app pattern, caught live by the branch rehearsal.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "package.json").write_text('{"name": "mysvc"}\n')
    (tmp_path / "Dockerfile").write_text(
        "FROM node:22-alpine AS builder\n"
        "COPY src/ ./src/\n"
        "RUN npm run build\n"
        "FROM node:22-alpine\n"
        "COPY --from=builder --chown=app:app /build/dist/ ./dist/\n"
    )
    (tmp_path / "VERSION").write_text("0.1.0\n")

    monkeypatch.setenv("GITHUB_SHA", "abc12345abc12345abc")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("HYPERCI_RELEASE_MODE", "false")

    cfg = _ci_config(
        container={
            "enabled": True,
            "platforms": ["linux/amd64", "linux/arm64"],
        },
        target="oss",
    )

    fake_build = MagicMock(return_value=0)
    monkeypatch.setattr(stage_module, "build_and_push", fake_build)

    assert run(cfg, language="typescript") == 0
    assert fake_build.call_args.kwargs["platforms"] == ["linux/amd64"]


class TestDistContextCopy:
    def test_context_copy_matches(self) -> None:
        assert stage_module._DIST_CONTEXT_COPY.search("COPY dist/app-linux-amd64 /\n")

    def test_from_stage_copy_does_not_match(self) -> None:
        assert not stage_module._DIST_CONTEXT_COPY.search(
            "COPY --from=builder /build/dist/ ./dist/\n"
        )

    def test_chown_context_copy_matches(self) -> None:
        assert stage_module._DIST_CONTEXT_COPY.search(
            "COPY --chown=app:app dist/app /app\n"
        )


class TestTemplatePlatforms:
    def test_prefers_amd64(self) -> None:
        got = stage_module._template_platforms(["linux/amd64", "linux/arm64"])
        assert got == ["linux/amd64"]

    def test_falls_back_to_first_configured(self) -> None:
        got = stage_module._template_platforms(["linux/arm64", "linux/s390x"])
        assert got == ["linux/arm64"]

    def test_single_platform_passthrough(self) -> None:
        assert stage_module._template_platforms(["linux/amd64"]) == ["linux/amd64"]


def test_run_legacy_target_both_routes_to_ghcr_only(
    tmp_path: Path, monkeypatch
) -> None:
    """Legacy ``target: both`` is accepted for back-compat but only
    routes to GHCR.
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
    monkeypatch.setenv("HYPERCI_RELEASE_MODE", "true")

    cfg = _ci_config(container={"enabled": "auto"}, target="both")

    fake_build = MagicMock(return_value=0)
    monkeypatch.setattr(stage_module, "build_and_push", fake_build)

    assert run(cfg, language="rust") == 0
    tags = fake_build.call_args.kwargs["tags"]
    assert tags
    assert all(t.startswith("ghcr.io/hyperi-io/") for t in tags)
