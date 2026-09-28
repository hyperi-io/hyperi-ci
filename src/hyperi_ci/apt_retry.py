# Project:   HyperI CI
# File:      src/hyperi_ci/apt_retry.py
# Purpose:   Retry settings for the apt-get calls hyperi-ci renders or runs
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Retry settings for the apt-get calls hyperi-ci renders or runs.

Covers the Dockerfiles hyperi-ci generates and ``native_deps``, which
runner-image bake runs from inside a Dockerfile ``RUN``.

Two failure shapes need two answers. A dropped connection or a timeout is
retried inside apt by ``Acquire::Retries``. A mirror caught mid-sync serves an
index whose size disagrees with its Release file ("File has unexpected size
... Mirror sync in progress?"), and apt 2.8.3 (Ubuntu noble) fails that fetch
once and exits 100 whatever ``Acquire::Retries`` says, so ``apt_update_sh``
re-runs the whole update after a pause long enough for the sync to finish.
"""

APT_RETRY_OPTION = "-o Acquire::Retries=5"
APT_UPDATE_ATTEMPTS = 4
APT_UPDATE_BACKOFF_SECONDS = 15


def apt_get_sh(args: str) -> str:
    """Render an ``apt-get`` shell command that retries transient fetch errors.

    Args:
        args: The apt-get subcommand and its arguments, e.g. ``"install -y curl"``.

    Returns:
        The command as Dockerfile ``RUN`` text.

    """
    return f"apt-get {APT_RETRY_OPTION} {args}"


def apt_update_sh() -> str:
    """Render a POSIX-sh ``apt-get update`` that outlasts a mirror mid-sync.

    Waits 15s, 30s, then 45s between attempts and exits 100, apt's own failure
    code, once every attempt has failed.

    Returns:
        One shell compound command, safe to chain with ``&&`` or ``;``.

    """
    attempts = " ".join(str(i) for i in range(1, APT_UPDATE_ATTEMPTS + 1))
    return (
        f"for i in {attempts}; do {apt_get_sh('update')} && break; "
        f'if [ "$i" -eq {APT_UPDATE_ATTEMPTS} ]; then exit 100; fi; '
        f"sleep $((i * {APT_UPDATE_BACKOFF_SECONDS})); done"
    )
