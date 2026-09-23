# Project:   HyperI CI
# File:      tests/unit/test_lint_compose.py
# Purpose:   Tests for compose discovery, the resolution gate and the orchestrator
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for the compose linting dimension (Path C).

Discovery and the fragment/stack split run against real compose files in
tmp_path. ``docker compose`` itself is only exercised through its absence: the
gate has to fail in CI when it cannot run, and warn-skip locally.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.quality import compose_config, lint_compose
from hyperi_ci.quality.targets import discover_compose_files

_STACK = """\
services:
  app:
    image: ghcr.io/org/app:${APP_VERSION:?set APP_VERSION}
    volumes:
      - ${DFE_SRC_ROOT:?set DFE_SRC_ROOT}/config:/etc/app:ro
"""

_FRAGMENT = """\
services:
  app:
    ports: !reset []
"""

_NOT_COMPOSE = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: app
"""


def _cfg(**quality: object) -> CIConfig:
    return CIConfig(_raw={"quality": quality} if quality else {})


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


class TestDiscovery:
    def test_finds_the_documented_namings(self, tmp_path: Path) -> None:
        _write(tmp_path / "docker-compose.yml", _STACK)
        _write(tmp_path / "compose.yaml", _STACK)
        _write(tmp_path / "templates" / "redpanda.compose.yaml", _STACK)
        found = {p.name for p in discover_compose_files(tmp_path)}
        assert found == {"docker-compose.yml", "compose.yaml", "redpanda.compose.yaml"}

    def test_yaml_without_services_is_not_compose(self, tmp_path: Path) -> None:
        _write(tmp_path / "docker-compose.yml", _NOT_COMPOSE)
        assert discover_compose_files(tmp_path) == []

    def test_custom_merge_tag_does_not_hide_a_fragment(self, tmp_path: Path) -> None:
        _write(tmp_path / "docker-compose.override.yml", _FRAGMENT)
        assert [p.name for p in discover_compose_files(tmp_path)] == [
            "docker-compose.override.yml"
        ]

    def test_excluded_dir_is_pruned(self, tmp_path: Path) -> None:
        _write(tmp_path / "vendor" / "docker-compose.yml", _STACK)
        assert discover_compose_files(tmp_path, exclude_dirs=["vendor"]) == []


class TestFragmentSplit:
    def test_patch_only_file_is_a_fragment(self, tmp_path: Path) -> None:
        assert compose_config.is_fragment(
            _write(tmp_path / "docker-compose.override.yml", _FRAGMENT)
        )

    def test_file_with_an_image_is_a_stack(self, tmp_path: Path) -> None:
        assert not compose_config.is_fragment(
            _write(tmp_path / "docker-compose.yml", _STACK)
        )


class TestPlaceholderEnv:
    def test_every_mandatory_key_gets_a_value(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "docker-compose.yml", _STACK)
        env = compose_config.placeholder_env(path)
        assert set(env) == {"APP_VERSION", "DFE_SRC_ROOT"}

    def test_path_shaped_key_gets_an_absolute_path(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "docker-compose.yml", _STACK)
        env = compose_config.placeholder_env(path)
        assert Path(env["DFE_SRC_ROOT"]).is_absolute()
        assert not Path(env["APP_VERSION"]).is_absolute()


class TestMissingCompose:
    @pytest.fixture
    def _absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(compose_config, "compose_available", lambda: False)

    def test_blocking_gate_fails_in_ci(
        self, _absent: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CI", "true")
        path = _write(tmp_path / "docker-compose.yml", _STACK)
        assert compose_config.run([path], _cfg()) == 1

    def test_warn_skips_locally(
        self, _absent: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL", "BUILDKITE"):
            monkeypatch.delenv(name, raising=False)
        path = _write(tmp_path / "docker-compose.yml", _STACK)
        assert compose_config.run([path], _cfg()) == 0

    def test_fragment_only_repo_never_needs_compose(
        self, _absent: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CI", "true")
        path = _write(tmp_path / "docker-compose.override.yml", _FRAGMENT)
        assert compose_config.run([path], _cfg()) == 0


class TestRun:
    def test_no_compose_files_is_not_a_failure(self, tmp_path: Path) -> None:
        assert lint_compose.run(tmp_path, _cfg()) == 0

    def test_pin_gate_fails_without_docker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(compose_config, "compose_available", lambda: False)
        for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL", "BUILDKITE"):
            monkeypatch.delenv(name, raising=False)
        _write(
            tmp_path / "docker-compose.yml",
            "services:\n  app:\n    image: nginx\n",
        )
        assert lint_compose.run(tmp_path, _cfg()) == 1

    def test_both_gates_disabled_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(compose_config, "compose_available", lambda: False)
        _write(
            tmp_path / "docker-compose.yml",
            "services:\n  app:\n    image: nginx\n",
        )
        cfg = _cfg(compose_config="disabled", compose_pins="disabled")
        assert lint_compose.run(tmp_path, cfg) == 0
