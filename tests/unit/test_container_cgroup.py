# Project:   HyperI CI
# File:      tests/unit/test_container_cgroup.py
# Purpose:   Tests for the buildx builder cgroup-parent probe
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for :mod:`hyperi_ci.container.cgroup` (issue #284).

The probe asks the runner's dockerd where it puts job containers, so the
buildx builder can go there too. Docker is replaced by a recording runner that
returns real ``CompletedProcess`` objects, one per expected call.
"""

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.container import stage as stage_module
from hyperi_ci.container.cgroup import (
    PROBE_IMAGE,
    CgroupProbe,
    builder_cgroup_parents,
    parent_from_proc_cgroup,
    probe_cgroup_parent,
)
from hyperi_ci.container.stage import run

POD_SCOPE = (
    "/kubepods.slice/kubepods-burstable.slice/"
    "kubepods-burstable-pod1d2c.slice/cri-containerd-9f3e.scope"
)
CONTAINER_ID = "4b1c" * 16


class _Docker:
    """Answers docker calls from a table keyed by the subcommand."""

    def __init__(self, answers: dict[str, tuple[int, str, str]]) -> None:
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        rc, out, err = self.answers[cmd[1]]
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr=err)

    def ran(self, subcommand: str) -> bool:
        return any(c[1] == subcommand for c in self.calls)


def _raising(exc: Exception) -> Callable[[list[str]], subprocess.CompletedProcess[str]]:
    def runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        raise exc

    return runner


class TestParentFromProcCgroup:
    def test_pod_scoped_dind_gives_the_jobs_cgroup(self) -> None:
        text = f"0::{POD_SCOPE}/jobs/{CONTAINER_ID}\n"
        assert parent_from_proc_cgroup(text) == f"{POD_SCOPE}/jobs"

    def test_systemd_scope_is_refused(self) -> None:
        assert (
            parent_from_proc_cgroup(f"0::/system.slice/docker-{CONTAINER_ID}.scope\n")
            is None
        )

    def test_plain_dockerd_default_is_refused(self) -> None:
        # A cgroupfs dockerd with no --cgroup-parent: node root, nothing to contain it.
        assert parent_from_proc_cgroup(f"0::/docker/{CONTAINER_ID}\n") is None

    def test_a_private_namespace_root_is_refused(self) -> None:
        # Without --cgroupns=host the container sees itself at "/".
        assert parent_from_proc_cgroup("0::/\n") is None

    def test_a_container_directly_in_the_pod_scope_is_refused(self) -> None:
        # Parent would be the scope itself, which holds dockerd's own cgroup.
        assert parent_from_proc_cgroup(f"0::{POD_SCOPE}/{CONTAINER_ID}\n") is None

    def test_cgroup_v1_has_no_unified_line(self) -> None:
        text = (
            f"12:memory:/docker/{CONTAINER_ID}\n11:cpu,cpuacct:/docker/{CONTAINER_ID}\n"
        )
        assert parent_from_proc_cgroup(text) is None

    def test_empty_output(self) -> None:
        assert parent_from_proc_cgroup("") is None

    def test_a_kubepods_path_with_no_scope_is_refused(self) -> None:
        assert (
            parent_from_proc_cgroup(f"0::/kubepods/besteffort/jobs/{CONTAINER_ID}\n")
            is None
        )


class TestProbeCgroupParent:
    def test_pod_scoped_dind(self) -> None:
        docker = _Docker(
            {
                "info": (0, "cgroupfs\n", ""),
                "run": (0, f"0::{POD_SCOPE}/jobs/{CONTAINER_ID}\n", ""),
            }
        )
        result = probe_cgroup_parent(docker)
        assert result == CgroupProbe(parent=f"{POD_SCOPE}/jobs", reason=result.reason)
        run_cmd = next(c for c in docker.calls if c[1] == "run")
        assert "--cgroupns=host" in run_cmd
        assert "--rm" in run_cmd
        assert PROBE_IMAGE in run_cmd
        assert run_cmd[-1] == "/proc/self/cgroup"

    def test_the_probe_image_is_the_one_buildx_pulls(self) -> None:
        # setup-buildx then reuses this pull rather than making a second one.
        assert PROBE_IMAGE == "moby/buildkit:buildx-stable-1"

    def test_systemd_driver_skips_the_container(self) -> None:
        # GitHub-hosted runners: buildx ignores cgroup-parent off cgroupfs, so
        # no image is pulled at all.
        docker = _Docker({"info": (0, "systemd\n", "")})
        result = probe_cgroup_parent(docker)
        assert result.parent is None
        assert "systemd" in result.reason
        assert not docker.ran("run")

    def test_docker_info_failure(self) -> None:
        docker = _Docker({"info": (1, "", "Cannot connect to the Docker daemon\n")})
        result = probe_cgroup_parent(docker)
        assert result.parent is None
        assert "Cannot connect" in result.reason

    def test_probe_container_failure(self) -> None:
        docker = _Docker(
            {
                "info": (0, "cgroupfs\n", ""),
                "run": (125, "", "Unable to find image\npull access denied\n"),
            }
        )
        result = probe_cgroup_parent(docker)
        assert result.parent is None
        assert "pull access denied" in result.reason

    def test_a_cgroupfs_daemon_at_node_root_is_refused(self) -> None:
        docker = _Docker(
            {
                "info": (0, "cgroupfs\n", ""),
                "run": (0, f"0::/docker/{CONTAINER_ID}\n", ""),
            }
        )
        result = probe_cgroup_parent(docker)
        assert result.parent is None
        assert f"/docker/{CONTAINER_ID}" in result.reason

    @pytest.mark.parametrize(
        "exc",
        [
            FileNotFoundError("docker"),
            subprocess.TimeoutExpired(["docker", "run"], 300),
        ],
    )
    def test_a_runner_exception_is_a_reason_not_a_crash(self, exc: Exception) -> None:
        result = probe_cgroup_parent(_raising(exc))
        assert result.parent is None
        assert result.reason


class TestBuilderCgroupParents:
    def test_reads_each_builder(self) -> None:
        name = "buildx_buildkit_builder-0a1b0"
        docker = _Docker(
            {
                "ps": (0, f"{name}\n", ""),
                "inspect": (0, f"{POD_SCOPE}/jobs\n", ""),
            }
        )
        assert builder_cgroup_parents(docker) == [(name, f"{POD_SCOPE}/jobs")]

    def test_no_builder(self) -> None:
        docker = _Docker({"ps": (0, "", "")})
        assert builder_cgroup_parents(docker) == []
        assert not docker.ran("inspect")

    def test_docker_failure_is_empty_not_a_crash(self) -> None:
        assert builder_cgroup_parents(_raising(FileNotFoundError("docker"))) == []


class TestCgroupProbeEnv:
    """The workflow sets the probe variable AND resolve-only, so a CLI release
    without the probe only resolves; this one must probe instead."""

    def _run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, found: CgroupProbe
    ) -> str:
        monkeypatch.chdir(tmp_path)
        out = tmp_path / "gh_output"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out))
        monkeypatch.setenv("HYPERCI_CONTAINER_CGROUP_PROBE", "1")
        monkeypatch.setenv("HYPERCI_CONTAINER_RESOLVE_ONLY", "1")
        monkeypatch.setattr(stage_module, "probe_cgroup_parent", lambda: found)

        def _no_build(**_):  # pragma: no cover - must not run
            raise AssertionError("the probe must not build")

        monkeypatch.setattr(stage_module, "_build_custom", _no_build)
        assert run(CIConfig(language="rust", _raw={}), language="rust") == 0
        return out.read_text(encoding="utf-8")

    def test_writes_the_parent_and_not_the_build_decision(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        written = self._run(
            monkeypatch, tmp_path, CgroupProbe(parent=f"{POD_SCOPE}/jobs", reason="ok")
        )
        assert written == f"cgroup-parent={POD_SCOPE}/jobs\n"

    def test_no_parent_writes_an_empty_value(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        written = self._run(
            monkeypatch, tmp_path, CgroupProbe(parent=None, reason="systemd driver")
        )
        assert written == "cgroup-parent=\n"


class TestBuilderCgroupLog:
    """The line a fixture rehearsal reads to see where the builder landed."""

    def _lines(
        self, monkeypatch: pytest.MonkeyPatch, builders: list[tuple[str, str]]
    ) -> list[str]:
        lines: list[str] = []
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setattr(stage_module, "builder_cgroup_parents", lambda: builders)
        monkeypatch.setattr(stage_module, "info", lines.append)
        stage_module._log_builder_cgroups()
        return lines

    def test_names_the_builder_and_its_parent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        name = "buildx_buildkit_builder-0a1b0"
        lines = self._lines(monkeypatch, [(name, f"{POD_SCOPE}/jobs")])
        assert lines == [f"Buildx builder cgroup: {name} under {POD_SCOPE}/jobs"]

    def test_says_when_there_is_no_builder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lines = self._lines(monkeypatch, [])
        assert lines == ["Buildx builder cgroup: no docker-container builder found"]

    def test_silent_outside_github_actions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

        def _no_docker():  # pragma: no cover - must not run
            raise AssertionError("no docker call off GitHub Actions")

        monkeypatch.setattr(stage_module, "builder_cgroup_parents", _no_docker)
        stage_module._log_builder_cgroups()
