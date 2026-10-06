# Project:   HyperI CI
# File:      tests/unit/test_apt_retry.py
# Purpose:   Every apt-get call hyperi-ci runs survives a mirror mid-sync
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Every apt-get call hyperi-ci runs retries.

A mirror caught mid-sync serves an index whose size disagrees with its Release
file. apt 2.8.3 fails that fetch once and exits 100 without honouring
``Acquire::Retries``, so the update itself has to be re-run after a pause.
"""

import subprocess

import pytest


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
