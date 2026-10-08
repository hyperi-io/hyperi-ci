# Project:   HyperI CI
# File:      tests/unit/test_repo_advisor.py
# Purpose:   Tests for the non-blocking alint repo-hygiene advisory wrapper
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
import json
import shutil
import subprocess
from pathlib import Path
from typing import cast

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.languages.rust import targets
from hyperi_ci.quality import repo_advisor


class _Config:
    """Minimal CIConfig stand-in exposing .get(key, default)."""

    def __init__(self, alint: str = "auto", language: str | None = None) -> None:
        self._alint = alint
        # Mirror CIConfig.language only when set - the no-language variant
        # exercises the getattr fallback in repo_advisor.run.
        if language is not None:
            self.language = language

    def get(self, key: str, default=None):
        if key == "quality.alint":
            return self._alint
        return default


def _cfg(alint: str = "auto", language: str | None = None) -> CIConfig:
    """Cast the minimal stand-in to CIConfig - the advisory only calls .get()."""
    return cast(CIConfig, _Config(alint, language))


def _have_alint(monkeypatch: pytest.MonkeyPatch, path: str = "/bin/alint") -> None:
    """Pretend alint is on PATH (run() resolves it through ci_binary)."""
    monkeypatch.setattr(repo_advisor, "ci_binary", lambda _n: path)


def _no_alint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend alint is absent, locally (no CI install path)."""
    monkeypatch.setattr(repo_advisor, "ci_binary", lambda _n: None)
    monkeypatch.setattr(repo_advisor, "is_ci", lambda: False)


def _stub_run(monkeypatch: pytest.MonkeyPatch, rc: int = 0) -> list[list[str]]:
    """Capture the command run_cmd is called with; return the capture list."""
    calls: list[list[str]] = []

    def fake_run_cmd(cmd, *, check=True, cwd=None, **_kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, rc, "", "")

    monkeypatch.setattr(repo_advisor, "run_cmd", fake_run_cmd)
    return calls


def _stub_run_capture_cfg(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture the CONTENT of the -c config at call time.

    The generated override layer lives in a TemporaryDirectory that dies when
    run() returns, so it must be read inside the fake run_cmd.
    """
    configs: list[str] = []

    def fake_run_cmd(cmd, *, check=True, cwd=None, **_kw):
        if "-c" in cmd:
            cfg = Path(cmd[cmd.index("-c") + 1])
            configs.append(cfg.read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(repo_advisor, "run_cmd", fake_run_cmd)
    return configs


def test_disabled_mode_never_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_run(monkeypatch)
    # find_tool must not even be consulted when disabled.
    monkeypatch.setattr(
        repo_advisor, "find_tool", lambda *a, **k: pytest.fail("should not resolve")
    )
    assert repo_advisor.run(_cfg("disabled"), Path(".")) == 0
    assert calls == []


def test_missing_alint_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_run(monkeypatch)
    _no_alint(monkeypatch)
    notices: list[tuple] = []
    monkeypatch.setattr(
        repo_advisor, "find_tool", lambda *a, **k: notices.append((a, k))
    )
    assert repo_advisor.run(_cfg("auto"), tmp_path) == 0
    assert calls == []  # nothing to run
    assert len(notices) == 1  # the install nudge still fires


def test_runs_with_packaged_config_when_no_repo_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub_run(monkeypatch, rc=0)
    _have_alint(monkeypatch)
    monkeypatch.setattr(repo_advisor, "is_ci", lambda: False)
    assert repo_advisor.run(_cfg("auto"), tmp_path) == 0
    (cmd,) = calls
    assert cmd[:3] == ["/bin/alint", "check", "--format"]
    assert "human" in cmd  # local
    assert "-c" in cmd  # ships the HyperI default
    assert cmd[-1].endswith("hyperi.alint.yml")


def test_repo_config_wins_no_dash_c(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".alint.yml").write_text("version: 1\n", encoding="utf-8")
    calls = _stub_run(monkeypatch, rc=0)
    _have_alint(monkeypatch)
    monkeypatch.setattr(repo_advisor, "is_ci", lambda: False)
    assert repo_advisor.run(_cfg("auto"), tmp_path) == 0
    (cmd,) = calls
    assert "-c" not in cmd  # let alint discover the repo's own .alint.yml


def test_ci_uses_github_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_run(monkeypatch, rc=1)  # error-level findings...
    _have_alint(monkeypatch)
    monkeypatch.setattr(repo_advisor, "is_ci", lambda: True)
    # ...still returns 0: advisory, never gates the build.
    assert repo_advisor.run(_cfg("auto"), tmp_path) == 0
    (cmd,) = calls
    assert "github" in cmd


def test_exec_failure_is_non_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Binary resolves but can't be exec'd (removed after which(), broken
    # symlink): run_cmd raises OSError - the advisory must still return 0.
    def boom(*_a, **_k):
        raise OSError("no such file")

    warns: list[str] = []
    _have_alint(monkeypatch)
    monkeypatch.setattr(repo_advisor, "is_ci", lambda: False)
    monkeypatch.setattr(repo_advisor, "run_cmd", boom)
    monkeypatch.setattr(repo_advisor, "warn", lambda m: warns.append(m))
    assert repo_advisor.run(_cfg("auto"), tmp_path) == 0
    assert any("not failing" in w for w in warns)


def test_alint_internal_error_still_non_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_run(monkeypatch, rc=2)  # config/internal error
    warns: list[str] = []
    _have_alint(monkeypatch)
    monkeypatch.setattr(repo_advisor, "is_ci", lambda: False)
    monkeypatch.setattr(repo_advisor, "warn", lambda m: warns.append(m))
    assert repo_advisor.run(_cfg("auto"), tmp_path) == 0
    assert any("advisory only" in w for w in warns)


# --- Primary-language-scoped default (issue #75) ---------------------------


def test_known_language_generates_override_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configs = _stub_run_capture_cfg(monkeypatch)
    _have_alint(monkeypatch)
    monkeypatch.setattr(repo_advisor, "is_ci", lambda: False)
    assert repo_advisor.run(_cfg("auto"), tmp_path, language="typescript") == 0
    (layer,) = configs
    # The layer extends the shipped default (one file - alint 0.13 honours
    # only the first -c and 0.14 rejects a second outright, so layering must
    # happen via extends).
    assert "hyperi.alint.yml" in layer
    # Both layer and packaged default sit outside the linted repo; without
    # this opt-out alint 0.14's extends confinement hard-errors the run.
    assert "allow_out_of_root: true" in layer
    # Non-primary ecosystems' root-only rules are off...
    for rule in (
        "go-mod-exists",
        "go-sum-exists",
        "rust-cargo-toml-exists",
        "python-manifest-exists",
    ):
        assert f"- id: {rule}\n    level: off" in layer
    # ...the primary's own stay active.
    assert "node-package-json-exists" not in layer
    assert "node-has-lockfile" not in layer


def test_language_falls_back_to_config_attribute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configs = _stub_run_capture_cfg(monkeypatch)
    _have_alint(monkeypatch)
    monkeypatch.setattr(repo_advisor, "is_ci", lambda: False)
    assert repo_advisor.run(_cfg("auto", language="rust"), tmp_path) == 0
    (layer,) = configs
    assert "- id: go-mod-exists\n    level: off" in layer
    assert "rust-cargo-toml-exists" not in layer


_CARGO_LOCK_OFF = "- id: rust-cargo-lock-exists\n    level: off"


def _rust_crate(root: Path, *, main: bool = False) -> Path:
    """Lay out a single Rust crate: a library, plus ``src/main.rs`` if ``main``."""
    (root / "Cargo.toml").write_text(
        '[package]\nname = "crate"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (root / "src").mkdir()
    (root / "src" / "lib.rs").write_text("pub fn f() {}\n", encoding="utf-8")
    if main:
        (root / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    return root


def _cargo_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the filesystem fallback so the answer does not depend on the host."""

    def _absent(*_a, **_k):
        raise FileNotFoundError(2, "No such file or directory: 'cargo'")

    monkeypatch.setattr(targets, "run_cmd", _absent)


def _cargo_reports(monkeypatch: pytest.MonkeyPatch, *members: list[dict]) -> None:
    """Stand in for ``cargo metadata --no-deps``, one package per target list."""
    packages = [{"name": f"m{i}", "targets": t} for i, t in enumerate(members)]
    payload = json.dumps({"packages": packages})
    monkeypatch.setattr(
        targets,
        "run_cmd",
        lambda cmd, **_k: subprocess.CompletedProcess(cmd, 0, payload, ""),
    )


def _layer_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, language: str) -> str:
    configs = _stub_run_capture_cfg(monkeypatch)
    _have_alint(monkeypatch)
    monkeypatch.setattr(repo_advisor, "is_ci", lambda: False)
    assert repo_advisor.run(_cfg("auto"), tmp_path, language=language) == 0
    (layer,) = configs
    return layer


def test_rust_library_turns_off_cargo_lock_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """scalo-rs: a library that deliberately leaves Cargo.lock uncommitted."""
    _cargo_absent(monkeypatch)
    layer = _layer_for(_rust_crate(tmp_path), monkeypatch, "rust")
    assert _CARGO_LOCK_OFF in layer
    # The primary's other root-only rules stay on.
    assert "rust-cargo-toml-exists" not in layer
    assert "rust-toolchain-pinned" not in layer


def test_rust_app_keeps_cargo_lock_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cargo_absent(monkeypatch)
    layer = _layer_for(_rust_crate(tmp_path, main=True), monkeypatch, "rust")
    assert "rust-cargo-lock-exists" not in layer


def test_feature_gated_bin_still_keeps_cargo_lock_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dfe-loader: its only extra bin is feature-gated, and it is still an app."""
    _cargo_reports(
        monkeypatch,
        [
            {"name": "crate", "kind": ["lib"]},
            {"name": "pgo-driver", "kind": ["bin"], "required-features": ["pgo"]},
        ],
    )
    layer = _layer_for(_rust_crate(tmp_path), monkeypatch, "rust")
    assert "rust-cargo-lock-exists" not in layer


def test_workspace_with_a_bin_member_keeps_cargo_lock_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bin in any workspace member makes the whole workspace an app."""
    _cargo_reports(
        monkeypatch,
        [{"name": "core", "kind": ["lib"]}],
        [{"name": "app", "kind": ["bin"]}],
    )
    layer = _layer_for(_rust_crate(tmp_path), monkeypatch, "rust")
    assert "rust-cargo-lock-exists" not in layer


def test_workspace_of_libraries_turns_off_cargo_lock_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cargo_reports(
        monkeypatch,
        [{"name": "core", "kind": ["lib"]}],
        [{"name": "macros", "kind": ["proc-macro"]}],
    )
    layer = _layer_for(_rust_crate(tmp_path), monkeypatch, "rust")
    assert _CARGO_LOCK_OFF in layer


def test_non_rust_primary_is_unchanged_by_a_rust_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-primary Rust group already has the rule off, and only once."""
    _cargo_absent(monkeypatch)
    layer = _layer_for(_rust_crate(tmp_path), monkeypatch, "python")
    assert layer.count("- id: rust-cargo-lock-exists") == 1
    assert "python-has-lockfile" not in layer


def test_unknown_language_uses_plain_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # "none" (CIConfig's default) and anything unrecognised: no primary to
    # judge against -> the shipped default runs unmodified.
    calls = _stub_run(monkeypatch)
    _have_alint(monkeypatch)
    monkeypatch.setattr(repo_advisor, "is_ci", lambda: False)
    assert repo_advisor.run(_cfg("auto"), tmp_path, language="none") == 0
    (cmd,) = calls
    assert cmd[-1].endswith("hyperi.alint.yml")


def test_repo_config_wins_even_with_language(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".alint.yml").write_text("version: 1\n", encoding="utf-8")
    calls = _stub_run(monkeypatch)
    _have_alint(monkeypatch)
    monkeypatch.setattr(repo_advisor, "is_ci", lambda: False)
    assert repo_advisor.run(_cfg("auto"), tmp_path, language="typescript") == 0
    (cmd,) = calls
    assert "-c" not in cmd  # the repo's own config still wins outright


# --- CI-time pinned install (tools.alint SSoT) -----------------------------


def test_run_uses_ci_installed_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub_run(monkeypatch)
    asked: list[str] = []
    monkeypatch.setattr(repo_advisor, "is_ci", lambda: True)
    monkeypatch.setattr(
        repo_advisor, "ci_binary", lambda name: asked.append(name) or "/dl/alint"
    )
    assert repo_advisor.run(_cfg("auto"), tmp_path, language="python") == 0
    (cmd,) = calls
    assert cmd[0] == "/dl/alint"
    assert asked == ["alint"]


def test_pin_and_digest_come_from_the_ssot() -> None:
    """No mirrored constant to track: the installer reads the SSOT directly."""
    from hyperi_ci import versions as ssot

    assert ssot.tool_version("alint").startswith("v")
    assert len(ssot.tool_sha256("alint", "x86_64")) == 64


@pytest.mark.skipif(shutil.which("alint") is None, reason="alint not installed")
def test_override_layer_suppresses_nested_go_mod_with_real_alint(
    tmp_path: Path,
) -> None:
    """E2E for issue #75: TS-primary monorepo, nested go.mod, real alint."""
    (tmp_path / "package.json").write_text(
        '{"name": "fixture", "private": true}\n', encoding="utf-8"
    )
    nested = tmp_path / "packages" / "oc"
    nested.mkdir(parents=True)
    (nested / "go.mod").write_text(
        "module example.com/oc\n\ngo 1.22\n", encoding="utf-8"
    )

    layer = repo_advisor._override_layer("typescript", tmp_path)
    assert layer is not None
    layer_path = tmp_path / "override.yml"
    layer_path.write_text(layer, encoding="utf-8", newline="\n")

    result = subprocess.run(
        ["alint", "check", str(tmp_path), "-c", str(layer_path), "--compact"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    out = result.stdout + result.stderr
    assert "go-mod-exists" not in out
    assert "go-sum-exists" not in out
    # The advisory still checks the primary ecosystem's root rules.
    assert "node-has-lockfile" in out
    # Exit 0/1 = ran (1 would mean error-level findings remain); >=2 = broken
    # config, which is exactly what this test exists to catch.
    assert result.returncode in (0, 1)
