# Project:   HyperI CI
# File:      src/hyperi_ci/versions.py
# Purpose:   The single reader for the pinned-version SSOT
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Read pinned third-party versions and digests from the shipped SSOT.

``config/versions.yaml`` ships INSIDE the package, next to ``defaults.yaml``
and ``org.yaml``, so runtime resolves a pin by reading the SSOT rather than a
constant copied into source. A copy is a thing that goes stale; there is no
copy.

Every caller goes through here. A version literal in a module is a bug -
``tests/unit/test_versions.py`` fails on one.

Two rules keep this from becoming self-referential:

- It pins THIRD-PARTY things only. hyperi-ci's own version is derived from git
  tags by :mod:`hyperi_ci.version_source`; putting it here would have the build
  back-end read a file inside the package it is building.
- It imports stdlib and ``yaml`` only, so nothing in the package can import-cycle
  through it.

The one thing that still needs the value COPIED is a file GitHub itself parses
before any of our code runs: a workflow's ``uses:`` line, or a composite
action's ``default:``. ``scripts/update-versions.py`` rewrites those from this
same SSOT, and that is the full extent of what it writes.
"""

from __future__ import annotations

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

    Verbatim matters: cargo-deny's tags carry no leading ``v`` and the download
    URL is built from this string, so normalising it would 404.

    Raises:
        KeyError: No such tool, or no version on it. Both mean the SSOT and the
            code disagree, which is the drift this module exists to remove -
            so it fails rather than guessing a default.

    """
    version = _tool(name).get("version")
    if not isinstance(version, str) or not version:
        raise KeyError(f"`tools.{name}.version` is missing from {VERSIONS_FILE.name}")
    return version


def tool_sha256(name: str, arch: str) -> str:
    """Return the pinned sha256 of ``name``'s release asset for ``arch``.

    The digest covers the RAW download - the binary itself, or the ``.tar.gz``
    before extraction - so one value verifies exactly the bytes off the wire.

    Args:
        name: Tool key under ``tools:``.
        arch: Key under that tool's ``sha256:``, spelled as the tool's own
            asset names spell it (``x64`` for gitleaks, ``x86_64`` for alint).

    Raises:
        KeyError: No digest for that tool/arch. Fail closed: an install with no
            digest to check is the gap, not an acceptable fallback.

    """
    digests = _tool(name).get("sha256")
    if not isinstance(digests, dict) or arch not in digests:
        raise KeyError(
            f"`tools.{name}.sha256.{arch}` is missing from {VERSIONS_FILE.name}"
        )
    return str(digests[arch])


def tool_names() -> list[str]:
    """Every tool key in the SSOT, sorted."""
    return sorted((_data().get("tools") or {}).keys())


def action_names() -> list[str]:
    """Every action key in the SSOT, sorted."""
    return sorted((_data().get("actions") or {}).keys())


def action_ref(name: str) -> str:
    """Return ``<sha> # <version>`` for a pinned action, or the bare tag.

    The shape a ``uses:`` line wants, so a caller scaffolding a workflow emits
    the same pin the rewriter would.

    Raises:
        KeyError: No such action.

    """
    actions = _data().get("actions") or {}
    spec = actions.get(name)
    if spec is None:
        raise KeyError(f"`actions.{name}` is missing from {VERSIONS_FILE.name}")
    if isinstance(spec, str):
        return spec
    sha, version = spec.get("sha"), spec.get("version")
    return f"{sha} # {version}" if sha else str(version)


def runtime_version(name: str) -> str:
    """Return a language runtime pin (``python``, ``node``, ``rust``).

    Raises:
        KeyError: No such runtime.

    """
    runtimes = _data().get("runtimes") or {}
    if name not in runtimes:
        raise KeyError(f"`runtimes.{name}` is missing from {VERSIONS_FILE.name}")
    return str(runtimes[name])
