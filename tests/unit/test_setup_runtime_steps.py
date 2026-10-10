# Project:   HyperI CI
# File:      tests/unit/test_setup_runtime_steps.py
# Purpose:   Run the setup-runtime composite's install steps under bash
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Execute the setup-runtime steps with recording ``uv`` and CLI shims.

The PATH holds only the shim directory, so ``command -v python3`` answers
exactly what each test puts there, and every ``uv`` or CLI call lands in a log
the test reads back.
"""

import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ACTION = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "actions"
    / "setup-runtime"
    / "action.yml"
)
_EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
_BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(_BASH is None, reason="needs bash")


def _action() -> dict:
    return yaml.safe_load(ACTION.read_text(encoding="utf-8"))


def _step(name: str) -> dict:
    return next(s for s in _action()["runs"]["steps"] if s.get("name") == name)


def _shim(bin_dir: Path, name: str, log: Path) -> None:
    shim = bin_dir / name
    shim.write_text(f'#!{_BASH}\necho "{name} $*" >> "{log}"\n', encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)


def _run(
    step_name: str,
    inputs: dict[str, str],
    tmp_path: Path,
    shims: tuple[str, ...],
    *,
    rc: int = 0,
    stdout: list[str] | None = None,
) -> list[str]:
    """Run one step with the action's defaults overlaid by ``inputs``.

    Asserts the step exits ``rc``; ``stdout``, when given, receives its output.
    """
    values = {
        f"inputs.{key}": str(spec.get("default", ""))
        for key, spec in _action()["inputs"].items()
    }
    values |= {f"inputs.{key}": value for key, value in inputs.items()}
    step = _step(step_name)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")
    for name in shims:
        _shim(bin_dir, name, log)
    env = {
        key: _EXPRESSION.sub(lambda m: values[m.group(1)], str(value))
        for key, value in step["env"].items()
    }
    result = subprocess.run(
        [str(_BASH), "-e", "-c", str(step["run"])],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={"PATH": str(bin_dir), **env},
        check=False,
    )
    assert result.returncode == rc, result.stderr
    if stdout is not None:
        stdout.extend(result.stdout.splitlines())
    return log.read_text(encoding="utf-8").splitlines()


class TestForcePythonInstall:
    def test_the_input_defaults_off(self) -> None:
        spec = _action()["inputs"]["force-python-install"]
        assert spec["default"] == "false"
        assert spec["required"] is False

    def test_unset_keeps_the_runner_python3(self, tmp_path: Path) -> None:
        calls = _run("Set up Python", {}, tmp_path, ("uv", "python3"))
        assert calls == []

    def test_unset_installs_when_the_runner_has_no_python3(
        self, tmp_path: Path
    ) -> None:
        calls = _run("Set up Python", {"python-version": "3.13"}, tmp_path, ("uv",))
        assert calls == ["uv python install --no-bin 3.13"]

    def test_true_installs_over_the_runner_python3(self, tmp_path: Path) -> None:
        inputs = {"python-version": "3.13", "force-python-install": "true"}
        calls = _run("Set up Python", inputs, tmp_path, ("uv", "python3"))
        assert calls == ["uv python install --no-bin 3.13"]

    @pytest.mark.parametrize("value", ["false", "", "TRUE", "yes", "1"])
    def test_anything_but_true_is_the_guarded_install(
        self, value: str, tmp_path: Path
    ) -> None:
        inputs = {"force-python-install": value}
        calls = _run("Set up Python", inputs, tmp_path, ("uv", "python3"))
        assert calls == []


class TestUvMustBePresent:
    def test_no_uv_fails_instead_of_fetching_an_installer(self, tmp_path: Path) -> None:
        # No uv and no curl shim: a pipe-to-shell fallback would fail differently.
        out: list[str] = []
        calls = _run("Set up Python", {}, tmp_path, ("python3",), rc=1, stdout=out)
        assert calls == []
        assert any(line.startswith("::error::uv is not on PATH") for line in out)

    def test_the_step_never_pipes_a_script_to_a_shell(self) -> None:
        assert "| sh" not in str(_step("Set up Python")["run"])


class TestNativeDependencies:
    def test_the_install_command_splits_into_words(self, tmp_path: Path) -> None:
        inputs = {"hyperci-install": "hyperi-ci --flag", "language": "golang"}
        calls = _run("Install native dependencies", inputs, tmp_path, ("hyperi-ci",))
        assert calls == ["hyperi-ci --flag install-native-deps golang"]

    def test_the_command_variable_stays_out_of_the_config_namespace(self) -> None:
        # load_config folds every HYPERCI_* variable into config.
        env = _step("Install native dependencies")["env"]
        assert not [key for key in env if key.startswith("HYPERCI_")]
