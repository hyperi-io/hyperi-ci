# Project:   HyperI CI
# File:      tests/unit/test_pip_audit_lock.py
# Purpose:   pip-audit audits the project's lock, not its own environment
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""pip-audit audits the pins in uv.lock.

``uv run --with pip-audit`` puts pip-audit's own dependencies (requests,
certifi) into the audited environment, so an advisory against one of them failed
every Python repo whether or not it used it. With a lock the pins are exported
and audited on their own.
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from hyperi_ci.common import run_cmd
from hyperi_ci.languages.python import quality
from hyperi_ci.languages.python.quality import _build_pip_audit_cmd
from hyperi_ci.quality.ignores import IgnoreEntry
from hyperi_ci.versions import tool_version

_SPEC = f"pip-audit=={tool_version('pip-audit')}"
_IGNORE = IgnoreEntry("pip-audit", "PYSEC-1", "r")


def _uv_on_path(monkeypatch: pytest.MonkeyPatch, *, present: bool = True) -> None:
    monkeypatch.setattr(
        quality.shutil, "which", lambda cmd: f"/usr/bin/{cmd}" if present else None
    )


class TestCommand:
    """The argv for each way pip-audit can be reached."""

    def test_a_lock_export_is_audited_as_pins_in_its_own_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _uv_on_path(monkeypatch)
        monkeypatch.setattr(quality, "_parse_python", lambda: "3.14")
        assert _build_pip_audit_cmd([_IGNORE], Path("req.txt")) == [
            "uvx",
            "--python",
            "3.14",
            "--from",
            _SPEC,
            "pip-audit",
            "--ignore-vuln",
            "PYSEC-1",
            "-r",
            "req.txt",
            "--disable-pip",
            "--no-deps",
        ]

    def test_no_lock_keeps_the_environment_audit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A repo with no uv.lock has no pins to read, so .venv is all there is."""
        _uv_on_path(monkeypatch)
        assert _build_pip_audit_cmd([_IGNORE]) == [
            "uv",
            "run",
            "--with",
            _SPEC,
            "--",
            "pip-audit",
            "--ignore-vuln",
            "PYSEC-1",
        ]

    def test_no_uv_is_bare_pip_audit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _uv_on_path(monkeypatch, present=False)
        assert _build_pip_audit_cmd([_IGNORE]) == [
            "pip-audit",
            "--ignore-vuln",
            "PYSEC-1",
        ]
        assert _build_pip_audit_cmd([_IGNORE], Path("req.txt")) == [
            "pip-audit",
            "--ignore-vuln",
            "PYSEC-1",
        ]


class _Recorder:
    """Stand in for the export and the audit, recording both in order."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, export_rc: int = 0) -> None:
        self.steps: list[tuple[str, list[str]]] = []
        self.said: list[str] = []
        self.export_rc = export_rc
        self.requirements_existed_during_audit = False
        monkeypatch.setattr(quality, "run_cmd", self._export)
        monkeypatch.setattr(quality, "run_gate_tool", self._audit)
        monkeypatch.setattr(quality, "_parse_python", lambda: "3.14")
        monkeypatch.setattr(quality, "error", self.said.append)
        monkeypatch.setattr(quality, "warn", self.said.append)

    def _export(self, cmd: list[str], **_kw: object) -> subprocess.CompletedProcess:
        self.steps.append(("export", cmd))
        if self.export_rc == 0:
            Path(cmd[cmd.index("-o") + 1]).write_text("six==1.16.0\n", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, self.export_rc, "", "lock is broken")

    def _audit(self, _name: str, cmd: list[str], _mode: str, **_kw: object) -> bool:
        self.steps.append(("audit", cmd))
        if "-r" in cmd:
            self.requirements_existed_during_audit = Path(
                cmd[cmd.index("-r") + 1]
            ).is_file()
        return True

    def kinds(self) -> list[str]:
        return [kind for kind, _cmd in self.steps]


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A directory holding a uv.lock, with uv on PATH."""
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _uv_on_path(monkeypatch)
    return tmp_path


class TestFlow:
    """Export first, audit the export, and leave nothing behind."""

    def test_the_lock_is_exported_then_that_file_is_audited(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = _Recorder(monkeypatch)
        assert quality._run_pip_audit("blocking", [_IGNORE]) is True

        assert rec.kinds() == ["export", "audit"]
        export = rec.steps[0][1]
        assert export[:-2] == [
            "uv",
            "export",
            "--frozen",
            "--no-hashes",
            "--no-header",
            "--all-extras",
            "--all-groups",
            "--no-emit-workspace",
            "--no-emit-local",
            "--format",
            "requirements-txt",
        ]
        assert export[-2] == "-o"
        audit = rec.steps[1][1]
        assert audit[audit.index("-r") + 1] == export[-1]
        assert rec.requirements_existed_during_audit
        assert not Path(export[-1]).exists()

    def test_no_lock_skips_the_export(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _uv_on_path(monkeypatch)
        rec = _Recorder(monkeypatch)
        assert quality._run_pip_audit("blocking", []) is True
        assert rec.kinds() == ["audit"]
        assert rec.steps[0][1][:4] == ["uv", "run", "--with", _SPEC]

    def test_no_uv_skips_the_export(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _uv_on_path(monkeypatch, present=False)
        rec = _Recorder(monkeypatch)
        assert quality._run_pip_audit("blocking", []) is True
        assert rec.steps == [("audit", ["pip-audit"])]

    def test_a_disabled_gate_exports_nothing(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = _Recorder(monkeypatch)
        assert quality._run_pip_audit("disabled", []) is True
        assert rec.kinds() == ["audit"]

    @pytest.mark.parametrize(
        ("mode", "continues"), [("blocking", False), ("warn", True)]
    )
    def test_a_failed_export_checks_nothing_and_says_so(
        self,
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
        continues: bool,
    ) -> None:
        """Falling back to the environment would bring the pip-audit advisories back."""
        rec = _Recorder(monkeypatch, export_rc=2)
        assert quality._run_pip_audit(mode, []) is continues
        assert rec.kinds() == ["export"]
        assert any("could not export uv.lock" in line for line in rec.said)


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")
class TestRealLock:
    """A real lock and a real pip-audit: needs PyPI and the advisory feed."""

    def test_pip_audits_own_dependencies_are_not_in_the_audited_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "locked"\nversion = "0.1.0"\n'
            'requires-python = ">=3.12"\ndependencies = ["six==1.16.0"]\n',
            encoding="utf-8",
        )
        run_cmd(["uv", "lock"], cwd=tmp_path, capture=True)
        assert "certifi" not in (tmp_path / "uv.lock").read_text(encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        audited: list[str] = []
        real = quality.run_gate_tool

        def gate(name: str, cmd: list[str], mode: str, **kw: Any) -> bool:
            listing = run_cmd([*cmd, "--format", "json"], check=False, capture=True)
            deps = json.loads(listing.stdout)["dependencies"]
            audited.extend(dep["name"] for dep in deps)
            return real(name, cmd, mode, **kw)

        monkeypatch.setattr(quality, "run_gate_tool", gate)

        assert quality._run_pip_audit("blocking", []) is True
        assert audited == ["six"]
