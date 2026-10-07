# Project:   HyperI CI
# File:      src/hyperi_ci/apt_retry.py
# Purpose:   Retry settings for the apt-get calls hyperi-ci runs
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Retry settings for the apt-get calls hyperi-ci runs.

``native_deps`` uses them, and runner-image bake runs that from inside a
Dockerfile ``RUN``.

Two failure shapes need two answers. A dropped connection or a timeout is
retried inside apt by ``Acquire::Retries``. A mirror caught mid-sync serves an
index whose size disagrees with its Release file ("File has unexpected size
... Mirror sync in progress?"), and apt 2.8.3 (Ubuntu noble) fails that fetch
once and exits 100 whatever ``Acquire::Retries`` says, so the whole update is
re-run after a pause long enough for the sync to finish.
"""

APT_RETRY_OPTION = "-o Acquire::Retries=5"
APT_UPDATE_ATTEMPTS = 4
APT_UPDATE_BACKOFF_SECONDS = 15
