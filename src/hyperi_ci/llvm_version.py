# Project:   HyperI CI
# File:      src/hyperi_ci/llvm_version.py
# Purpose:   Resolve the one LLVM major CI installs and links with
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""The designated LLVM major, the version every Rust job installs and uses.

A runner's unversioned ``clang`` and ``ld.lld`` point at whichever major the
image picked, while CI uses the designated major regardless. Highest wins:

1. ``HYPERCI_LLVM_VERSION`` in the environment.
2. ``build.rust.llvm_version`` in the project's ``.hyperi-ci.yaml``.
3. ``runtimes.llvm`` in the shipped ``versions.yaml``.
"""

import os
from pathlib import Path
from typing import NamedTuple

from hyperi_ci.common import warn
from hyperi_ci.project_config import read_project_config
from hyperi_ci.versions import runtime_version

LLVM_VERSION_ENV = "HYPERCI_LLVM_VERSION"
LLVM_VERSION_KEY = "build.rust.llvm_version"


class LLVMVersionError(ValueError):
    """A designated LLVM version that is not a positive integer major."""


class DesignatedLLVM(NamedTuple):
    """The designated LLVM major and the source that set it.

    Attributes:
        major: The LLVM major version, such as 23.
        source: Where it came from, for log lines: the env var name, the
            project config file name, or ``versions.yaml``.

    """

    major: int
    source: str


def _as_major(value: object, source: str) -> int:
    """Return ``value`` as an LLVM major, or raise naming where it came from.

    Raises:
        LLVMVersionError: ``value`` is not a positive whole number.

    """
    # YAML reads `true` as a bool, which is an int and would pass as major 1.
    text = "" if isinstance(value, bool) else str(value).strip()
    if not (text.isascii() and text.isdigit()) or int(text) == 0:
        raise LLVMVersionError(
            f"{source} must be an LLVM major version such as 23, not {value!r}"
        )
    return int(text)


def _configured(project_dir: Path) -> tuple[object, str]:
    """Return ``build.rust.llvm_version`` from the project config and its file name.

    The value is None when the key is absent or the file cannot be read.
    """
    project = read_project_config(project_dir)
    if project.data is None:
        warn(f"{project.unreadable} -- {LLVM_VERSION_KEY} not read from it")
        return None, project.name
    node: object = project.data
    for part in LLVM_VERSION_KEY.split("."):
        if not isinstance(node, dict) or part not in node:
            return None, project.name
        node = node[part]
    return node, project.name


def default_llvm_major() -> int:
    """Return the hyperi-ci default LLVM major, ``runtimes.llvm`` in versions.yaml.

    The runner image bakes this major alone, whatever a project or the
    environment designates.

    Raises:
        LLVMVersionError: ``runtimes.llvm`` is missing, or is not a positive
            whole number.

    """
    source = "runtimes.llvm in versions.yaml"
    try:
        value = runtime_version("llvm")
    except KeyError as exc:
        raise LLVMVersionError(f"{source} is missing: {exc}") from exc
    return _as_major(value, source)


def designated_llvm_version(project_dir: Path | None = None) -> DesignatedLLVM:
    """Return the LLVM major CI installs and puts first on PATH.

    Args:
        project_dir: Project root holding ``.hyperi-ci.yaml``. Defaults to cwd.

    Returns:
        The major from the environment, else the project config, else
        ``versions.yaml``, with the source that set it.

    Raises:
        LLVMVersionError: The winning source holds something other than a
            positive whole number, or it falls through to a versions.yaml
            with no ``runtimes.llvm``.

    """
    from_env = os.environ.get(LLVM_VERSION_ENV, "").strip()
    if from_env:
        return DesignatedLLVM(_as_major(from_env, LLVM_VERSION_ENV), LLVM_VERSION_ENV)

    configured, config_name = _configured(project_dir or Path.cwd())
    if configured is not None and str(configured).strip():
        major = _as_major(configured, f"{LLVM_VERSION_KEY} in {config_name}")
        return DesignatedLLVM(major, config_name)

    return DesignatedLLVM(default_llvm_major(), "versions.yaml")
