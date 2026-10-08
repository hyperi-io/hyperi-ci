# Project:   HyperI CI
# File:      src/hyperi_ci/versions.py
# Purpose:   The single reader for the pinned-version SSOT
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Read pinned third-party versions and digests from the shipped SSOT.

``config/versions.yaml`` ships inside the package, so runtime reads a pin from
it rather than from a constant copied into source. A version literal in a
module is a bug and ``tests/unit/test_versions.py`` fails on one.

- It pins THIRD-PARTY things only. hyperi-ci's own version comes from git tags
  via :mod:`hyperi_ci.version_source`, because the build back-end would
  otherwise read a file inside the package it is building.
- It imports stdlib and ``yaml`` only, so nothing can import-cycle through it.

The only copies are files GitHub parses before our code runs (a workflow's
``uses:`` line, a composite action's ``default:``), which
``scripts/update-versions.py`` rewrites from this SSOT.
"""

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

VERSIONS_FILE = Path(__file__).resolve().parent / "config" / "versions.yaml"


@lru_cache(maxsize=1)
def _data() -> dict[str, Any]:
    """Parse the SSOT once per process."""
    with open(VERSIONS_FILE, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _tool(name: str) -> dict[str, Any]:
    tools = _data().get("tools") or {}
    spec = tools.get(name)
    if not isinstance(spec, dict):
        raise KeyError(
            f"{name!r} is not in {VERSIONS_FILE.name} under `tools:` - "
            "add the pin there rather than hardcoding it"
        )
    return spec


def tool_version(name: str) -> str:
    """Return the pinned version string for ``name``, verbatim.

    Verbatim because the download URL is built from it (cargo-deny's tags carry
    no leading ``v``).

    Raises:
        KeyError: No such tool, or no version on it.

    """
    version = _tool(name).get("version")
    if not isinstance(version, str) or not version:
        raise KeyError(f"`tools.{name}.version` is missing from {VERSIONS_FILE.name}")
    return version


def tool_sha256(name: str, arch: str) -> str:
    """Return the pinned sha256 of ``name``'s release asset for ``arch``.

    The digest covers the RAW download (the binary, or the ``.tar.gz`` before
    extraction).

    Args:
        name: Tool key under ``tools:``.
        arch: Key under that tool's ``sha256:``, spelled as the tool's own
            asset names spell it (``x64`` for gitleaks, ``x86_64`` for alint).

    Raises:
        KeyError: No digest for that tool/arch (fail closed).

    """
    digests = _tool(name).get("sha256")
    if not isinstance(digests, dict) or arch not in digests:
        raise KeyError(
            f"`tools.{name}.sha256.{arch}` is missing from {VERSIONS_FILE.name}"
        )
    return str(digests[arch])


def runtime_version(name: str) -> str:
    """Return a language runtime pin (``python``, ``node``, ``rust``).

    An entry is a bare value, or a mapping that also lists the files mirroring
    it.

    Raises:
        KeyError: No such runtime, or no version on it.

    """
    runtimes = _data().get("runtimes") or {}
    if name not in runtimes:
        raise KeyError(f"`runtimes.{name}` is missing from {VERSIONS_FILE.name}")
    spec = runtimes[name]
    version = spec.get("version") if isinstance(spec, dict) else spec
    if not version:
        raise KeyError(
            f"`runtimes.{name}.version` is missing from {VERSIONS_FILE.name}"
        )
    return str(version)
