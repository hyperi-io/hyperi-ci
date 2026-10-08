# Project:   HyperI CI
# File:      src/hyperi_ci/quality/node_tools.py
# Purpose:   Install the pinned npm tree behind markdownlint and the mermaid check
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Install the pinned npm packages the docs checks run on.

Direct versions come from ``versions.yaml`` and the rest of the tree from the
shipped ``config/node-tools/package-lock.json``. ``npm ci`` checks every
tarball's sha512 and refuses a ``package.json`` that disagrees with the lock,
so a version bumped without a relock fails the install.

Installs on CI only, into the user cache keyed by lock and versions. Node is
not installed: the GitHub-hosted and ARC images carry ``node`` and ``npm``.
"""

import functools
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from hyperi_ci.common import info, is_ci, run_cmd, warn
from hyperi_ci.upgrade import CACHE_DIR
from hyperi_ci.versions import tool_version

NODE_TOOLS = ("linkedom", "markdownlint-cli2", "mermaid")

# Transitive packages forced to their versions.yaml pin through npm `overrides`,
# for a parent that pins an exact version carrying an advisory.
NODE_OVERRIDES = ("lodash-es", "smol-toml", "katex")

LOCKFILE = Path(__file__).parent.parent / "config" / "node-tools" / "package-lock.json"

# Matches the `name` in the lockfile, which npm writes from this manifest.
MANIFEST_NAME = "hyperi-ci-node-tools"

# Written last, so a directory without it is a partial install to ignore.
_DONE = ".hyperi-ci-installed"

# npm ci over a cold cache fetches ~220 tarballs; this bounds a hung registry.
_NPM_TIMEOUT = 600


def manifest() -> dict[str, object]:
    """Return the ``package.json`` for the pinned set, versions from the SSOT."""
    return {
        "name": MANIFEST_NAME,
        "private": True,
        "dependencies": {name: tool_version(name) for name in NODE_TOOLS},
        "overrides": {name: tool_version(name) for name in NODE_OVERRIDES},
    }


def _install_dir() -> Path:
    """Return the cache directory for this exact lock and manifest pair."""
    key = hashlib.sha256(LOCKFILE.read_bytes())
    key.update(json.dumps(manifest(), sort_keys=True).encode("utf-8"))
    return CACHE_DIR / "node-tools" / key.hexdigest()[:16]


@functools.cache
def install() -> Path | None:
    """Return the ``node_modules`` holding the pinned set, installing it on CI.

    Cached, so the docs checks share one ``npm ci`` per run. Returns None off
    CI, without node or npm, or when ``npm ci`` fails.
    """
    if not is_ci():
        return None
    npm = shutil.which("npm")
    if npm is None or shutil.which("node") is None:
        warn("  node-tools: node or npm is not on PATH - cannot install the docs tools")
        return None

    final = _install_dir()
    if (final / _DONE).is_file():
        return final / "node_modules"

    final.parent.mkdir(parents=True, exist_ok=True)
    # Stage then rename, so a concurrent run never sees a half-built tree.
    staging = Path(tempfile.mkdtemp(prefix=f"{final.name}.", dir=final.parent))
    try:
        (staging / "package.json").write_text(
            json.dumps(manifest(), indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        shutil.copyfile(LOCKFILE, staging / "package-lock.json")
        pinned = ", ".join(f"{name}@{tool_version(name)}" for name in NODE_TOOLS)
        info(f"  Installing {pinned} (npm ci)...")
        try:
            result = run_cmd(
                [npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
                check=False,
                capture=True,
                cwd=staging,
                timeout=_NPM_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            warn(f"  node-tools: npm ci could not complete ({exc})")
            return None
        if result.returncode != 0:
            detail = " | ".join(
                (result.stderr or result.stdout).strip().splitlines()[-5:]
            )
            warn(f"  node-tools: npm ci exited {result.returncode}: {detail}")
            return None
        (staging / _DONE).write_text("", encoding="utf-8", newline="\n")
        try:
            staging.rename(final)
        except OSError:
            # Another run renamed an identical tree into place first.
            pass
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return final / "node_modules" if (final / _DONE).is_file() else None


def executable(name: str) -> str | None:
    """Return the installed ``node_modules/.bin`` entry for ``name``, or None."""
    modules = install()
    if modules is None:
        return None
    path = modules / ".bin" / name
    return str(path) if path.is_file() else None
