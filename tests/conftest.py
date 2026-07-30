# Project:   HyperI CI
# File:      tests/conftest.py
# Purpose:   Shared fixtures -- keep the suite off the developer's real config
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from pathlib import Path

import pytest

from hyperi_ci import channel


@pytest.fixture(autouse=True)
def isolated_channel_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point auto-update channel/freeze state at tmp_path for every test.

    Without this a developer who has run `hyperi-ci autoupdate freeze` (or has
    hyperi-ai frozen) sees auto-update tests fail on their machine and pass in
    CI. The state is two files in the homedir, so the only safe default is to
    redirect both tools' directories.

    Returns:
        The redirected hyperi-ci config directory.

    """
    ci_dir = tmp_path / "config-hyperi-ci"
    ai_dir = tmp_path / "config-hyperi-ai"
    monkeypatch.setattr(channel, "CONFIG_DIR", ci_dir)
    monkeypatch.setattr(channel, "AI_CONFIG_DIR", ai_dir)
    return ci_dir
