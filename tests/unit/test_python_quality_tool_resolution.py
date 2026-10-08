# Project:   HyperI CI
# File:      tests/unit/test_python_quality_tool_resolution.py
# Purpose:   Tests for pinned-vs-PATH quality tool resolution
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""A quality tool found on PATH must never silently beat a pinned spec.

`hyperi-ci check` ran a system vulture 2.14 instead of the pinned 2.16, and
2.14 flagged a finding 2.16 does not -- a local pass/fail that disagreed with
CI, with nothing in the output naming the mismatch.
"""

import shutil

import pytest

from hyperi_ci.languages import quality_common
from hyperi_ci.languages.quality_common import resolve_tool_cmd


class TestPinnedSpecBeatsPath:
    """A pin is a version CI validated; a same-named PATH tool is not."""

    def test_pinned_spec_runs_through_uvx_even_when_the_tool_is_on_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        resolved = resolve_tool_cmd(
            ["vulture", "src/"], via="uvx", spec="vulture==2.16"
        )
        assert resolved == ["uvx", "--from", "vulture==2.16", "vulture", "src/"]

    def test_pinned_spec_runs_through_uv_with_even_when_the_tool_is_on_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        resolved = resolve_tool_cmd(["ty", "check"], via="uv-with", spec="ty==0.1.0")
        assert resolved == ["uv", "run", "--with", "ty==0.1.0", "--", "ty", "check"]

    def test_unpinned_spec_on_path_is_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ruff/pytest resolve via the project's own venv -- PATH still wins."""
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        resolved = resolve_tool_cmd(["ruff", "check", "."], via="uv")
        assert resolved == ["ruff", "check", "."]

    def test_path_is_returned_as_given(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``path`` never rewrites the command, even with uv present and a pin."""
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        resolved = resolve_tool_cmd(
            ["vulture", "src/"], via="path", spec="vulture==2.16"
        )
        assert resolved == ["vulture", "src/"]

    def test_pinned_spec_falls_back_to_path_when_uv_is_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def which(name: str) -> str | None:
            return None if name == "uv" else f"/usr/bin/{name}"

        monkeypatch.setattr(shutil, "which", which)
        said: list[str] = []
        monkeypatch.setattr(quality_common, "warn", said.append)
        monkeypatch.setattr(quality_common, "installed_version", lambda _binary: None)

        resolved = resolve_tool_cmd(
            ["vulture", "src/"], via="uvx", spec="vulture==2.16"
        )

        assert resolved == ["vulture", "src/"]
        assert len(said) == 1
        assert "vulture==2.16" in said[0]
        assert "uv is not on PATH" in said[0]
