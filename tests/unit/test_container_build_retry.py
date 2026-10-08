# Project:   HyperI CI
# File:      tests/unit/test_container_build_retry.py
# Purpose:   Tests for the apt-mirror rebuild in build_and_push
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED

from pathlib import Path

import pytest
import yaml

from hyperi_ci.container import build
from hyperi_ci.container.build import MIRROR_MARKERS, build_and_push, retry_settings

_MIRROR_FAIL = (
    "#6 1.2 E: Failed to fetch http://ports.ubuntu.com/ubuntu-ports/dists/"
    "noble-security/InRelease  File has unexpected size (2886090 != 2895913). "
    "Mirror sync in progress? [IP: 91.189.91.102 80]\n"
    "ERROR: failed to build: exit code: 100"
)
_OTHER_FAIL = "#6 1.2 E: Unable to locate package nosuchpkg\nERROR: exit code: 100"


class _Script:
    """A stand-in for stream_cmd that replays one (code, output) per call."""

    def __init__(self, results: list[tuple[int, str]]) -> None:
        self.results = list(results)
        self.calls: list[list[str]] = []
        self.on_chunk = None

    def __call__(self, cmd, *, on_line=None, on_chunk=None, **_kw):
        self.calls.append(list(cmd))
        self.on_chunk = on_chunk
        code, output = self.results.pop(0)
        if on_chunk is not None:
            on_chunk(output)
        return code, output


def _run(script: _Script, monkeypatch, *, attempts: int = 3) -> tuple[int, list[float]]:
    monkeypatch.setattr(build, "stream_cmd", script)
    slept: list[float] = []
    rc = build_and_push(
        dockerfile_path=Path("Dockerfile"),
        tags=["ghcr.io/hyperi-io/x:1"],
        platforms=["linux/arm64"],
        labels={},
        push=False,
        attempts=attempts,
        retry_delay=60.0,
        sleep=slept.append,
    )
    return rc, slept


@pytest.mark.parametrize("marker", MIRROR_MARKERS)
def test_each_marker_is_recognised(marker: str) -> None:
    assert build.mirror_marker(f"before {marker} after") == marker


def test_marker_then_success_rebuilds_the_same_command(monkeypatch) -> None:
    script = _Script([(1, _MIRROR_FAIL), (0, "built")])
    rc, slept = _run(script, monkeypatch)
    assert rc == 0
    assert len(script.calls) == 2
    assert script.calls[0] == script.calls[1]
    assert slept == [60.0]


def test_marker_on_every_attempt_fails_after_n(monkeypatch) -> None:
    script = _Script([(1, _MIRROR_FAIL)] * 3)
    rc, slept = _run(script, monkeypatch, attempts=3)
    assert rc == 1
    assert len(script.calls) == 3
    assert slept == [60.0, 60.0]


def test_failure_without_marker_is_not_retried(monkeypatch) -> None:
    script = _Script([(100, _OTHER_FAIL)])
    rc, slept = _run(script, monkeypatch)
    assert rc == 100
    assert len(script.calls) == 1
    assert slept == []


def test_one_attempt_turns_the_retry_off(monkeypatch) -> None:
    script = _Script([(1, _MIRROR_FAIL)])
    rc, slept = _run(script, monkeypatch, attempts=1)
    assert rc == 1
    assert len(script.calls) == 1
    assert slept == []


def test_output_is_streamed_unchanged_and_each_retry_is_warned(
    monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    warned: list[str] = []
    monkeypatch.setattr(build, "warn", warned.append)
    script = _Script([(1, _MIRROR_FAIL), (0, "second build output\n")])
    _run(script, monkeypatch)
    out = capsys.readouterr().out
    assert _MIRROR_FAIL in out
    assert "second build output" in out
    assert script.on_chunk is build.echo_chunk
    assert len(warned) == 1
    assert "'Mirror sync in progress'" in warned[0]


class TestRetrySettings:
    def test_shipped_defaults_are_three_attempts_a_minute_apart(self) -> None:
        defaults = Path(build.__file__).parents[1] / "config" / "defaults.yaml"
        data = yaml.safe_load(defaults.read_text(encoding="utf-8"))
        container = data["release"]["container"]
        assert retry_settings(container) == (3, 60.0)
        assert container["build_attempts"] == 3
        assert container["build_retry_delay_seconds"] == 60

    def test_configured_values_are_used(self) -> None:
        cfg = {"build_attempts": 5, "build_retry_delay_seconds": 0}
        assert retry_settings(cfg) == (5, 0.0)

    @pytest.mark.parametrize("bad", [0, -1, "3", None, True, 2.5])
    def test_bad_attempts_fall_back(self, bad: object) -> None:
        assert retry_settings({"build_attempts": bad})[0] == 3

    @pytest.mark.parametrize("bad", [-1, "60", None, True])
    def test_bad_delay_falls_back(self, bad: object) -> None:
        assert retry_settings({"build_retry_delay_seconds": bad})[1] == 60.0
