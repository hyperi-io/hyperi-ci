# Project:   HyperI CI
# File:      src/hyperi_ci/quality/install.py
# Purpose:   Install a pinned release binary on Linux CI (shared by the linters)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Install a pinned static release binary on a Linux CI runner.

hadolint, kubeconform and kube-linter are all small single static binaries
fetched the same way: skip if already on PATH, skip off-CI / non-Linux (the
caller warn-skips locally), else download the pinned release and drop it in
``/usr/local/bin``. One helper rather than the same 25 lines copied per tool.

Some ship as a raw binary (hadolint), others inside a ``.tar.gz``
(kubeconform, kube-linter); ``tar_member`` selects the entry to extract.
"""

from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from hyperi_ci.common import error, info, is_ci, warn


def fetch_verified(
    name: str, url: str, expected_sha256: str | None = None
) -> bytes | None:
    """Download ``url`` and return the bytes, or None if they are not the pinned ones.

    Split from :func:`install_ci_binary` because placement differs per caller:
    alint is exec'd by absolute path from a scratch dir, with no sudo and no
    PATH mutation, so it runs on an ARC pod. The integrity gate is the shared
    part.

    A pinned URL is not integrity on its own - a release asset can be deleted
    and re-uploaded under the same tag, and these bytes are chmod+exec'd on
    every consumer's CI.

    ``expected_sha256`` of None warns and returns the bytes unverified, for a
    tool not yet pinned.
    """
    # -f: fail (empty body, non-zero exit) on an HTTP error instead of saving a
    # 404/captive-portal HTML page and later chmod+exec'ing it as "the tool".
    # --connect-timeout/--max-time give a HARD ceiling so a stalled mirror
    # cannot hang the runner unbounded (the repo's no-unbounded-wait doctrine);
    # the outer timeout= is a belt-and-braces backstop.
    try:
        dl = subprocess.run(
            ["curl", "-fsSL", "--connect-timeout", "10", "--max-time", "180", url],
            capture_output=True,
            timeout=200,
        )
    except (OSError, subprocess.TimeoutExpired):
        error(f"  Failed to download {name} (network error / timeout)")
        return None
    if dl.returncode != 0 or not dl.stdout:
        error(f"  Failed to download {name} (curl exit {dl.returncode})")
        return None

    # Hash the RAW download (the binary itself, or the .tar.gz for a tar
    # member) BEFORE extracting or exec'ing anything, so one pinned hash covers
    # the exact bytes that arrived off the wire.
    got = hashlib.sha256(dl.stdout).hexdigest()
    if expected_sha256 is None:
        warn(
            f"  {name}: installing WITHOUT a pinned SHA256 - integrity unverified "
            f"(downloaded {got})"
        )
    elif got.lower() != expected_sha256.strip().lower():
        error(
            f"  {name}: SHA256 mismatch - refusing to install "
            f"(expected {expected_sha256}, got {got})"
        )
        return None
    return dl.stdout


def install_ci_binary(
    name: str,
    url: str,
    *,
    tar_member: str | None = None,
    expected_sha256: str | None = None,
) -> str | None:
    """Return a path to ``name``, installing the pinned release on Linux CI.

    Returns the existing path if already installed; ``None`` off-CI / non-Linux
    or on any download/extract failure (the caller decides whether that is
    fatal). ``tar_member`` is the binary's name inside a ``.tar.gz`` (omit for a
    raw-binary download).

    ``expected_sha256`` is the fail-closed integrity gate - see
    :func:`fetch_verified`, which owns it.
    """
    exe = shutil.which(name)
    if exe:
        return exe
    if not is_ci() or sys.platform != "linux":
        return None

    info(f"  Installing {name}...")
    payload = fetch_verified(name, url, expected_sha256)
    if payload is None:
        return None

    if tar_member:
        try:
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tf:
                member = next(
                    (m for m in tf.getmembers() if Path(m.name).name == tar_member),
                    None,
                )
                extracted = tf.extractfile(member) if member else None
                data = extracted.read() if extracted else None
        except (tarfile.TarError, OSError):
            data = None
        if not data:
            error(f"  {name}: '{tar_member}' not found in release archive")
            return None
    else:
        data = payload

    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        dest = Path("/usr/local/bin") / name
        # `sudo` can be absent / non-passwordless on a hardened runner - that is
        # an install failure to report (None), NOT a traceback that crashes a
        # blocking gate's whole quality stage.
        subprocess.run(["sudo", "mv", tmp_path, str(dest)], check=True)
        subprocess.run(["sudo", "chmod", "+x", str(dest)], check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        error(f"  Failed to install {name}: {exc}")
        Path(tmp_path).unlink(missing_ok=True)  # do not leak the temp on failure
        return None
    return shutil.which(name)
