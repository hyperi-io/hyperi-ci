# Project:   HyperI CI
# File:      tests/unit/test_common.py
# Purpose:   Tests for common utilities
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import pytest

from hyperi_ci import common
from hyperi_ci.common import normalise_tristate, run_cmd, sanitize_ref_name


class TestNormaliseTristate:
    """The shared on/off/auto coercion for stage gates."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (True, "true"),
            (False, "false"),
            ("true", "true"),
            ("True", "true"),
            ("FALSE", "false"),
            ("auto", "auto"),
            ("Auto", "auto"),
            (None, "auto"),
            ("garbage", "auto"),
        ],
    )
    def test_coercion(self, raw: object, expected: str) -> None:
        assert normalise_tristate(raw, key="publish.container.enabled") == expected

    def test_unknown_value_names_the_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The warning has to be findable — a bare "unknown value" tells
        # an operator nothing about which key to go fix. Intercept warn
        # itself; the loguru sink doesn't flush to stderr until teardown.
        warnings: list[str] = []
        monkeypatch.setattr(common, "warn", warnings.append)
        normalise_tristate("yes-please", key="deployment.producer")
        assert warnings and "deployment.producer" in warnings[0]

    def test_known_value_is_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        warnings: list[str] = []
        monkeypatch.setattr(common, "warn", warnings.append)
        normalise_tristate("auto", key="deployment.producer")
        assert not warnings


class TestSanitizeRefName:
    """Sanitize git ref names for use in file paths."""

    def test_branch_with_slash(self) -> None:
        assert sanitize_ref_name("fix/reconcile-release") == "fix-reconcile-release"

    def test_multiple_slashes(self) -> None:
        assert sanitize_ref_name("feat/scope/thing") == "feat-scope-thing"

    def test_no_slash(self) -> None:
        assert sanitize_ref_name("main") == "main"

    def test_tag_version(self) -> None:
        assert sanitize_ref_name("v1.2.3") == "v1.2.3"

    def test_empty_string(self) -> None:
        assert sanitize_ref_name("") == ""


class TestRunCmdUtf8:
    """run_cmd must tolerate non-UTF-8 bytes from subprocesses without
    crashing the caller. GitHub Actions log files in particular contain
    arbitrary build output that may include invalid UTF-8 sequences."""

    def test_decodes_utf8_output(self) -> None:
        """Plain UTF-8 output round-trips cleanly."""
        result = run_cmd(
            ["python3", "-c", "import sys; sys.stdout.write('hello — world')"],
            capture=True,
        )
        assert "hello — world" in result.stdout

    def test_replaces_invalid_utf8_bytes(self) -> None:
        """Invalid UTF-8 bytes (e.g. raw 0xff) must be replaced, not raise."""
        # 0xff is never valid in UTF-8. Without errors="replace" this would
        # raise UnicodeDecodeError when run_cmd tries to decode the captured
        # bytes — which is exactly what was breaking `hyperi-ci logs`.
        result = run_cmd(
            [
                "python3",
                "-c",
                "import sys; sys.stdout.buffer.write(b'before\\xffafter')",
            ],
            capture=True,
        )
        # We don't pin the exact replacement char (�) — just that no
        # exception was raised and the surrounding text is intact.
        assert "before" in result.stdout
        assert "after" in result.stdout
