# Project:   HyperI CI
# File:      tests/unit/test_relock_node_tools.py
# Purpose:   Tests for the node-tools relock and its --auto-update bump
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/relock-node-tools.py, with npm and the registry faked."""

import importlib.util
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from hyperi_ci import versions

_SPEC = importlib.util.spec_from_file_location(
    "relock_node_tools",
    Path(__file__).resolve().parents[2] / "scripts" / "relock-node-tools.py",
)
assert _SPEC is not None and _SPEC.loader is not None  # always resolves for a real file
relock = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(relock)

# Midnight, as main() passes it: npm's date-only --before resolves to the same.
TODAY = datetime(2026, 5, 28, tzinfo=UTC)


@pytest.fixture
def ssot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    """A copy of the shipped versions.yaml, read and written in its place."""
    copy = tmp_path / "versions.yaml"
    copy.write_text(versions.VERSIONS_FILE.read_text(encoding="utf-8"), "utf-8")
    monkeypatch.setattr(versions, "VERSIONS_FILE", copy)
    versions._data.cache_clear()
    yield copy
    versions._data.cache_clear()


class _Npm:
    """Stand-in npm: records each call and the manifest it was handed."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.rc = 0

    def run(self, cmd: list[str], *, check: bool, cwd: Path) -> object:
        manifest = json.loads((Path(cwd) / "package.json").read_text("utf-8"))
        self.calls.append({"cmd": cmd, "manifest": manifest})
        (Path(cwd) / "package-lock.json").write_text(
            json.dumps({"from": manifest}), "utf-8"
        )
        return subprocess.CompletedProcess(cmd, self.rc, "", "")


@pytest.fixture
def npm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Npm:
    fake = _Npm()
    monkeypatch.setattr(relock.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(relock, "LOCKFILE", tmp_path / "lock" / "package-lock.json")
    monkeypatch.setattr(relock, "run_cmd", fake.run)
    return fake


def _registry(
    monkeypatch: pytest.MonkeyPatch, newer: dict[str, list[dict] | None]
) -> None:
    """Serve each lock-pinned package its current pin, plus any ``newer`` ones.

    A package mapped to None is unreachable.
    """
    tools = yaml.safe_load(versions.VERSIONS_FILE.read_text("utf-8"))["tools"]
    table: dict[str, list[dict] | None] = {}
    for spec in tools.values():
        if not (isinstance(spec, dict) and spec.get("lockfile")):
            continue
        pinned = {
            "tag_name": str(spec["version"]),
            "published_at": "2026-01-01T00:00:00Z",
        }
        releases = newer.get(str(spec["npm"]), [])
        table[str(spec["npm"])] = None if releases is None else [pinned, *releases]
    monkeypatch.setattr(
        relock.update_versions, "_npm_releases", lambda package: table.get(package)
    )


class TestHelp:
    def test_help_prints_help_and_relocks_nothing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(relock.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(
            relock, "run_cmd", lambda *_a, **_k: pytest.fail("--help ran npm")
        )
        monkeypatch.setattr("sys.argv", ["relock-node-tools.py", "--help"])
        with pytest.raises(SystemExit) as exit_info:
            relock.main()
        assert exit_info.value.code == 0
        assert "--auto-update" in capsys.readouterr().out


class TestAutoUpdate:
    def test_bumps_past_the_cooldown_then_relocks(
        self, ssot: Path, npm: _Npm, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 0.18.14 is 7.5 days old at midnight; 0.18.15 is inside the soak.
        _registry(
            monkeypatch,
            {
                "linkedom": [
                    {"tag_name": "0.18.14", "published_at": "2026-05-20T12:00:00Z"},
                    {"tag_name": "0.18.15", "published_at": "2026-05-21T12:00:00Z"},
                ]
            },
        )
        assert relock._auto_update(TODAY) == 0

        written = yaml.safe_load(ssot.read_text("utf-8"))["tools"]
        assert written["linkedom"]["version"] == "0.18.14"
        (call,) = npm.calls
        assert call["manifest"]["dependencies"]["linkedom"] == "0.18.14"
        assert "--before=2026-05-21" in call["cmd"]
        lock = json.loads(relock.LOCKFILE.read_text("utf-8"))
        assert lock["from"]["dependencies"]["linkedom"] == "0.18.14"

    def test_a_failed_relock_restores_the_pins(
        self, ssot: Path, npm: _Npm, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = ssot.read_text("utf-8")
        npm.rc = 1
        _registry(
            monkeypatch,
            {
                "mermaid": [
                    {"tag_name": "99.0.0", "published_at": "2026-05-01T00:00:00Z"}
                ]
            },
        )
        assert relock._auto_update(TODAY) == 1
        assert ssot.read_text("utf-8") == before
        assert versions.tool_version("mermaid") != "99.0.0"
        assert not relock.LOCKFILE.exists()

    def test_nothing_newer_relocks_nothing(
        self, ssot: Path, npm: _Npm, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = ssot.read_text("utf-8")
        _registry(monkeypatch, {})
        assert relock._auto_update(TODAY) == 0
        assert npm.calls == []
        assert ssot.read_text("utf-8") == before

    def test_an_unreachable_registry_is_not_current(
        self, ssot: Path, npm: _Npm, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _registry(monkeypatch, {"katex": None})
        assert relock._auto_update(TODAY) == 1
        assert npm.calls == []
