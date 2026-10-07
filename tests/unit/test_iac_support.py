# Project:   HyperI CI
# File:      tests/unit/test_iac_support.py
# Purpose:   Tests for the pieces lint-iac adds to shared modules
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for IaC discovery, the startup-probe check, the memory cap, the IaC
tool installs, kubeconform -strict and Checkov's no-report guard."""

import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from hyperi_ci import common, native_tools
from hyperi_ci.config import CIConfig
from hyperi_ci.quality import checkov, install, kube_linter, kubeconform
from hyperi_ci.quality.targets import (
    discover_kustomizations,
    discover_manifests,
    discover_tofu_dirs,
    git_ignored_dirs,
)
from hyperi_ci.versions import tool_version


def _touch(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _alive(pid: int) -> bool:
    """Report whether ``pid`` is a running (not zombie) process."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    return stat.rsplit(")", 1)[-1].split()[0] != "Z"


def _not_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL", "BUILDKITE"):
        monkeypatch.delenv(name, raising=False)


class TestIacDiscovery:
    def test_hidden_dirs_hold_no_iac_target(self, tmp_path: Path) -> None:
        _touch(tmp_path / "infra" / "main.tf")
        _touch(tmp_path / ".claude" / "worktrees" / "a" / "infra" / "main.tf")
        _touch(tmp_path / "infra" / ".terraform" / "modules" / "m" / "main.tf")
        assert discover_tofu_dirs(tmp_path) == [tmp_path / "infra"]

    def test_kustomization_dirs_found_and_kept_out_of_manifests(
        self, tmp_path: Path
    ) -> None:
        k = tmp_path / "overlays" / "prod"
        _touch(k / "kustomization.yaml", "patches:\n  - path: patch.yaml\n")
        _touch(
            k / "patch.yaml",
            "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: x\n",
        )
        assert discover_kustomizations(tmp_path) == [k]
        assert discover_manifests(tmp_path) == []

    def test_git_ignored_dirs_are_returned_repo_relative(self, tmp_path: Path) -> None:
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        _touch(tmp_path / ".gitignore", "agent-copies/\n")
        _touch(tmp_path / "agent-copies" / "x" / "main.tf")
        _touch(tmp_path / "infra" / "main.tf")
        ignored = git_ignored_dirs(tmp_path)
        assert ignored == ["agent-copies"]
        assert discover_tofu_dirs(tmp_path, exclude_dirs=ignored) == [
            tmp_path / "infra"
        ]

    def test_outside_git_nothing_is_ignored(self, tmp_path: Path) -> None:
        assert git_ignored_dirs(tmp_path) == []

    def test_values_fragment_is_not_a_manifest(self, tmp_path: Path) -> None:
        _touch(
            tmp_path / "k8s" / "arc-runner-base.yaml",
            "githubConfigUrl: https://example.invalid\ntemplate:\n  spec: {}\n",
        )
        assert discover_manifests(tmp_path) == []


_DEPLOYMENT = """\
apiVersion: apps/v1
kind: Deployment
metadata: {name: d}
spec:
  selector: {matchLabels: {a: b}}
  template:
    metadata: {labels: {a: b}}
    spec:
      containers:
        - name: bad
          image: x:1
          livenessProbe: {tcpSocket: {port: 1}}
        - name: good
          image: x:1
          livenessProbe: {tcpSocket: {port: 1}}
          startupProbe: {tcpSocket: {port: 1}}
"""


class TestStartupProbeCheck:
    def test_merges_into_the_repo_config(self, tmp_path: Path) -> None:
        _touch(
            tmp_path / ".kube-linter.yaml",
            "checks:\n  exclude: [unset-cpu-requirements]\n",
        )
        out = kube_linter.merged_config(tmp_path, tmp_path / "merged.yaml")
        doc = yaml.safe_load(out.read_text(encoding="utf-8"))
        assert doc["checks"]["exclude"] == ["unset-cpu-requirements"]
        assert [c["name"] for c in doc["customChecks"]] == [
            kube_linter.STARTUP_PROBE_CHECK
        ]

    def test_not_added_twice(self, tmp_path: Path) -> None:
        _touch(
            tmp_path / ".kube-linter.yml",
            yaml.safe_dump({"customChecks": [kube_linter.startup_probe_check()]}),
        )
        out = kube_linter.merged_config(tmp_path, tmp_path / "merged.yaml")
        doc = yaml.safe_load(out.read_text(encoding="utf-8"))
        assert len(doc["customChecks"]) == 1

    @pytest.mark.skipif(shutil.which("kube-linter") is None, reason="no kube-linter")
    def test_real_kube_linter_flags_only_the_container_missing_one(
        self, tmp_path: Path
    ) -> None:
        manifest = _touch(tmp_path / "d.yaml", _DEPLOYMENT)
        config = kube_linter.merged_config(tmp_path, tmp_path / "kl.yaml")
        result = subprocess.run(
            ["kube-linter", "lint", "--config", str(config), str(manifest)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        hits = [
            line
            for line in result.stdout.splitlines()
            if kube_linter.STARTUP_PROBE_CHECK in line
        ]
        assert len(hits) == 1
        assert '"bad"' in hits[0]


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_AS is Linux only")
class TestRunCmdMemoryLimit:
    def test_the_child_runs_under_the_cap(self) -> None:
        probe = (
            "import os, resource; print(resource.getrlimit(resource.RLIMIT_AS)[0], "
            "os.environ['MALLOC_ARENA_MAX'])"
        )
        result = common.run_cmd(
            [sys.executable, "-c", probe], capture=True, memory_limit_bytes=1024**3
        )
        assert result.stdout.split() == [str(1024**3), "2"]

    def test_a_timeout_kills_the_grandchildren_too(self, tmp_path: Path) -> None:
        pidfile = tmp_path / "grandchild.pid"
        script = (
            "import subprocess, sys, time; "
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            f"open({str(pidfile)!r}, 'w').write(str(p.pid)); time.sleep(60)"
        )
        with pytest.raises(subprocess.TimeoutExpired):
            common.run_cmd(
                [sys.executable, "-c", script], capture=True, timeout=2, own_group=True
            )
        pid = int(pidfile.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _alive(pid):
            time.sleep(0.05)
        assert not _alive(pid)

    def test_no_cap_by_default(self) -> None:
        probe = "import resource; print(resource.getrlimit(resource.RLIMIT_AS)[0])"
        result = common.run_cmd([sys.executable, "-c", probe], capture=True)
        assert int(result.stdout) != 1024**3


def _targz(member: str) -> bytes:
    data = b"#!/bin/sh\necho v0\n"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        entry = tarfile.TarInfo(name=member)
        entry.size = len(data)
        archive.addfile(entry, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def linux_amd64(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Pretend to be Linux x86_64 and serve one tarball through the digest gate."""
    state: dict = {"urls": [], "payload": b""}
    monkeypatch.setattr(native_tools.sys, "platform", "linux")
    monkeypatch.setattr(native_tools.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        native_tools,
        "tool_sha256",
        lambda name, arch: hashlib.sha256(state["payload"]).hexdigest(),
    )

    def _download(name: str, url: str) -> bytes:
        state["urls"].append(url)
        return state["payload"]

    monkeypatch.setattr(install, "download_artefact", _download)
    return state


class TestIacToolInstall:
    @pytest.mark.parametrize(
        ("name", "url_tail"),
        [
            ("tofu", "/{v}/tofu_{bare}_linux_amd64.tar.gz"),
            ("kustomize", "/kustomize%2F{v}/kustomize_{v}_linux_amd64.tar.gz"),
        ],
    )
    def test_fetches_the_pinned_asset(
        self, name: str, url_tail: str, tmp_path: Path, linux_amd64: dict
    ) -> None:
        linux_amd64["payload"] = _targz(name)
        version = tool_version(name)
        bin_dir = native_tools.install_tool(name, tmp_path)
        assert bin_dir is not None
        assert (bin_dir / name).is_file()
        bare = version.removeprefix("v")
        assert linux_amd64["urls"][0].endswith(url_tail.format(v=version, bare=bare))

    def test_ci_binary_prefers_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(native_tools.shutil, "which", lambda n: f"/usr/bin/{n}")
        assert native_tools.ci_binary("tofu") == "/usr/bin/tofu"

    def test_ci_binary_installs_nothing_off_ci(
        self, monkeypatch: pytest.MonkeyPatch, linux_amd64: dict
    ) -> None:
        _not_ci(monkeypatch)
        monkeypatch.setattr(native_tools.shutil, "which", lambda n: None)
        assert native_tools.ci_binary("tofu") is None
        assert linux_amd64["urls"] == []

    def test_ci_binary_installs_the_pin_in_ci(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linux_amd64: dict
    ) -> None:
        linux_amd64["payload"] = _targz("tofu")
        monkeypatch.setenv("CI", "true")
        monkeypatch.setattr(native_tools.shutil, "which", lambda n: None)
        real_install = native_tools.install_tool
        monkeypatch.setattr(
            native_tools, "install_tool", lambda name: real_install(name, tmp_path)
        )
        exe = native_tools.ci_binary("tofu")
        assert exe is not None
        assert Path(exe).is_file()


class TestKubeconformStrict:
    def _capture(self, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
        calls: list[list[str]] = []
        monkeypatch.setattr(kubeconform, "_install_kubeconform", lambda: "kubeconform")

        def _run(cmd: list[str], **_: object) -> SimpleNamespace:
            calls.append(cmd)
            return SimpleNamespace(stdout='{"resources": []}', returncode=0)

        monkeypatch.setattr(kubeconform, "run_cmd", _run)
        return calls

    @pytest.mark.parametrize(
        ("quality", "strict"),
        [({}, True), ({"kubeconform": {"strict": False}}, False)],
    )
    def test_strict_by_default_and_cached(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        quality: dict,
        strict: bool,
    ) -> None:
        calls = self._capture(monkeypatch)
        kubeconform.run([tmp_path / "a.yaml"], CIConfig(_raw={"quality": quality}))
        assert ("-strict" in calls[0]) is strict
        assert "-cache" in calls[0]

    def test_schema_cache_is_per_pin_and_drops_week_old_entries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(kubeconform, "CACHE_DIR", tmp_path)
        cache = tmp_path / "kubeconform-schemas" / tool_version("kubeconform")
        cache.mkdir(parents=True)
        old, fresh = cache / "old.json", cache / "fresh.json"
        old.write_text("{}", encoding="utf-8")
        fresh.write_text("{}", encoding="utf-8")
        week_ago = time.time() - 8 * 86400
        os.utime(old, (week_ago, week_ago))
        assert kubeconform._schema_cache() == cache
        assert not old.exists()
        assert fresh.exists()

    def test_a_timeout_fails_a_blocking_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(kubeconform, "_install_kubeconform", lambda: "kubeconform")

        def _hang(cmd: list[str], **kw: Any) -> SimpleNamespace:
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])

        monkeypatch.setattr(kubeconform, "run_cmd", _hang)
        assert kubeconform.run([tmp_path / "a.yaml"], CIConfig(_raw={}), timeout=1) == 1


class TestCheckovNoReport:
    def test_a_crash_with_no_report_is_not_a_clean_scan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(checkov, "_base_cmd", lambda: ["checkov"])
        monkeypatch.setattr(
            checkov,
            "run_cmd",
            lambda cmd, **kw: SimpleNamespace(
                returncode=1, stdout="", stderr="MemoryError"
            ),
        )
        assert checkov.run(tmp_path, CIConfig(_raw={})) == 0
        blocking = CIConfig(_raw={"quality": {"checkov": "blocking"}})
        assert checkov.run(tmp_path, blocking) == 1

    def test_the_cap_reaches_run_cmd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict = {}
        monkeypatch.setattr(checkov, "_base_cmd", lambda: ["checkov"])

        def _run(cmd: list[str], **kw: object) -> SimpleNamespace:
            seen.update(kw)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(checkov, "run_cmd", _run)
        checkov.run(tmp_path, CIConfig(_raw={}), timeout=3, memory_limit_bytes=99)
        assert seen["memory_limit_bytes"] == 99
        assert seen["timeout"] == 3
