# Project:   HyperI CI
# File:      src/hyperi_ci/container/cgroup.py
# Purpose:   Find the runner pod's job cgroup for the buildx builder
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Find the cgroup a runner's dockerd gives job containers, for the buildx builder.

On a cgroupfs dockerd, buildx's docker-container driver puts its builder under
the absolute ``/docker/buildx``, at the node root and outside a runner pod's
memory limit. hyperi-infra's pod-scoped dind (``k8s/arc-runner-dind-pod-scoped.sh``)
starts dockerd with ``--cgroup-parent=<its own pod container scope>/jobs``. That
path differs per pod, so the probe reads it back from a throwaway container and
the workflow passes it to setup-buildx as ``cgroup-parent``.
"""

import posixpath
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from hyperi_ci.common import run_cmd

# buildx's default docker-container driver image, so setup-buildx reuses this pull.
PROBE_IMAGE = "moby/buildkit:buildx-stable-1"

# Room for a cold pull of PROBE_IMAGE.
_PROBE_TIMEOUT = 300
_INSPECT_TIMEOUT = 30

# One cgroup below a pod container scope: inside the pod's memory limit, and
# not the scope itself, which holds dockerd.
_POD_SCOPED_PARENT = re.compile(r"^/kubepods\S*\.scope/[^/\s]+$")

type DockerRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class CgroupProbe:
    """What the probe found.

    Attributes:
        parent: The cgroup to put the builder under, or None to leave buildx's
            default.
        reason: Why, for the job log.
    """

    parent: str | None
    reason: str


def _docker(timeout: float) -> DockerRunner:
    def runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        return run_cmd(cmd, check=False, capture=True, timeout=timeout)

    return runner


def _last_line(text: str | None) -> str:
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return lines[-1].strip() if lines else "no output"


def parent_from_proc_cgroup(text: str) -> str | None:
    """Return the parent cgroup of a probe container, if it is pod-scoped.

    Args:
        text: ``/proc/self/cgroup`` as read inside a container started with
            ``--cgroupns=host``.

    Returns:
        The directory above the container's own cgroup v2 path when that sits
        one level below a ``/kubepods...scope``, else None.
    """
    for line in text.splitlines():
        if line.startswith("0::"):
            parent = posixpath.dirname(line[3:].strip())
            return parent if _POD_SCOPED_PARENT.match(parent) else None
    return None


def probe_cgroup_parent(runner: DockerRunner | None = None) -> CgroupProbe:
    """Ask the runner's dockerd which cgroup it gives job containers.

    Mirrors buildx, which honours ``cgroup-parent`` only on the cgroupfs
    driver, so any other driver returns before an image is pulled.

    Args:
        runner: Runs one docker argv. Defaults to the local docker CLI.

    Returns:
        The parent to pass to buildx, or None with the reason it was not set.
    """
    run = runner or _docker(_PROBE_TIMEOUT)
    try:
        info = run(["docker", "info", "--format", "{{.CgroupDriver}}"])
        if info.returncode != 0:
            return CgroupProbe(None, f"docker info failed: {_last_line(info.stderr)}")
        driver = info.stdout.strip()
        if driver != "cgroupfs":
            return CgroupProbe(
                None,
                f"dockerd uses the {driver or 'unknown'} cgroup driver, and buildx "
                "takes a cgroup parent only on cgroupfs",
            )
        probe = run(
            [
                "docker",
                "run",
                "--rm",
                "--network=none",
                "--cgroupns=host",
                "--entrypoint",
                "cat",
                PROBE_IMAGE,
                "/proc/self/cgroup",
            ]
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return CgroupProbe(None, f"docker could not run: {exc}")
    if probe.returncode != 0:
        return CgroupProbe(None, f"probe container failed: {_last_line(probe.stderr)}")
    parent = parent_from_proc_cgroup(probe.stdout)
    if parent is None:
        return CgroupProbe(
            None,
            f"probe container's cgroup is {_last_line(probe.stdout)!r}, not one "
            "level below a runner pod's container scope",
        )
    return CgroupProbe(parent, "dockerd nests job containers under the runner pod")


def builder_cgroup_parents(runner: DockerRunner | None = None) -> list[tuple[str, str]]:
    """List each buildx docker-container builder and the cgroup parent it got.

    Args:
        runner: Runs one docker argv. Defaults to the local docker CLI.

    Returns:
        ``(container name, HostConfig.CgroupParent)`` pairs. Empty when docker
        is absent, fails, or runs no such builder.
    """
    run = runner or _docker(_INSPECT_TIMEOUT)
    try:
        listed = run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                "name=^buildx_buildkit_",
                "--format",
                "{{.Names}}",
            ]
        )
        if listed.returncode != 0:
            return []
        found: list[tuple[str, str]] = []
        for name in listed.stdout.split():
            inspected = run(
                ["docker", "inspect", "--format", "{{.HostConfig.CgroupParent}}", name]
            )
            if inspected.returncode == 0:
                found.append((name, inspected.stdout.strip()))
        return found
    except (OSError, subprocess.SubprocessError):
        return []
