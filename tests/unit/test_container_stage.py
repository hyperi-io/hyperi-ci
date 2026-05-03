# Project:   HyperI CI
# File:      tests/unit/test_container_stage.py
# Purpose:   Tests for container stage gate, mode resolution, validate vs push
#
# License:   FSL-1.1-ALv2
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.container import stage as stage_module
from hyperi_ci.container.detect import Decision
from hyperi_ci.container.stage import _normalise_enabled, _resolve_mode, run

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


def test_run_invalid_target_returns_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "myapp"\nversion = "0.1.0"\n'
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.rs").write_text("fn main() {}\n")
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    cfg = _ci_config(container={"enabled": "auto"}, target="dockerhub")
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


def test_run_validate_skips_when_no_dist_binaries(tmp_path: Path, monkeypatch) -> None:
    """Validate-only on push-to-main with no dist/ binaries should skip cleanly."""
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

    # Should return 0 (skip cleanly, not fail) and never call buildx.
    assert run(cfg, language="rust") == 0
    fake_build.assert_not_called()


def test_build_contract_chmods_binary_before_subprocess(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression: actions/download-artifact strips +x; we must restore it.

    Without this chmod, contract-mode apps (Rust + hyperi-rustlib, no
    repo Dockerfile) hit PermissionError [Errno 13] when subprocess
    tries to invoke `<bin> generate-artefacts` to produce the manifest.

    Reproduced on dfe-transform-vrl run 25294105711 against
    hyperi-ci 1.16.1 — covered by this test from 1.16.2 onwards.
    """
    # `_build_contract` uses `Path.cwd().name` as the binary-glob prefix.
    # Run inside a subdir named after the binary so the glob matches.
    project_root = tmp_path / "myapp"
    project_root.mkdir()
    monkeypatch.chdir(project_root)

    # Tier 1 layout: Cargo.toml + Build artefact in dist/, no Dockerfile,
    # no committed .ci/container-manifest.json. Forces contract mode and
    # the run-binary-to-generate-manifest branch.
    (project_root / "Cargo.toml").write_text(
        '[package]\nname = "myapp"\nversion = "0.1.0"\n'
        "[dependencies]\n"
        'hyperi-rustlib = "2.7"\n',
    )
    (project_root / "src").mkdir()
    (project_root / "src" / "main.rs").write_text("fn main() {}\n")
    (project_root / "VERSION").write_text("0.1.0\n")

    dist = project_root / "dist"
    dist.mkdir()
    binary = dist / "myapp-linux-amd64"
    # Fake binary with NO execute bits — mirrors the post-download state.
    binary.write_text(
        "#!/usr/bin/env bash\n"
        'for i in "$@"; do\n'
        '  if [ "$prev" = "--output-dir" ]; then\n'
        '    mkdir -p "$i"\n'
        "    cat > \"$i/container-manifest.json\" <<'JSON'\n"
        '{"binary_name": "myapp", "base_image": "ubuntu:24.04", '
        '"runtime_packages": [], "labels": {}, "env": {}, '
        '"user": {"uid": 10001}}\n'
        "JSON\n"
        "  fi\n"
        '  prev="$i"\n'
        "done\n"
    )
    binary.chmod(0o644)  # Read-only, NOT executable.

    monkeypatch.setenv("GITHUB_SHA", "abc12345abc12345abc")
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.delenv("GITHUB_REF", raising=False)

    cfg = _ci_config(container={"enabled": "auto"}, target="oss")

    fake_build = MagicMock(return_value=0)
    monkeypatch.setattr(stage_module, "build_and_push", fake_build)

    rc = run(cfg, language="rust")
    assert rc == 0, f"contract build failed: {rc}"
    fake_build.assert_called_once()

    # Verify the chmod actually took effect — execute bits must be set.
    assert binary.stat().st_mode & 0o111, "binary should have execute bits after chmod"


def test_run_multi_registry_when_target_both(tmp_path: Path, monkeypatch) -> None:
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

    cfg = _ci_config(container={"enabled": "auto"}, target="both")

    fake_build = MagicMock(return_value=0)
    monkeypatch.setattr(stage_module, "build_and_push", fake_build)

    assert run(cfg, language="rust") == 0
    tags = fake_build.call_args.kwargs["tags"]
    assert any("ghcr.io/hyperi-io" in t for t in tags)
    assert any("hypersec.jfrog.io/hyperi-docker-local" in t for t in tags)
