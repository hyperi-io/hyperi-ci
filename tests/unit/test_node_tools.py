# Project:   HyperI CI
# File:      tests/unit/test_node_tools.py
# Purpose:   Cover the pinned npm install behind markdownlint and mermaid-parse
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for :mod:`hyperi_ci.quality.node_tools`.

markdownlint-cli2 and the mermaid grammar check had no install path on any
runner, so both warned on every consumer run and checked nothing (issue #230).
The pins, the lockfile and the install mechanics are tested offline with a fake
npm; the real install runs only where it can, which on CI is every run.
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from hyperi_ci import versions
from hyperi_ci.common import run_cmd
from hyperi_ci.config import CIConfig
from hyperi_ci.quality import markdownlint, mermaid_parse, node_tools


@pytest.fixture(autouse=True)
def _fresh_install() -> object:
    """install() is cached per process; a faked result must not leak out."""
    node_tools.install.cache_clear()
    yield
    # A test that replaced install() outright may still have it patched here.
    clear = getattr(node_tools.install, "cache_clear", None)
    if clear is not None:
        clear()


def _lock() -> dict:
    return json.loads(node_tools.LOCKFILE.read_text(encoding="utf-8"))


class TestPins:
    """versions.yaml owns the versions; the lock must agree with it."""

    @pytest.mark.parametrize("tool", node_tools.NODE_TOOLS)
    def test_each_tool_is_pinned_exactly_in_the_ssot(self, tool: str) -> None:
        assert re.fullmatch(r"\d+\.\d+\.\d+", versions.tool_version(tool)), tool

    @pytest.mark.parametrize("tool", node_tools.NODE_TOOLS)
    def test_each_ssot_entry_names_the_npm_package_and_the_lock(
        self, tool: str
    ) -> None:
        data = yaml.safe_load(versions.VERSIONS_FILE.read_text(encoding="utf-8"))
        spec = data["tools"][tool]
        assert spec["npm"] == tool
        root = Path(__file__).resolve().parents[2]
        assert root / spec["lockfile"] == node_tools.LOCKFILE

    def test_the_manifest_is_rendered_from_the_ssot(self) -> None:
        deps = node_tools.manifest()["dependencies"]
        assert deps == {t: versions.tool_version(t) for t in node_tools.NODE_TOOLS}

    def test_the_lock_root_matches_the_manifest(self) -> None:
        """A bump in versions.yaml without a relock fails here, not on a runner."""
        root = _lock()["packages"][""]
        assert root["name"] == node_tools.MANIFEST_NAME
        assert root["dependencies"] == node_tools.manifest()["dependencies"], (
            "versions.yaml and the lock disagree - run "
            "`uv run scripts/relock-node-tools.py`"
        )

    @pytest.mark.parametrize("tool", node_tools.NODE_TOOLS)
    def test_the_lock_resolves_each_tool_to_its_pin(self, tool: str) -> None:
        entry = _lock()["packages"][f"node_modules/{tool}"]
        assert entry["version"] == versions.tool_version(tool)

    def test_every_locked_package_carries_a_sha512_from_the_public_registry(
        self,
    ) -> None:
        packages = {k: v for k, v in _lock()["packages"].items() if k}
        assert packages
        for name, entry in packages.items():
            assert str(entry.get("integrity", "")).startswith("sha512-"), name
            assert str(entry.get("resolved", "")).startswith(
                "https://registry.npmjs.org/"
            ), name

    def test_no_locked_package_runs_an_install_script(self) -> None:
        """`--ignore-scripts` would silently break a package that needed one."""
        scripted = [
            k for k, v in _lock()["packages"].items() if v.get("hasInstallScript")
        ]
        assert scripted == []

    def test_the_lock_ships_inside_the_package(self) -> None:
        root = Path(__file__).resolve().parents[2]
        assert node_tools.LOCKFILE.is_relative_to(root / "src" / "hyperi_ci")


def _fake_npm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, rc: int = 0
) -> list[dict]:
    """Stand in for node + npm; record each `npm ci` and build a fake tree."""
    calls: list[dict] = []
    monkeypatch.setattr(node_tools, "is_ci", lambda: True)
    monkeypatch.setattr(node_tools, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(node_tools.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(cmd, *, check, capture, cwd, timeout):
        cwd = Path(cwd)
        calls.append(
            {
                "cmd": cmd,
                "manifest": json.loads((cwd / "package.json").read_text("utf-8")),
                "lock": (cwd / "package-lock.json").read_bytes(),
            }
        )
        if rc == 0:
            bin_dir = cwd / "node_modules" / ".bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "markdownlint-cli2").write_text("#!/bin/sh\n", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, rc, "", "npm ERR! boom\n")

    monkeypatch.setattr(node_tools, "run_cmd", fake_run)
    return calls


class TestInstall:
    """The install mechanics, with npm faked."""

    def test_off_ci_nothing_is_installed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls = _fake_npm(monkeypatch, tmp_path)
        monkeypatch.setattr(node_tools, "is_ci", lambda: False)
        assert node_tools.install() is None
        assert calls == []

    def test_no_npm_on_path_is_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls = _fake_npm(monkeypatch, tmp_path)
        monkeypatch.setattr(node_tools.shutil, "which", lambda _n: None)
        assert node_tools.install() is None
        assert calls == []

    def test_npm_ci_runs_against_the_shipped_lock(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls = _fake_npm(monkeypatch, tmp_path)
        modules = node_tools.install()
        assert modules is not None and modules.is_dir()
        assert modules.is_relative_to(tmp_path / "cache" / "node-tools")
        (call,) = calls
        assert call["cmd"][1:3] == ["ci", "--ignore-scripts"]
        assert call["manifest"] == node_tools.manifest()
        assert call["lock"] == node_tools.LOCKFILE.read_bytes()

    def test_one_install_serves_the_whole_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls = _fake_npm(monkeypatch, tmp_path)
        node_tools.install()
        node_tools.install()
        assert len(calls) == 1

    def test_a_finished_tree_is_reused_by_the_next_process(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls = _fake_npm(monkeypatch, tmp_path)
        first = node_tools.install()
        node_tools.install.cache_clear()
        assert node_tools.install() == first
        assert len(calls) == 1

    def test_a_failed_npm_ci_leaves_nothing_behind(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _fake_npm(monkeypatch, tmp_path, rc=1)
        assert node_tools.install() is None
        leftovers = list((tmp_path / "cache" / "node-tools").iterdir())
        assert leftovers == []

    def test_a_timeout_is_none_not_a_traceback(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _fake_npm(monkeypatch, tmp_path)

        def hung(cmd, **_kw):
            raise subprocess.TimeoutExpired(cmd, 1)

        monkeypatch.setattr(node_tools, "run_cmd", hung)
        assert node_tools.install() is None

    def test_executable_points_into_the_installed_bin(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _fake_npm(monkeypatch, tmp_path)
        exe = node_tools.executable("markdownlint-cli2")
        assert exe is not None and exe.endswith("node_modules/.bin/markdownlint-cli2")
        assert node_tools.executable("not-installed") is None


class TestCallers:
    """Both checks reach the install when nothing else supplies the tool."""

    def test_markdownlint_falls_back_to_the_installed_binary(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        asked: list[str] = []
        monkeypatch.setattr(markdownlint.shutil, "which", lambda _n: None)
        monkeypatch.setattr(
            node_tools, "executable", lambda name: asked.append(name) or None
        )
        doc = tmp_path / "README.md"
        doc.write_text("# x\n", encoding="utf-8")
        config = CIConfig(_raw={"quality": {"markdownlint": "warn"}})
        assert markdownlint.run([doc], config, root=tmp_path) == 0
        assert asked == ["markdownlint-cli2"]

    def test_mermaid_uses_the_install_after_the_repos_own(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        installed = tmp_path / "cache" / "node_modules"
        monkeypatch.setattr(node_tools, "install", lambda: installed)
        env = mermaid_parse._node_env(tmp_path)
        dirs = env["HYPERCI_NODE_MODULES"].split(os.pathsep)
        assert dirs == [str(tmp_path / "node_modules"), str(installed)]

    def test_mermaid_skips_the_install_when_the_repo_supplies_both(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        for name in mermaid_parse.NODE_PACKAGES:
            pkg = tmp_path / "node_modules" / name
            pkg.mkdir(parents=True)
            (pkg / "package.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            node_tools, "install", lambda: pytest.fail("installed needlessly")
        )
        assert mermaid_parse._module_dirs(tmp_path) == [tmp_path / "node_modules"]


class TestRealInstall:
    """The real npm tree, run for real. Runs on CI; skips where npm cannot."""

    @pytest.fixture
    def modules(self) -> Path:
        modules = node_tools.install()
        if modules is None:
            pytest.skip("no pinned node-tools install here (off CI, or no npm)")
        return modules

    def test_markdownlint_reports_a_real_violation(
        self, modules: Path, tmp_path: Path
    ) -> None:
        doc = tmp_path / "bad.md"
        doc.write_text("# Title\n### Skipped a level\n", encoding="utf-8")
        exe = node_tools.executable("markdownlint-cli2")
        assert exe is not None
        result = run_cmd(
            [exe, "--config", str(markdownlint.DEFAULT_CONFIG), ":bad.md"],
            check=False,
            capture=True,
            cwd=tmp_path,
        )
        rules = {f.rule for f in markdownlint.parse(result.stdout + result.stderr)}
        assert "MD001/heading-increment" in rules, result.stderr

    def test_the_mermaid_grammar_runs_from_the_install(
        self, modules: Path, tmp_path: Path
    ) -> None:
        good = mermaid_parse.Block(tmp_path / "d.md", 1, "graph TD\n  A --> B", True)
        bad = mermaid_parse.Block(tmp_path / "d.md", 5, "graph TD\n  A --> ", True)
        errors, skipped = mermaid_parse._run_parser([good, bad], tmp_path)
        assert skipped is None
        assert list(errors) == [1]
