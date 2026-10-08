# Project:   HyperI CI
# File:      tests/unit/test_gate_annotation_level.py
# Purpose:   Only a check that can fail the job raises an error annotation
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""The level each finding gate surfaces at, by mode.

A gate running at ``warn``, or one that is advisory in every mode, cannot fail
the job, so an error annotation from it is a red mark on a green job and spends
the step's 10-error annotation budget. Each gate runs a real fake binary; only
the binary lookup and :func:`findings.surface` are replaced.
"""

import json
from pathlib import Path

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.quality import (
    checkov,
    compose_config,
    compose_pins,
    droast,
    hadolint,
    kube_linter,
    kubeconform,
)
from hyperi_ci.quality import findings as fdg

_SARIF_ERROR = json.dumps(
    {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": "x", "rules": []}},
                "results": [
                    {
                        "ruleId": "R1",
                        "level": "error",
                        "message": {"text": "bad"},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": "Dockerfile"},
                                    "region": {"startLine": 1},
                                }
                            }
                        ],
                    }
                ],
            }
        ],
    }
)

_MODES = [("warn", "warning", 0), ("blocking", "error", 1)]


def _config(tool: str, mode: str) -> CIConfig:
    return CIConfig(_raw={"quality": {tool: mode}})


def _fake_tool(tmp_path: Path, name: str, body: str) -> str:
    exe = tmp_path / "bin" / name
    exe.parent.mkdir(exist_ok=True)
    exe.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    exe.chmod(0o755)
    return str(exe)


def _printing(tmp_path: Path, name: str, output: str, rc: int = 0) -> str:
    return _fake_tool(tmp_path, name, f"cat <<'EOF'\n{output}\nEOF\nexit {rc}")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("HYPERCI_QUALITY_SKIP", "HYPERCI_QUALITY_STRICT"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def surfaced(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    levels: list[str] = []

    def capture(tool, findings, *, sarif_path=None):  # noqa: ANN001
        levels.extend(f.level for f in findings)
        return 0

    monkeypatch.setattr(fdg, "surface", capture)
    return levels


@pytest.mark.parametrize(("mode", "level", "rc"), _MODES)
def test_hadolint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surfaced, mode, level, rc
) -> None:
    report = json.dumps(
        [{"file": "Dockerfile", "line": 1, "level": "error", "code": "SC2086"}]
    )
    exe = _printing(tmp_path, "hadolint", report)
    monkeypatch.setattr(hadolint, "ci_binary", lambda _n: exe)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Dockerfile").write_text("FROM x\n", encoding="utf-8")
    assert hadolint.run(_config("hadolint", mode)) == rc
    assert surfaced == [level]


@pytest.mark.parametrize(("mode", "level", "rc"), _MODES)
def test_kubeconform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surfaced, mode, level, rc
) -> None:
    report = json.dumps(
        {"resources": [{"filename": "a.yaml", "kind": "X", "status": "statusInvalid"}]}
    )
    exe = _printing(tmp_path, "kubeconform", report, rc=1)
    monkeypatch.setattr(kubeconform, "ci_binary", lambda _n: exe)
    monkeypatch.setattr(kubeconform, "CACHE_DIR", tmp_path / "cache")
    config = _config("kubeconform", mode)
    assert kubeconform.run([tmp_path / "a.yaml"], config) == rc
    assert surfaced == [level]


@pytest.mark.parametrize(("mode", "level", "rc"), _MODES)
def test_checkov(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surfaced, mode, level, rc
) -> None:
    body = (
        'while [ $# -gt 0 ]; do [ "$1" = --output-file-path ] && out="$2"; shift; done\n'
        f"cat > \"$out/results_sarif.sarif\" <<'EOF'\n{_SARIF_ERROR}\nEOF"
    )
    exe = _fake_tool(tmp_path, "checkov", body)
    monkeypatch.setattr(checkov, "_base_cmd", lambda: [exe])
    assert checkov.run(tmp_path, _config("checkov", mode)) == rc
    assert surfaced == [level]


@pytest.mark.parametrize(("mode", "level", "rc"), _MODES)
def test_compose_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surfaced, mode, level, rc
) -> None:
    body = '[ "$2" = version ] && exit 0\necho "service app: bad" >&2\nexit 15'
    _fake_tool(tmp_path, "docker", body)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    stack = tmp_path / "compose.yaml"
    stack.write_text("services:\n  app:\n    image: nginx:1.27\n", encoding="utf-8")
    assert compose_config.run([stack], _config("compose_config", mode)) == rc
    assert surfaced == [level]


@pytest.mark.parametrize(("mode", "level", "rc"), _MODES)
def test_compose_pins(tmp_path: Path, surfaced, mode, level, rc) -> None:
    stack = tmp_path / "compose.yaml"
    stack.write_text("services:\n  app:\n    image: nginx\n", encoding="utf-8")
    assert compose_pins.run([stack], _config("compose_pins", mode)) == rc
    assert surfaced == [level]


@pytest.mark.parametrize("mode", ["warn", "blocking"])
def test_kube_linter_is_advisory_in_every_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surfaced, mode
) -> None:
    exe = _printing(tmp_path, "kube-linter", _SARIF_ERROR, rc=1)
    monkeypatch.setattr(kube_linter, "ci_binary", lambda _n: exe)
    config = _config("kube_linter", mode)
    assert kube_linter.run([tmp_path / "a.yaml"], config) == 0
    assert surfaced == ["warning"]


@pytest.mark.parametrize("mode", ["warn", "blocking"])
def test_droast_is_advisory_in_every_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surfaced, mode
) -> None:
    exe = _printing(tmp_path, "droast", _SARIF_ERROR)
    monkeypatch.setattr(droast, "find_tool", lambda *_a, **_k: exe)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Dockerfile").write_text("FROM x\n", encoding="utf-8")
    assert droast.run(_config("droast", mode)) == 0
    assert surfaced == ["warning"]
