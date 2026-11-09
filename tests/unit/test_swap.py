"""Tests for swap on free runners."""

import sys
from pathlib import Path

import pytest

from hyperi_ci import swap
from hyperi_ci.memory import GIB

_HEADER = "Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"
_LINUX_ONLY = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="swap is only added on Linux"
)
_FREE = {"HYPERCI_RUNNER_MODE": "free"}


def _proc_swaps(tmp_path: Path, *rows: str) -> Path:
    path = tmp_path / "swaps"
    path.write_text(_HEADER + "".join(f"{r}\n" for r in rows), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("rows", "total"),
    [
        ((), 0),
        (("/mnt/swapfile file 4194300 0 -2",), 4194300 * 1024),
        (
            ("/mnt/swapfile file 4194300 0 -2", "/tmp/x.swap file 1048576 0 -3"),
            (4194300 + 1048576) * 1024,
        ),
        (("garbage",), 0),
    ],
)
def test_swap_total_sums_every_area(tmp_path, rows, total):
    assert swap.swap_total_bytes(_proc_swaps(tmp_path, *rows)) == total


def test_swap_total_is_zero_when_the_table_is_unreadable(tmp_path):
    assert swap.swap_total_bytes(tmp_path / "missing") == 0


def test_swap_active_matches_the_whole_path(tmp_path):
    table = _proc_swaps(tmp_path, "/runner/_temp/hyperi-ci.swap file 1024 0 -2")
    assert swap.swap_active(Path("/runner/_temp/hyperi-ci.swap"), table)
    assert not swap.swap_active(Path("/runner/_temp/hyperi-ci.swa"), table)


@pytest.mark.parametrize(
    ("ram_gib", "free_gib", "target_gib"),
    [
        (15.6, 144, 31.2),  # free x64 or arm64 runner: twice the RAM
        (7.75, 144, 15.5),  # free runner on a private repo
        (64, 500, 32),  # never past 32 GiB
        (15.6, 40, 10),  # never past a quarter of the free disk
        (15.6, 0, 0),
    ],
)
def test_target_is_twice_ram_capped_by_32_gib_and_a_quarter_of_the_disk(
    ram_gib, free_gib, target_gib
):
    got = swap.target_total_bytes(int(ram_gib * GIB), int(free_gib * GIB))
    assert got == pytest.approx(target_gib * GIB, abs=1)


def test_swap_commands_create_lock_down_format_and_enable(monkeypatch):
    monkeypatch.setattr(swap, "sudo_prefix", lambda: ["sudo"])
    got = swap.swap_commands(Path("/t/hyperi-ci.swap"), 27)
    assert got == [
        ["sudo", "fallocate", "-l", "27G", "/t/hyperi-ci.swap"],
        ["sudo", "chmod", "600", "/t/hyperi-ci.swap"],
        ["sudo", "mkswap", "/t/hyperi-ci.swap"],
        ["sudo", "swapon", "/t/hyperi-ci.swap"],
    ]


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"HYPERCI_RUNNER_MODE": "", "RUNNER_TEMP": "/t"},
        {"HYPERCI_RUNNER_MODE": "arc", "RUNNER_TEMP": "/t"},
        {"HYPERCI_RUNNER_MODE": "free"},
    ],
)
def test_nothing_is_touched_off_a_free_runner(tmp_path, environ):
    table = _proc_swaps(tmp_path, "/mnt/swapfile file 4194300 0 -2")
    assert swap.ensure_free_runner_swap(16 * GIB, environ, proc_swaps=table) is None


def test_a_free_runner_off_linux_is_not_touched(tmp_path, monkeypatch):
    monkeypatch.setattr(swap, "is_linux", lambda: False)
    environ = {**_FREE, "RUNNER_TEMP": str(tmp_path)}
    table = _proc_swaps(tmp_path)
    assert swap.ensure_free_runner_swap(16 * GIB, environ, proc_swaps=table) is None


@_LINUX_ONLY
def test_an_active_swap_file_is_not_added_twice(tmp_path):
    path = tmp_path / swap.SWAP_FILE
    table = _proc_swaps(tmp_path, f"{path} file 1048576 0 -2")
    environ = {"HYPERCI_RUNNER_MODE": "free", "RUNNER_TEMP": str(tmp_path)}
    got = swap.ensure_free_runner_swap(16 * GIB, environ, proc_swaps=table)
    assert got == GIB
    assert not path.exists()


@_LINUX_ONLY
def test_a_failed_command_warns_and_builds_without_extra_swap(tmp_path, monkeypatch):
    # No root here, so the real fallocate or sudo fails, which is the case under test.
    monkeypatch.setattr(swap, "sudo_prefix", lambda: [])
    monkeypatch.setattr(
        swap, "swap_commands", lambda path, gib: [["false-not-a-binary", str(path)]]
    )
    table = _proc_swaps(tmp_path, "/mnt/swapfile file 4194304 0 -2")
    environ = {"HYPERCI_RUNNER_MODE": "free", "RUNNER_TEMP": str(tmp_path)}
    got = swap.ensure_free_runner_swap(15 * GIB, environ, proc_swaps=table)
    assert got == 4 * GIB


@_LINUX_ONLY
def test_enough_swap_already_adds_nothing(tmp_path):
    table = _proc_swaps(tmp_path, "/mnt/swapfile file 33554432 0 -2")
    environ = {"HYPERCI_RUNNER_MODE": "free", "RUNNER_TEMP": str(tmp_path)}
    got = swap.ensure_free_runner_swap(15 * GIB, environ, proc_swaps=table)
    assert got == 32 * GIB
    assert not (tmp_path / swap.SWAP_FILE).exists()
