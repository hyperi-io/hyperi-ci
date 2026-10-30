# Project:   HyperI CI
# File:      tests/unit/test_container_stage.py
# Purpose:   Tests for container stage gate, validate vs push
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED

import json
import re
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hyperi_ci.config import CIConfig, OrgConfig
from hyperi_ci.container import stage as stage_module
from hyperi_ci.container.build import PublishedImage
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


# --- HYPERCI_CONTAINER_MODE: plan, leg, merge ---------------------------------

_REV = "feedfacecafebeef0123456789abcdef01234567"
_AMD = "sha256:" + "a" * 64
_ARM = "sha256:" + "b" * 64
_INDEX = "sha256:" + "c" * 64
_CREATED = "2026-10-31T01:02:03Z"
_BOTH = ["linux/amd64", "linux/arm64"]
_MODE_ENV = (
    "HYPERCI_CONTAINER_MODE",
    "HYPERCI_CONTAINER_PLATFORM",
    "HYPERCI_CONTAINER_CREATED",
    "HYPERCI_CONTAINER_DIGEST_DIR",
    "HYPERCI_CONTAINER_PLATFORMS",
    "HYPERCI_CONTAINER_REUSE",
    "HYPERCI_CONTAINER_RESOLVE_ONLY",
)


class _Seams:
    """Records what a mode asked docker and the registry to do."""

    def __init__(self) -> None:
        self.builds: list[dict] = []
        self.writes: list[list[str]] = []
        self.outputs: dict[str, str] = {}
        self.published: dict[str, PublishedImage | None] = {}
        self.leg_digest: str | None = None


@pytest.fixture
def seams(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Seams:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "thing"\ndescription = "Ships the thing"\n',
        encoding="utf-8",
    )
    (tmp_path / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HYPERCI_VERSION", "1.2.3")
    monkeypatch.setenv("GITHUB_SHA", _REV)
    for name in _MODE_ENV:
        monkeypatch.delenv(name, raising=False)
    seen = _Seams()

    def build_and_push(**kwargs) -> int:
        seen.builds.append(kwargs)
        if seen.leg_digest and kwargs["metadata_file"] is not None:
            kwargs["metadata_file"].write_text(
                json.dumps({"containerimage.digest": seen.leg_digest}),
                encoding="utf-8",
            )
        return 0

    def registry_write(cmd: list[str], **_kwargs) -> subprocess.CompletedProcess:
        seen.writes.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(stage_module, "build_and_push", build_and_push)
    monkeypatch.setattr(stage_module, "run_cmd", registry_write)
    monkeypatch.setattr(stage_module, "set_github_output", seen.outputs.update)
    monkeypatch.setattr(stage_module, "published_image", seen.published.get)
    return seen


def _repo() -> str:
    return f"ghcr.io/hyperi-io/{Path.cwd().name}"


def _mode(push_mode: str, mode: str | None, **raw) -> int:
    return stage_module._dispatch_build(
        dockerfile_path=Path("Dockerfile"),
        config=CIConfig(_raw={"release": {"container": raw}} if raw else {}),
        org=OrgConfig(),
        registry_bases=["ghcr.io/hyperi-io"],
        push_mode=push_mode,
        binary_backed=False,
        mode=mode,
    )


def _flag_values(cmd: list[str], flag: str) -> list[str]:
    return [cmd[i + 1] for i, arg in enumerate(cmd) if arg == flag]


class TestModeSelection:
    def test_an_unknown_mode_fails_naming_the_valid_ones(
        self, seams: _Seams, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        errors: list[str] = []
        monkeypatch.setattr(stage_module, "error", errors.append)
        monkeypatch.setenv("HYPERCI_CONTAINER_MODE", "split")
        assert run(_ci_config(container={"enabled": True}), language="python") == 1
        assert seams.builds == []
        assert "plan, leg, merge" in errors[0]

    @pytest.mark.parametrize("resolve_only", [True, False])
    def test_plan_writes_every_output_with_or_without_resolve_only(
        self, seams: _Seams, monkeypatch: pytest.MonkeyPatch, resolve_only: bool
    ) -> None:
        """The workflow sets both, so an older CLI only resolves."""
        monkeypatch.setenv("HYPERCI_CONTAINER_MODE", "plan")
        monkeypatch.setenv("HYPERCI_RELEASE_MODE", "true")
        if resolve_only:
            monkeypatch.setenv("HYPERCI_CONTAINER_RESOLVE_ONLY", "1")
        assert run(_ci_config(container={"enabled": True}), language="python") == 0
        assert seams.builds == []
        assert seams.outputs["build"] == "true"
        assert json.loads(seams.outputs["platforms"]) == _BOTH
        assert set(seams.outputs) == {"build", "platforms", "push", "reuse", "created"}

    def test_plan_for_a_library_builds_nothing_and_plans_nothing(
        self, seams: _Seams, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HYPERCI_CONTAINER_MODE", "plan")
        assert run(_ci_config(container={"enabled": False}), language="rust") == 0
        assert seams.outputs == {
            "build": "false",
            "platforms": "[]",
            "push": "false",
            "reuse": "false",
            "created": "",
        }


class TestPlanMode:
    @pytest.mark.parametrize(
        ("push_mode", "platforms", "push"),
        [
            pytest.param("release", _BOTH, "true", id="release-every-platform"),
            pytest.param("dev", ["linux/amd64"], "true", id="dev-one-arch"),
            pytest.param("validate", ["linux/amd64"], "false", id="validate-no-push"),
        ],
    )
    def test_plan_takes_the_platforms_a_single_build_would(
        self, seams: _Seams, push_mode: str, platforms: list[str], push: str
    ) -> None:
        assert _mode(push_mode, "plan") == 0
        assert seams.builds == []
        assert seams.writes == []
        assert json.loads(seams.outputs["platforms"]) == platforms
        assert seams.outputs["push"] == push
        assert seams.outputs["reuse"] == "false"
        assert re.fullmatch(
            r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", seams.outputs["created"]
        )

    @pytest.mark.parametrize(
        ("revision", "arches", "reuse"),
        [
            pytest.param(_REV, frozenset(_BOTH), "true", id="this-commit-everywhere"),
            pytest.param("0" * 40, frozenset(_BOTH), "false", id="another-commit"),
            pytest.param(_REV, frozenset({"linux/amd64"}), "false", id="missing-arm"),
        ],
    )
    def test_plan_reports_reuse_without_writing_the_registry(
        self, seams: _Seams, revision: str, arches: frozenset[str], reuse: str
    ) -> None:
        seams.published[f"{_repo()}:v1.2.3"] = PublishedImage(_INDEX, revision, arches)
        assert _mode("release", "plan") == 0
        assert seams.outputs["reuse"] == reuse
        assert seams.writes == []


class TestLegMode:
    @pytest.mark.parametrize(
        ("platform", "created", "digest_dir"),
        [
            pytest.param("", _CREATED, True, id="no-platform"),
            pytest.param("linux/arm64", "", True, id="no-created"),
            pytest.param("linux/arm64", _CREATED, False, id="push-without-dir"),
        ],
    )
    def test_a_missing_input_fails_before_any_build(
        self,
        seams: _Seams,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        platform: str,
        created: str,
        digest_dir: bool,
    ) -> None:
        monkeypatch.setenv("HYPERCI_CONTAINER_PLATFORM", platform)
        monkeypatch.setenv("HYPERCI_CONTAINER_CREATED", created)
        if digest_dir:
            monkeypatch.setenv("HYPERCI_CONTAINER_DIGEST_DIR", str(tmp_path / "d"))
        assert _mode("release", "leg") == 1
        assert seams.builds == []

    def test_a_push_leg_pushes_one_platform_by_digest(
        self, seams: _Seams, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        digests = tmp_path / "digests"
        monkeypatch.setenv("HYPERCI_CONTAINER_PLATFORM", "linux/arm64")
        monkeypatch.setenv("HYPERCI_CONTAINER_CREATED", _CREATED)
        monkeypatch.setenv("HYPERCI_CONTAINER_DIGEST_DIR", str(digests))
        seams.leg_digest = _ARM

        assert _mode("release", "leg") == 0
        (built,) = seams.builds
        assert built["platforms"] == ["linux/arm64"]
        assert built["tags"] == []
        assert built["push"] is False
        assert built["output"] == (
            f"type=image,name={_repo()},push-by-digest=true,"
            "name-canonical=true,push=true"
        )
        assert built["labels"]["org.opencontainers.image.created"] == _CREATED
        assert (digests / "arm64").read_text(encoding="utf-8") == f"{_ARM}\n"
        assert seams.outputs == {}

    def test_a_push_leg_with_no_digest_fails(
        self, seams: _Seams, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        digests = tmp_path / "digests"
        monkeypatch.setenv("HYPERCI_CONTAINER_PLATFORM", "linux/amd64")
        monkeypatch.setenv("HYPERCI_CONTAINER_CREATED", _CREATED)
        monkeypatch.setenv("HYPERCI_CONTAINER_DIGEST_DIR", str(digests))
        assert _mode("dev", "leg") == 1
        assert not digests.exists()

    def test_a_validate_leg_builds_without_pushing_or_recording(
        self, seams: _Seams, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HYPERCI_CONTAINER_PLATFORM", "linux/arm64")
        monkeypatch.setenv("HYPERCI_CONTAINER_CREATED", _CREATED)
        assert _mode("validate", "leg") == 0
        (built,) = seams.builds
        assert built["push"] is False
        assert built["output"] is None
        assert built["metadata_file"] is None


class TestMergeMode:
    @staticmethod
    def _digests(tmp_path: Path, **files: str) -> Path:
        digests = tmp_path / "digests"
        digests.mkdir()
        for name, digest in files.items():
            (digests / name).write_text(f"{digest}\n", encoding="utf-8")
        return digests

    @staticmethod
    def _env(monkeypatch: pytest.MonkeyPatch, digests: Path, platforms: list[str]):
        monkeypatch.setenv("HYPERCI_CONTAINER_DIGEST_DIR", str(digests))
        monkeypatch.setenv("HYPERCI_CONTAINER_PLATFORMS", json.dumps(platforms))
        monkeypatch.setenv("HYPERCI_CONTAINER_CREATED", _CREATED)

    def test_two_legs_become_one_annotated_index(
        self, seams: _Seams, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._env(monkeypatch, self._digests(tmp_path, amd64=_AMD, arm64=_ARM), _BOTH)
        seams.published[f"{_repo()}:v1.2.3"] = PublishedImage(
            _INDEX, _REV, frozenset(_BOTH)
        )
        assert _mode("release", "merge") == 0
        assert seams.builds == []
        (cmd,) = seams.writes
        assert cmd[:4] == ["docker", "buildx", "imagetools", "create"]
        assert cmd[-2:] == [f"{_repo()}@{_AMD}", f"{_repo()}@{_ARM}"]
        tags = _flag_values(cmd, "--tag")
        assert tags[0] == f"{_repo()}:v1.2.3"
        assert f"{_repo()}:sha-{_REV[:8]}" in tags
        annotations = _flag_values(cmd, "--annotation")
        assert f"index:org.opencontainers.image.revision={_REV}" in annotations
        assert f"index:org.opencontainers.image.created={_CREATED}" in annotations
        assert seams.outputs == {
            "digest": _INDEX,
            "image": f"{_repo()}:v1.2.3@{_INDEX}",
        }

    def test_one_leg_is_tagged_with_no_annotations(
        self, seams: _Seams, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._env(monkeypatch, self._digests(tmp_path, amd64=_AMD), ["linux/amd64"])
        seams.published[f"{_repo()}:branch-x"] = PublishedImage(
            _AMD, _REV, frozenset({"linux/amd64"})
        )
        monkeypatch.setattr(stage_module, "dev_branch_slug", lambda: "x")
        assert _mode("dev", "merge") == 0
        (cmd,) = seams.writes
        assert _flag_values(cmd, "--annotation") == []
        assert _flag_values(cmd, "--tag") == [
            f"{_repo()}:branch-x",
            f"{_repo()}:branch-x-sha-{_REV[:8]}",
        ]
        assert cmd[-1] == f"{_repo()}@{_AMD}"

    @pytest.mark.parametrize(
        ("files", "named"),
        [
            pytest.param({"amd64": _AMD}, "missing: arm64, extra: none", id="missing"),
            pytest.param(
                {"amd64": _AMD, "arm64": _ARM, "s390x": _AMD},
                "missing: none, extra: s390x",
                id="extra",
            ),
            pytest.param({"amd64": _AMD, "arm64": "latest"}, "no sha256", id="bad"),
        ],
    )
    def test_digests_that_do_not_match_the_plan_fail_closed(
        self,
        seams: _Seams,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        files: dict[str, str],
        named: str,
    ) -> None:
        errors: list[str] = []
        monkeypatch.setattr(stage_module, "error", errors.append)
        self._env(monkeypatch, self._digests(tmp_path, **files), _BOTH)
        assert _mode("release", "merge") == 1
        assert seams.writes == []
        assert named in errors[-1]

    def test_reuse_repoints_the_published_image_and_reads_no_digest(
        self, seams: _Seams, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._env(monkeypatch, self._digests(tmp_path), _BOTH)
        monkeypatch.setenv("HYPERCI_CONTAINER_REUSE", "true")
        seams.published[f"{_repo()}:v1.2.3"] = PublishedImage(
            _INDEX, _REV, frozenset(_BOTH)
        )
        assert _mode("release", "merge") == 0
        (cmd,) = seams.writes
        assert cmd[-1] == f"{_repo()}@{_INDEX}"
        assert f"{_repo()}:v1.2.3" not in _flag_values(cmd, "--tag")
        assert seams.outputs["image"] == f"{_repo()}:v1.2.3@{_INDEX}"

    def test_reuse_with_the_image_gone_fails(
        self, seams: _Seams, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._env(monkeypatch, self._digests(tmp_path), _BOTH)
        monkeypatch.setenv("HYPERCI_CONTAINER_REUSE", "true")
        assert _mode("release", "merge") == 1
        assert seams.writes == []

    @pytest.mark.parametrize(
        "readback",
        [
            pytest.param(None, id="unreadable"),
            pytest.param(PublishedImage(_INDEX, None, frozenset(_BOTH)), id="no-rev"),
            pytest.param(
                PublishedImage(_INDEX, _REV, frozenset({"linux/amd64"})), id="one-arch"
            ),
        ],
    )
    def test_an_index_that_reads_back_wrong_fails(
        self,
        seams: _Seams,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        readback: PublishedImage | None,
    ) -> None:
        """Reuse needs the revision on the index, or every re-run rebuilds."""
        self._env(monkeypatch, self._digests(tmp_path, amd64=_AMD, arm64=_ARM), _BOTH)
        seams.published[f"{_repo()}:v1.2.3"] = readback
        assert _mode("release", "merge") == 1
        assert seams.outputs == {}

    @pytest.mark.parametrize(
        ("platforms", "with_dir"),
        [
            pytest.param(None, True, id="no-platforms"),
            pytest.param("[]", True, id="empty"),
            pytest.param("linux/amd64", True, id="not-json"),
            pytest.param('["linux/amd64","linux/amd64"]', True, id="duplicate"),
            pytest.param(json.dumps(_BOTH), False, id="no-dir"),
        ],
    )
    def test_bad_merge_inputs_fail_before_the_registry(
        self,
        seams: _Seams,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        platforms: str | None,
        with_dir: bool,
    ) -> None:
        if with_dir:
            monkeypatch.setenv("HYPERCI_CONTAINER_DIGEST_DIR", str(tmp_path))
        if platforms is not None:
            monkeypatch.setenv("HYPERCI_CONTAINER_PLATFORMS", platforms)
        assert _mode("release", "merge") == 1
        assert seams.writes == []

    def test_merge_refuses_validate_mode(
        self, seams: _Seams, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._env(monkeypatch, self._digests(tmp_path, amd64=_AMD), ["linux/amd64"])
        assert _mode("validate", "merge") == 1
        assert seams.writes == []
