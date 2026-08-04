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


class TestMask:
    """`mask` registers a secret for redaction, so what it emits is security-
    relevant in its own right: the runner parses stdout line by line, and the
    one caller feeds it an R2 secret key straight from the environment."""

    def test_emits_the_add_mask_command(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(common, "is_github_actions", lambda: True)
        common.mask("s3cret")
        assert capsys.readouterr().out == "::add-mask::s3cret\n"

    def test_silent_outside_actions(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(common, "is_github_actions", lambda: False)
        common.mask("s3cret")
        assert capsys.readouterr().out == ""

    def test_percent_is_escaped_so_the_registered_value_is_the_real_one(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The runner unescapes command data, so an unescaped `%` registers a
        different string and leaves the actual secret unmasked."""
        monkeypatch.setattr(common, "is_github_actions", lambda: True)
        common.mask("ab%25cd")
        assert capsys.readouterr().out == "::add-mask::ab%2525cd\n"

    def test_newlines_are_encoded_not_split(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """One line, so the remainder cannot be parsed as a further command."""
        monkeypatch.setattr(common, "is_github_actions", lambda: True)
        common.mask("first\n::set-output name=x::y")
        out = capsys.readouterr().out
        assert out == "::add-mask::first%0A::set-output name=x::y\n"
        assert len(out.splitlines()) == 1

    @pytest.mark.parametrize("blank", ["", "   ", "\n", "\t"])
    def test_blank_values_emit_nothing(
        self,
        blank: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The runner rejects a blank mask with a warning, so do not send one."""
        monkeypatch.setattr(common, "is_github_actions", lambda: True)
        common.mask(blank)
        assert capsys.readouterr().out == ""

    def test_is_flushed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Masking is not retroactive: an unflushed command can reach the log
        after a spawned child has already printed the secret."""
        monkeypatch.setattr(common, "is_github_actions", lambda: True)
        flushed: list[bool] = []
        monkeypatch.setattr(
            "builtins.print", lambda *a, **kw: flushed.append(kw.get("flush", False))
        )
        common.mask("s3cret")
        assert flushed == [True]


class TestEscapeCommandData:
    """The encoding the runner's UnescapeData expects."""

    def test_percent_first_then_line_breaks(self) -> None:
        # `%` must go first, or it would re-encode the `%` the others insert.
        assert common.escape_command_data("a%b\rc\nd") == "a%25b%0Dc%0Ad"

    def test_plain_value_unchanged(self) -> None:
        assert common.escape_command_data("plain-value") == "plain-value"
