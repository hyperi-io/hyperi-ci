# Project:   HyperI CI
# File:      tests/unit/test_memory_scope.py
# Purpose:   Tests for running a local check inside a memory-capped scope
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for :mod:`hyperi_ci.memory_scope`."""

from pathlib import Path

import pytest

from hyperi_ci import memory_scope
from hyperi_ci.memory import GIB
from hyperi_ci.memory_scope import SCOPE_ENV, scope_cap_bytes, scope_command

SYSTEMD_RUN = "/usr/bin/systemd-run"


@pytest.mark.parametrize(
    ("available", "cap"),
    [(30 * GIB, 28 * GIB), (3 * GIB, GIB), (2 * GIB, GIB), (0, GIB)],
    ids=["roomy", "at-floor", "headroom-only", "none-free"],
)
def test_the_cap_leaves_headroom_and_never_drops_below_the_floor(
    available: int, cap: int
) -> None:
    assert scope_cap_bytes(available) == cap


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    """A local Linux host with systemd-run and a user bus."""
    monkeypatch.setattr(memory_scope, "is_ci", lambda: False)
    monkeypatch.setattr(memory_scope, "is_linux", lambda: True)
    monkeypatch.setattr(memory_scope.shutil, "which", lambda _name: SYSTEMD_RUN)
    (tmp_path / "bus").touch()
    return {"XDG_RUNTIME_DIR": str(tmp_path)}


def test_a_local_check_reruns_itself_in_a_capped_scope(
    host: dict[str, str],
) -> None:
    argv = ["/usr/bin/python3", "/home/u/.local/bin/hyperi-ci", "check", "--quick"]
    assert scope_command(argv, 8 * GIB, host) == [
        SYSTEMD_RUN,
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        "-p",
        f"MemoryMax={8 * GIB}",
        f"--setenv={SCOPE_ENV}=1",
        "--",
        *argv,
    ]


@pytest.mark.parametrize(
    "case",
    [
        "ci",
        "macos",
        "already-scoped",
        "opted-out",
        "no-runtime-dir",
        "no-bus",
        "no-systemd-run",
    ],
)
def test_no_scope_where_one_cannot_or_should_not_run(
    host: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case: str,
) -> None:
    environ = dict(host)
    match case:
        case "ci":
            monkeypatch.setattr(memory_scope, "is_ci", lambda: True)
        case "macos":
            monkeypatch.setattr(memory_scope, "is_linux", lambda: False)
        case "already-scoped":
            environ[SCOPE_ENV] = "1"
        case "opted-out":
            environ[SCOPE_ENV] = "off"
        case "no-runtime-dir":
            del environ["XDG_RUNTIME_DIR"]
        case "no-bus":
            (tmp_path / "bus").unlink()
        case "no-systemd-run":
            monkeypatch.setattr(memory_scope.shutil, "which", lambda _name: None)
    assert scope_command(["hyperi-ci", "check"], 8 * GIB, environ) is None
