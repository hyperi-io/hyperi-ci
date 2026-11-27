# Project:   HyperI CI
# File:      src/hyperi_ci/release/bench_memory.py
# Purpose:   Measure a Rust app's memory profile for its deployment contract
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Measure the ``memory`` block of a Rust app's deployment contract.

An app that lists scalo's ``bench-memory`` feature runs ``<app> bench-memory``
on its release binary, and ``generate-artefacts --memory-profile`` puts the
profile in the contract. Only a linux amd64 Build leg of a run that ships does
this, as the figure varies from run to run and the release tail needs every
leg's contract to match. Where docker runs cgroup v2 the bench runs in the
contract's ``base_image``, so scalo reads the container's own
``memory.current``, the working set kubelet evicts on. Elsewhere it reads the
process's resident memory.
"""

import json
import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from hyperi_ci.common import info, run_cmd, warn
from hyperi_ci.deployment.detect import enables_bench_memory

BENCH_ARCH = "amd64"
PROFILE_FILE = "memory-profile.json"
# docker run's own exit codes: the daemon failed, or the entrypoint could not
# be run or found in the image.
_DOCKER_FAILED = frozenset({125, 126, 127})
_DOCKER_INFO_SECONDS = 60


class BenchError(Exception):
    """The bench ran and failed, or wrote no profile."""


def wanted(root: Path) -> bool:
    """Return True on a Build that ships an app with the ``bench-memory`` feature."""
    if os.environ.get("HYPERCI_CHANNEL", "") != "release":
        return False
    return enables_bench_memory(root)


def docker_argv(
    command: Sequence[str], image: str, root: Path, scratch: Path, output: Path
) -> list[str]:
    """Return the ``docker run`` that benches ``command`` inside ``image``.

    The repo and scratch directory mount at their own paths, so the binary, its
    config and the profile keep the paths the bench was given.
    """
    return [
        "docker",
        "run",
        "--rm",
        "--cgroupns",
        "private",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--env",
        f"HOME={scratch}",
        "--volume",
        f"{root}:{root}",
        "--volume",
        f"{scratch}:{scratch}",
        "--workdir",
        str(root),
        image,
        *command,
        "bench-memory",
        "--output",
        str(output),
    ]


def _docker_cgroup_v2() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = run_cmd(
            ["docker", "info", "--format", "{{.CgroupVersion}}"],
            check=False,
            capture=True,
            timeout=_DOCKER_INFO_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0 and result.stdout.strip() == "2"


def _base_image(contract: bytes) -> str:
    try:
        image = json.loads(contract).get("base_image", "")
    except (ValueError, AttributeError):
        return ""
    return image if isinstance(image, str) else ""


def measure(command: Sequence[str], contract: bytes, root: Path, scratch: Path) -> Path:
    """Run ``<app> bench-memory`` and return the profile it wrote.

    Args:
        command: The release binary, as ``producer_command`` gives it.
        contract: The contract the app emits without a profile, for its
            ``base_image``.
        root: The repo the binary reads its config from.
        scratch: A directory the profile is written to.

    Returns:
        The profile file.

    Raises:
        BenchError: The bench exited non-zero or wrote no profile.

    """
    output = scratch / PROFILE_FILE
    output.unlink(missing_ok=True)
    direct = [*command, "bench-memory", "--output", str(output)]
    image = _base_image(contract)
    argv = direct
    if not image:
        warn("bench-memory: the contract names no base_image, so it runs on the runner")
    elif not _docker_cgroup_v2():
        warn("bench-memory: no docker on cgroup v2 here, so it reads resident memory")
    else:
        argv = docker_argv(command, image, root, scratch, output)
    info(f"Measuring memory: {' '.join(argv)}")
    rc = run_cmd(argv, check=False, cwd=root).returncode
    if argv is not direct and rc in _DOCKER_FAILED:
        warn(f"bench-memory: docker exited {rc} before the bench ran, so it runs here")
        rc = run_cmd(direct, check=False, cwd=root).returncode
    if rc != 0:
        raise BenchError(f"{' '.join(direct)} exited {rc}")
    if not output.is_file():
        raise BenchError(f"{' '.join(direct)} wrote no {PROFILE_FILE}")
    return output
