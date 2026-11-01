# Project:   HyperI CI
# File:      tests/unit/test_lint_compose.py
# Purpose:   Tests for compose discovery, the resolution gate and lint-iac's compose dimension
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for the compose linting dimension.

Discovery and the fragment/stack split run against real compose files in
tmp_path. ``docker compose`` itself is only exercised through its absence: the
gate has to fail in CI when it cannot run, and warn-skip locally.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.quality import compose_config, lint_iac
from hyperi_ci.quality.targets import discover_compose_files


def _lint_compose(root: Path, cfg: CIConfig) -> int:
    return lint_iac.run(root, cfg, dimensions=lint_iac.COMPOSE_DIMENSIONS)


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
        _write(tmp_path / "deploy" / "compose" / "app.yaml", _STACK)
        _write(tmp_path / "deploy" / "compose" / "values.yaml", _NOT_COMPOSE)
        _write(tmp_path / "deploy" / "compose" / "nested" / "x.yaml", _STACK)
        found = {p.name for p in discover_compose_files(tmp_path)}
        assert found == {
            "docker-compose.yml",
            "compose.yaml",
            "redpanda.compose.yaml",
            "app.yaml",
        }

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


def _names(stack: list[Path]) -> list[str]:
    return [p.name for p in stack]


class TestOverlayStack:
    def test_a_base_stands_alone(self, tmp_path: Path) -> None:
        base = _write(tmp_path / "docker-compose.yml", _STACK)
        assert compose_config.overlay_stack(base) == []

    def test_an_overlay_resolves_on_its_base(self, tmp_path: Path) -> None:
        _write(tmp_path / "docker-compose.yml", _STACK)
        overlay = _write(tmp_path / "docker-compose.dfe.yml", _FRAGMENT)
        assert _names(compose_config.overlay_stack(overlay)) == [
            "docker-compose.yml",
            "docker-compose.dfe.yml",
        ]

    def test_a_dotted_overlay_stacks_the_file_named_by_its_last_segment(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path / "docker-compose.yml", _STACK)
        _write(tmp_path / "docker-compose.dev.yml", _STACK)
        _write(tmp_path / "docker-compose.dfe.yml", _FRAGMENT)
        overlay = _write(tmp_path / "docker-compose.dfe.dev.yml", _FRAGMENT)
        assert _names(compose_config.overlay_stack(overlay)) == [
            "docker-compose.yml",
            "docker-compose.dev.yml",
            "docker-compose.dfe.dev.yml",
        ]

    def test_a_dotted_overlay_without_a_middle_file_uses_the_base_only(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path / "compose.yaml", _STACK)
        overlay = _write(tmp_path / "compose.a.b.yml", _FRAGMENT)
        assert _names(compose_config.overlay_stack(overlay)) == [
            "compose.yaml",
            "compose.a.b.yml",
        ]

    def test_an_overlay_with_no_base_stands_alone(self, tmp_path: Path) -> None:
        overlay = _write(tmp_path / "docker-compose.dfe.yml", _FRAGMENT)
        assert compose_config.overlay_stack(overlay) == []

    def test_a_base_in_another_directory_does_not_count(self, tmp_path: Path) -> None:
        _write(tmp_path / "docker-compose.yml", _STACK)
        overlay = _write(tmp_path / "sub" / "docker-compose.dfe.yml", _FRAGMENT)
        assert compose_config.overlay_stack(overlay) == []

    def test_compose_and_docker_compose_names_do_not_mix(self, tmp_path: Path) -> None:
        _write(tmp_path / "docker-compose.yml", _STACK)
        other = _write(tmp_path / "compose.dfe.yml", _FRAGMENT)
        assert compose_config.overlay_stack(other) == []

    def test_placeholders_cover_every_layer(self, tmp_path: Path) -> None:
        base = _write(tmp_path / "docker-compose.yml", _STACK)
        overlay = _write(
            tmp_path / "docker-compose.x.yml",
            "services:\n  app:\n    environment:\n      A: ${EXTRA_KEY:?set it}\n",
        )
        env = compose_config.placeholder_env(base, overlay)
        assert "EXTRA_KEY" in env


class TestStandaloneFirst:
    @pytest.fixture
    def calls(self, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
        seen: list[list[str]] = []

        def _fake(cmd: list[str], _fail: object, **_k: object) -> SimpleNamespace:
            seen.append(cmd)
            stacked = cmd.count("-f") > 1
            return SimpleNamespace(
                returncode=0 if stacked or "bad" not in " ".join(cmd) else 1,
                stderr='service "app" has neither an image nor a build context',
                stdout="",
            )

        monkeypatch.setattr(compose_config.fdg, "run_tool", _fake)
        return seen

    def test_a_complete_overlay_named_file_is_never_merged_with_the_base(
        self, tmp_path: Path, calls: list[list[str]]
    ) -> None:
        _write(tmp_path / "docker-compose.yml", _STACK)
        full = _write(tmp_path / "docker-compose.dev.yml", _STACK)
        assert compose_config._validate(full) is None
        assert len(calls) == 1
        assert calls[0].count("-f") == 1

    def test_a_failing_overlay_is_retried_with_its_stack(
        self, tmp_path: Path, calls: list[list[str]]
    ) -> None:
        _write(tmp_path / "docker-compose.yml", _STACK)
        overlay = _write(tmp_path / "docker-compose.bad.yml", _FRAGMENT)
        assert compose_config._validate(overlay) is None
        assert len(calls) == 2
        assert calls[1][2:6] == [
            "-f",
            "docker-compose.yml",
            "-f",
            "docker-compose.bad.yml",
        ]

    def test_a_failing_file_with_no_base_reports_the_standalone_error(
        self, tmp_path: Path, calls: list[list[str]]
    ) -> None:
        overlay = _write(tmp_path / "docker-compose.bad.yml", _FRAGMENT)
        found = compose_config._validate(overlay)
        assert found is not None
        assert found.message.startswith('service "app" has neither')
        assert len(calls) == 1


class TestErrorDetail:
    _NOTICE = (
        'time="2026-10-16T00:16:21+11:00" level=warning '
        'msg="The \\"POSTGRES_USER\\" variable is not set."'
    )

    def test_variable_notices_are_skipped(self) -> None:
        out = (
            f"{self._NOTICE}\n{self._NOTICE}\n"
            'service "app" has neither an image nor a build context specified: '
            "invalid compose project\n"
        )
        assert compose_config.error_detail(out, 1).startswith('service "app" has')

    def test_the_last_error_line_wins(self) -> None:
        assert compose_config.error_detail("first\nsecond\n", 1) == "second"

    def test_only_notices_falls_back_to_the_last_line(self) -> None:
        assert compose_config.error_detail(self._NOTICE, 1) == self._NOTICE

    def test_empty_output_names_the_exit_code(self) -> None:
        assert compose_config.error_detail("", 3) == "docker compose exited 3"


class TestPlaceholderEnv:
    @pytest.fixture(autouse=True)
    def _unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("APP_VERSION", raising=False)
        monkeypatch.delenv("DFE_SRC_ROOT", raising=False)

    def test_every_mandatory_key_gets_a_value(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "docker-compose.yml", _STACK)
        env = compose_config.placeholder_env(path)
        assert set(env) == {"APP_VERSION", "DFE_SRC_ROOT"}

    def test_path_shaped_key_gets_an_absolute_path(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "docker-compose.yml", _STACK)
        env = compose_config.placeholder_env(path)
        assert Path(env["DFE_SRC_ROOT"]).is_absolute()
        assert not Path(env["APP_VERSION"]).is_absolute()

    def test_a_value_already_set_is_not_overwritten(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APP_VERSION", "1.2.3")
        path = _write(tmp_path / "docker-compose.yml", _STACK)
        assert set(compose_config.placeholder_env(path)) == {"DFE_SRC_ROOT"}

    def test_an_empty_value_still_gets_a_placeholder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APP_VERSION", "")
        path = _write(tmp_path / "docker-compose.yml", _STACK)
        assert "APP_VERSION" in compose_config.placeholder_env(path)


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
        assert _lint_compose(tmp_path, _cfg()) == 0

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
        assert _lint_compose(tmp_path, _cfg()) == 1

    def test_both_gates_disabled_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(compose_config, "compose_available", lambda: False)
        _write(
            tmp_path / "docker-compose.yml",
            "services:\n  app:\n    image: nginx\n",
        )
        cfg = _cfg(compose_config="disabled", compose_pins="disabled")
        assert _lint_compose(tmp_path, cfg) == 0
