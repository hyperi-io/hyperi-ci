# Project:   HyperI CI
# File:      tests/unit/test_tool_pins.py
# Purpose:   Tests that semgrep, cargo-hack and the Rust/Go tools honour their pins
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""A tool already on PATH must not silently stand in for the version we pin.

semgrep ran a PATH copy ahead of its pin, cargo-hack had no pin at all, and
the Rust/Go tools ran whatever a dev box carried with nothing saying so.
"""

import contextlib
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from hyperi_ci import tools
from hyperi_ci.config import CIConfig
from hyperi_ci.languages import quality_common
from hyperi_ci.languages.golang import quality as go_quality
from hyperi_ci.languages.rust import quality as rust_quality
from hyperi_ci.quality import osv_scanner, semgrep
from hyperi_ci.versions import tool_version

Run = Callable[..., subprocess.CompletedProcess[str]]


def _done(cmd: list[str], rc: int = 0, out: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")


def _on_path(*present: str) -> Callable[[str], str | None]:
    return lambda name: f"/usr/bin/{name}" if name in present else None


@pytest.fixture(autouse=True)
def _workstation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run as on a dev box, with no strict or skip override in force."""
    monkeypatch.delenv("HYPERCI_QUALITY_STRICT", raising=False)
    monkeypatch.delenv("HYPERCI_QUALITY_SKIP", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


class TestSemgrepRunsItsPin:
    """semgrep resolves the pinned spec first, by the rule #451 set for Python."""

    def _scan(self, monkeypatch: pytest.MonkeyPatch, *present: str) -> list[str]:
        seen: list[list[str]] = []

        def run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess:
            seen.append(list(cmd))
            return _done(cmd)

        monkeypatch.setattr(shutil, "which", _on_path(*present))
        monkeypatch.setattr(subprocess, "run", run)
        assert semgrep.run(CIConfig(_raw={})) == 0
        scans = [c for c in seen if "scan" in c]
        assert len(scans) == 1, seen
        return scans[0]

    def test_the_pin_wins_over_a_semgrep_on_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = self._scan(monkeypatch, "semgrep", "uv", "uvx")
        spec = f"semgrep=={tool_version('semgrep')}"
        assert cmd[:4] == ["uvx", "--from", spec, "semgrep"]

    def test_without_uv_the_path_copy_runs_with_a_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said: list[str] = []
        monkeypatch.setattr(quality_common, "warn", said.append)
        monkeypatch.setattr(quality_common, "installed_version", lambda _b: None)

        cmd = self._scan(monkeypatch, "semgrep")

        assert cmd[:2] == ["semgrep", "scan"]
        assert len(said) == 1, said
        assert f"semgrep=={tool_version('semgrep')}" in said[0]
        assert "uv is not on PATH" in said[0]


class _Cargo:
    """cargo as `cargo hack --version` and `cargo install` see it."""

    def __init__(self, installed: str | None, install_rc: int = 0) -> None:
        self.installed = installed
        self.install_rc = install_rc
        self.installs: list[list[str]] = []

    def which(self, name: str) -> str | None:
        if name == "cargo-hack":
            return "/home/dev/.cargo/bin/cargo-hack" if self.installed else None
        return f"/usr/bin/{name}"

    def run(self, cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess:
        if cmd[:3] == ["cargo", "hack", "--version"]:
            return _done(cmd, out=f"cargo-hack {self.installed}\n")
        if cmd[:2] == ["cargo", "install"]:
            self.installs.append(list(cmd))
            if self.install_rc == 0 and "--version" in cmd:
                self.installed = cmd[cmd.index("--version") + 1]
            return _done(cmd, rc=self.install_rc)
        raise AssertionError(f"unexpected command: {cmd}")


class TestCargoHackRunsItsPin:
    """The feature matrix installs cargo-hack at the pin, and replaces a stray one."""

    def _matrix(self, monkeypatch: pytest.MonkeyPatch, cargo: _Cargo) -> bool:
        monkeypatch.setattr(shutil, "which", cargo.which)
        monkeypatch.setattr(subprocess, "run", cargo.run)
        monkeypatch.setattr(rust_quality, "_package_lib_map", lambda *_a: {})
        monkeypatch.setattr(rust_quality, "_has_lib_target", lambda *_a: True)
        monkeypatch.setattr(rust_quality, "_run_matrix_pass", lambda *_a, **_k: True)
        monkeypatch.setattr(
            rust_quality, "restore_cargo_manifests", contextlib.nullcontext
        )
        return rust_quality._run_feature_matrix(CIConfig(_raw={}))

    def test_a_missing_cargo_hack_is_installed_at_the_pin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cargo = _Cargo(installed=None)
        assert self._matrix(monkeypatch, cargo) is True
        pin = tool_version("cargo-hack")
        assert cargo.installs == [
            ["cargo", "install", "--locked", "cargo-hack", "--version", pin]
        ]

    def test_a_different_version_on_path_is_replaced_by_the_pin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cargo = _Cargo(installed="0.6.30")
        assert self._matrix(monkeypatch, cargo) is True
        assert len(cargo.installs) == 1
        assert cargo.installs[0][-2:] == ["--version", tool_version("cargo-hack")]
        assert cargo.installed == tool_version("cargo-hack")

    def test_the_pinned_version_already_on_path_is_left_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cargo = _Cargo(installed=tool_version("cargo-hack"))
        assert self._matrix(monkeypatch, cargo) is True
        assert cargo.installs == []

    def test_a_failed_install_fails_the_matrix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cargo = _Cargo(installed="0.6.30", install_rc=101)
        assert self._matrix(monkeypatch, cargo) is False

    def test_an_install_that_does_not_take_effect_is_named(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said: list[str] = []
        cargo = _Cargo(installed="0.6.30")
        monkeypatch.setattr(rust_quality, "warn", said.append)
        cargo.run = _shadowed(cargo.run)
        assert self._matrix(monkeypatch, cargo) is True
        assert any("0.6.30" in w and "check PATH" in w for w in said), said


def _shadowed(run: Run) -> Run:
    """An install that reports success while an older cargo-hack still answers."""

    def wrapper(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess:
        if cmd[:2] == ["cargo", "install"]:
            return _done(cmd)
        return run(cmd, **kw)

    return wrapper


class TestMatchesPin:
    @pytest.mark.parametrize(
        ("pin", "output"),
        [
            ("v2.6.0", "osv-scanner version: 2.6.0\ncommit: abc"),
            ("0.20.2", "cargo-deny 0.20.2"),
            ("v1.8.0", "Go: go1.25.1\nScanner: govulncheck@v1.8.0\nDB: x"),
            ("v2.13.2", "golangci-lint has version 2.13.2 built with go1.25"),
        ],
    )
    def test_names_the_pin(self, pin: str, output: str) -> None:
        assert tools.matches_pin(pin, output)

    @pytest.mark.parametrize(
        ("pin", "output"),
        [
            ("0.20.2", "cargo-deny 0.20.21"),
            ("0.20.2", "cargo-deny 10.20.2"),
            ("0.20.2", "cargo-deny 0.20.2.1"),
            ("v2.6.0", "osv-scanner version: 2.5.1"),
            ("v2.6.0", ""),
        ],
    )
    def test_rejects_another_version(self, pin: str, output: str) -> None:
        assert not tools.matches_pin(pin, output)


class _Versions:
    """Tools on PATH answering a version probe, and recording each probe."""

    def __init__(self, reported: dict[str, str]) -> None:
        self.reported = reported
        self.probes: list[list[str]] = []

    def which(self, name: str) -> str | None:
        return f"/usr/bin/{name}" if name in self.reported else None

    def run(self, cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess:
        name = cmd[0].rsplit("/", 1)[-1]
        if len(cmd) == 2 and cmd[1] in ("--version", "-version"):
            self.probes.append(list(cmd))
            return _done(cmd, out=self.reported[name])
        return _done(cmd)


class TestPinDriftWarning:
    """A local PATH tool at another version is named, once, with both versions."""

    def _drift(
        self, monkeypatch: pytest.MonkeyPatch, reported: dict[str, str]
    ) -> tuple[list[str], _Versions]:
        said: list[str] = []
        box = _Versions(reported)
        monkeypatch.setattr(shutil, "which", box.which)
        monkeypatch.setattr(subprocess, "run", box.run)
        monkeypatch.setattr(tools, "warn", said.append)
        return said, box

    def test_a_different_version_is_named_with_the_pin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said, _ = self._drift(monkeypatch, {"cargo-deny": "cargo-deny 0.19.0"})
        tools.warn_on_pin_drift("cargo-deny")
        assert len(said) == 1, said
        assert "cargo-deny 0.19.0" in said[0]
        assert tool_version("cargo-deny") in said[0]

    def test_the_pinned_version_is_quiet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pin = tool_version("cargo-audit").removeprefix("v")
        said, _ = self._drift(monkeypatch, {"cargo-audit": f"cargo-audit {pin}"})
        tools.warn_on_pin_drift("cargo-audit")
        assert said == []

    def test_an_absent_tool_is_left_to_the_missing_tool_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said, box = self._drift(monkeypatch, {})
        tools.warn_on_pin_drift("gosec")
        assert said == []
        assert box.probes == []

    def test_an_unreadable_version_is_said_rather_than_passed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said, _ = self._drift(monkeypatch, {"gosec": ""})
        tools.warn_on_pin_drift("gosec")
        assert len(said) == 1
        assert "could not read" in said[0]

    def test_govulncheck_is_asked_with_its_own_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pin = tool_version("govulncheck")
        said, box = self._drift(
            monkeypatch, {"govulncheck": f"Go: go1.25\nScanner: govulncheck@{pin}"}
        )
        tools.warn_on_pin_drift("govulncheck")
        assert box.probes == [["/usr/bin/govulncheck", "-version"]]
        assert said == []

    def test_go_handler_names_drift_once_per_tool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def bare(name: str) -> str:
            return tool_version(name).removeprefix("v")

        said, _ = self._drift(
            monkeypatch,
            {
                "gofmt": "",
                "go": "",
                "golangci-lint": "golangci-lint has version 1.64.0 built with go",
                "gosec": f"Version: {bare('gosec')}",
                "govulncheck": f"Scanner: govulncheck@{tool_version('govulncheck')}",
            },
        )
        monkeypatch.setattr(go_quality, "warn", said.append)

        assert go_quality.run(CIConfig(_raw={})) == 0

        drift = [w for w in said if "pins" in w]
        assert len(drift) == 1, said
        assert "golangci-lint" in drift[0]
        assert "1.64.0" in drift[0]

    def test_rust_handler_names_cargo_audit_drift(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said, _ = self._drift(
            monkeypatch, {"cargo": "", "cargo-audit": "cargo-audit 0.18.3"}
        )
        assert rust_quality._run_tool(
            "cargo audit", ["cargo", "audit"], "blocking", pinned="cargo-audit"
        )
        assert len(said) == 1, said
        assert "0.18.3" in said[0]
        assert tool_version("cargo-audit") in said[0]

    def test_osv_scanner_names_drift_before_scanning(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        said, _ = self._drift(
            monkeypatch, {"osv-scanner": "osv-scanner version: 2.5.1\ncommit: x"}
        )
        lockfile = tmp_path / "Cargo.lock"
        lockfile.write_text("", encoding="utf-8")

        assert osv_scanner.run(lockfile, [], "warn")

        assert len(said) == 1, said
        assert "2.5.1" in said[0]
        assert tool_version("osv-scanner") in said[0]
