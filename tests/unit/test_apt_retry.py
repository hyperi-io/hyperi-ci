# Project:   HyperI CI
# File:      tests/unit/test_apt_retry.py
# Purpose:   Every generated apt-get call survives a mirror mid-sync
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Every apt-get call hyperi-ci writes into a Dockerfile retries.

A mirror caught mid-sync serves an index whose size disagrees with its Release
file. apt 2.8.3 fails that fetch once and exits 100 without honouring
``Acquire::Retries``, so the update itself has to be re-run after a pause.
"""

import re
import subprocess
from pathlib import Path

import pytest

from hyperi_ci.container.compose import compose_contract_dockerfile
from hyperi_ci.container.manifest import ContainerManifest
from hyperi_ci.container.templates import render_python_template

_APT_CALL = re.compile(r"apt-get((?:\s+-o\s+\S+)*)\s+(update|install)\b")
_UPDATE_LOOP = re.compile(
    r"for i in [0-9 ]+; do apt-get -o Acquire::Retries=5 update && break;"
)


def _joined(dockerfile: str) -> str:
    """Fold Dockerfile line continuations so one RUN reads as one line."""
    return dockerfile.replace("\\\n", " ")


def _manifest() -> ContainerManifest:
    return ContainerManifest(
        base_image="ubuntu:24.04",
        binary_name="demo",
        runtime_packages=["ca-certificates", "librdkafka1"],
        entrypoint=["demo"],
    )


_RENDERED = {
    "python-template": lambda: render_python_template(),
    "rust-contract": lambda: compose_contract_dockerfile(_manifest(), "1.90"),
}


@pytest.mark.parametrize("name", sorted(_RENDERED))
def test_every_apt_call_passes_retry_option(name: str) -> None:
    text = _joined(_RENDERED[name]())
    calls = _APT_CALL.findall(text)
    assert calls, f"{name}: expected apt-get calls in the rendered Dockerfile"
    for opts, verb in calls:
        assert "Acquire::Retries=5" in opts, (
            f"{name}: apt-get {verb} has no retry option"
        )


@pytest.mark.parametrize("name", sorted(_RENDERED))
def test_every_apt_update_is_rerun_on_failure(name: str) -> None:
    text = _joined(_RENDERED[name]())
    updates = [verb for _, verb in _APT_CALL.findall(text) if verb == "update"]
    assert updates, f"{name}: expected an apt-get update"
    assert len(_UPDATE_LOOP.findall(text)) == len(updates)


def _run_update_loop(tmp_path: Path, fails: int) -> tuple[int, int, list[str]]:
    """Run the rendered update loop against a stub apt-get that fails ``fails`` times.

    Returns the exit code, how many times apt-get ran, and each sleep argument.
    """
    from hyperi_ci.apt_retry import apt_update_sh

    calls = tmp_path / "calls"
    sleeps = tmp_path / "sleeps"
    apt_get = tmp_path / "apt-get"
    apt_get.write_text(
        f'#!/bin/sh\necho x >> "{calls}"\n[ "$(wc -l < "{calls}")" -gt {fails} ]\n',
        encoding="utf-8",
    )
    sleep = tmp_path / "sleep"
    sleep.write_text(f'#!/bin/sh\necho "$1" >> "{sleeps}"\n', encoding="utf-8")
    apt_get.chmod(0o755)
    sleep.chmod(0o755)

    result = subprocess.run(
        ["sh", "-c", apt_update_sh()],
        env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    ran = len(calls.read_text(encoding="utf-8").splitlines()) if calls.exists() else 0
    slept = sleeps.read_text(encoding="utf-8").split() if sleeps.exists() else []
    return result.returncode, ran, slept


def test_update_loop_recovers_after_a_failed_fetch(tmp_path: Path) -> None:
    rc, ran, slept = _run_update_loop(tmp_path, fails=1)
    assert (rc, ran, slept) == (0, 2, ["15"])


def test_update_loop_succeeds_first_time_without_sleeping(tmp_path: Path) -> None:
    rc, ran, slept = _run_update_loop(tmp_path, fails=0)
    assert (rc, ran, slept) == (0, 1, [])


def test_update_loop_gives_up_with_apt_exit_code(tmp_path: Path) -> None:
    rc, ran, slept = _run_update_loop(tmp_path, fails=99)
    assert (rc, ran, slept) == (100, 4, ["15", "30", "45"])


def test_native_deps_reruns_update_with_retry_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hyperi_ci import native_deps

    calls: list[list[str]] = []
    slept: list[float] = []

    def fake_run_cmd(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        updates_so_far = sum(1 for c in calls if c[-1] == "update")
        rc = 100 if cmd[-1] == "update" and updates_so_far == 1 else 0
        return subprocess.CompletedProcess(cmd, rc)

    monkeypatch.setattr(native_deps, "_sudo_prefix", lambda: [])
    monkeypatch.setattr(native_deps, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(native_deps.time, "sleep", slept.append)

    assert native_deps._apt_install(["curl"]) == 0
    assert [c[3] for c in calls] == ["update", "update", "install"]
    assert all(c[:3] == ["apt-get", "-o", "Acquire::Retries=5"] for c in calls)
    assert slept == [15]
