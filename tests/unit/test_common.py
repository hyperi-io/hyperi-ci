# Project:   HyperI CI
# File:      tests/unit/test_common.py
# Purpose:   Tests for common utilities
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from hyperi_ci.common import run_cmd, sanitize_ref_name


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
