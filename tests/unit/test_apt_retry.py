# Project:   HyperI CI
# File:      tests/unit/test_apt_retry.py
# Purpose:   Every apt-get call hyperi-ci runs survives a mirror mid-sync
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Every apt-get call hyperi-ci runs retries.

A mirror caught mid-sync serves an index whose size disagrees with its Release
file. apt 2.8.3 fails that fetch once and exits 100 without honouring
``Acquire::Retries``, so the update itself has to be re-run after a pause. A
failure confined to a source hyperi-ci installs nothing from is not retried.
"""

import subprocess

import pytest

_ARCHIVE = "http://azure.archive.ubuntu.com/ubuntu/dists/resolute/InRelease"
_LLVM_REPO = "https://apt.llvm.org/resolute/"
_LLVM = "https://apt.llvm.org/resolute/dists/llvm-toolchain-resolute-23/InRelease"
_MICROSOFT = "https://packages.microsoft.com/ubuntu/26.04/prod/dists/resolute/InRelease"


def _failed(*urls: str) -> str:
    lines = [
        f"E: Failed to fetch {url}  403  Forbidden [IP: 1.2.3.4 443]" for url in urls
    ]
    return "\n".join(["Hit:1 http://azure.archive.ubuntu.com/ubuntu resolute", *lines])


@pytest.mark.parametrize(
    ("updates", "repo_urls", "expected_updates", "slept", "logged"),
    [
        pytest.param([(0, "")], (), 1, [], [], id="clean"),
        pytest.param(
            [(100, _failed(_ARCHIVE)), (0, "")], (), 2, [15], [], id="archive-mid-sync"
        ),
        pytest.param(
            [(100, "E: Could not get lock /var/lib/apt/lists/lock")],
            (),
            4,
            [15, 30, 45],
            ["warning"],
            id="no-fetch-line",
        ),
        pytest.param(
            [(100, _failed(_MICROSOFT))], (), 1, [], ["info"], id="unused-source"
        ),
        pytest.param(
            [(100, _failed(_MICROSOFT, _LLVM))],
            (_LLVM_REPO,),
            4,
            [15, 30, 45],
            ["warning"],
            id="used-repo-and-unused",
        ),
        pytest.param(
            [(100, _failed(_LLVM))], (), 1, [], ["info"], id="repo-not-needed-now"
        ),
        pytest.param(
            [(100, _failed(_MICROSOFT, _ARCHIVE)), (0, "")],
            (),
            2,
            [15],
            [],
            id="archive-and-unused",
        ),
    ],
)
def test_apt_update_retries_only_sources_in_use(
    monkeypatch: pytest.MonkeyPatch,
    updates: list[tuple[int, str]],
    repo_urls: tuple[str, ...],
    expected_updates: int,
    slept: list[float],
    logged: list[str],
) -> None:
    from hyperi_ci import native_deps

    update_calls: list[list[str]] = []
    install_calls: list[list[str]] = []
    sleeps: list[float] = []
    logs: list[str] = []

    def fake_stream_cmd(cmd: list[str], **_: object) -> tuple[int, str]:
        update_calls.append(cmd)
        return updates[min(len(update_calls), len(updates)) - 1]

    def fake_run_cmd(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        install_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(native_deps, "sudo_prefix", lambda: [])
    monkeypatch.setattr(native_deps, "stream_cmd", fake_stream_cmd)
    monkeypatch.setattr(native_deps, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(native_deps, "_ensure_ubuntu_archive", lambda _apt: None)
    monkeypatch.setattr(native_deps.time, "sleep", sleeps.append)
    monkeypatch.setattr(native_deps, "info", lambda _m: logs.append("info"))
    monkeypatch.setattr(
        native_deps.logger, "warning", lambda *_a, **_k: logs.append("warning")
    )

    assert native_deps.apt_install(["curl"], repo_urls) == 0
    assert len(update_calls) == expected_updates
    assert all(
        c == ["apt-get", "-o", "Acquire::Retries=5", "update"] for c in update_calls
    )
    assert [c[3] for c in install_calls] == ["install"]
    assert sleeps == slept
    assert logs == logged
