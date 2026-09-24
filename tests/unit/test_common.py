# Project:   HyperI CI
# File:      tests/unit/test_common.py
# Purpose:   Tests for common utilities
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

import subprocess
import sys
import time
from typing import Literal

import pytest

from hyperi_ci import common
from hyperi_ci.common import (
    is_prerelease_build,
    normalise_tristate,
    optimize_tier,
    release_unoptimized,
    run_cmd,
    sanitize_ref_name,
    skip_optimize,
)
from hyperi_ci.config import CIConfig

_PLANTED = "no fix yet\n::error title=planted::x\r  ::notice::planted\n##[error]planted"


class TestLogTextStartsNoCommand:
    """What a GitHub Actions runner reads from our log lines.

    scalo writes every line of a message after the first raw, and the runner
    reads a line starting with ``::`` or ``##[`` as a workflow command, so a
    value from repo config could plant one through any log call.
    """

    @staticmethod
    def _commands(call: str, env: dict[str, str]) -> list[str]:
        code = f"from hyperi_ci import common\ncommon.{call}({_PLANTED!r})\n"
        result = run_cmd(
            [sys.executable, "-c", code], capture=True, env=env, timeout=60
        )
        output = f"{result.stdout}\n{result.stderr}"
        return [
            line
            for line in output.splitlines()
            if line.lstrip().startswith(("::", "##[")) and "planted" in line
        ]

    @pytest.mark.parametrize("call", ["info", "success", "warn", "error"])
    def test_a_planted_line_starts_no_command(self, call: str) -> None:
        env = {"CI": "true", "GITHUB_ACTIONS": "true"}
        assert self._commands(call, env) == []

    def test_the_text_still_reaches_the_log(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(common, "is_github_actions", lambda: True)
        assert common._inert(_PLANTED).splitlines() == [
            "no fix yet",
            "| ::error title=planted::x",
            "|   ::notice::planted",
            "| ##[error]planted",
        ]

    def test_ordinary_multi_line_text_is_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(common, "is_github_actions", lambda: True)
        text = "help: install it with one of:\n  brew install x\n  cargo install x"
        assert common._inert(text) == text

    def test_outside_github_actions_nothing_changes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(common, "is_github_actions", lambda: False)
        assert common._inert(_PLANTED) == _PLANTED


class TestAnnounce:
    """One report per event: an annotation under GitHub Actions, a log line elsewhere.

    Under GitHub Actions the logger's own warning or error is an annotation too,
    so logging as well as annotating reports the event twice.
    """

    @pytest.fixture
    def logged(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
        said: dict[str, list[str]] = {"warn": [], "error": []}
        monkeypatch.setattr(common, "warn", said["warn"].append)
        monkeypatch.setattr(common, "error", said["error"].append)
        return said

    def test_under_github_actions_it_is_one_escaped_annotation(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        logged: dict[str, list[str]],
    ) -> None:
        monkeypatch.setattr(common, "is_github_actions", lambda: True)
        common.announce("first\n::error::planted", "hyperi-ci test")
        out = capsys.readouterr().out
        assert out == "::warning title=hyperi-ci test::first%0A::error::planted\n"
        assert logged == {"warn": [], "error": []}

    def test_an_error_is_an_error_annotation(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        logged: dict[str, list[str]],
    ) -> None:
        monkeypatch.setattr(common, "is_github_actions", lambda: True)
        common.announce("refused", "hyperi-ci test", level="error")
        assert capsys.readouterr().out == "::error title=hyperi-ci test::refused\n"
        assert logged == {"warn": [], "error": []}

    @pytest.mark.parametrize("level", ["warning", "error"])
    def test_elsewhere_it_is_a_log_line_at_its_level(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        logged: dict[str, list[str]],
        level: Literal["warning", "error"],
    ) -> None:
        monkeypatch.setattr(common, "is_github_actions", lambda: False)
        common.announce("said once", "hyperi-ci test", level=level)
        assert capsys.readouterr().out == ""
        key = "error" if level == "error" else "warn"
        assert logged[key] == ["said once"]
        assert sum(len(v) for v in logged.values()) == 1


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
        # The warning has to be findable -- a bare "unknown value" tells
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


class TestSkipOptimize:
    """issue #132: the language-agnostic "skip the optimisation stage" switch.

    Precedence is the config cascade -- the env var the reusable workflows set
    beats the project's own config key, and both default off so optimisation
    stays on for a repo that asks for nothing.
    """

    @staticmethod
    def _config(value: object) -> CIConfig:
        return CIConfig(_raw={"build": {"skip_optimize": value}})

    def test_default_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HYPERCI_SKIP_OPTIMIZE", raising=False)
        assert skip_optimize() is False

    def test_config_key_opts_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HYPERCI_SKIP_OPTIMIZE", raising=False)
        assert skip_optimize(self._config(True)) is True

    def test_config_key_absent_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HYPERCI_SKIP_OPTIMIZE", raising=False)
        assert skip_optimize(CIConfig(_raw={})) is False

    @pytest.mark.parametrize("raw", ["true", "TRUE", "1", "yes", "on"])
    def test_env_opts_in(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("HYPERCI_SKIP_OPTIMIZE", raw)
        assert skip_optimize() is True

    @pytest.mark.parametrize("raw", ["false", "0", "no", "off"])
    def test_env_opts_out(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("HYPERCI_SKIP_OPTIMIZE", raw)
        assert skip_optimize() is False

    def test_env_beats_the_config_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A run that explicitly asks for optimisation gets it even in a repo
        # whose config skips by default.
        monkeypatch.setenv("HYPERCI_SKIP_OPTIMIZE", "false")
        assert skip_optimize(self._config(True)) is False

    def test_empty_env_falls_through_to_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The workflows always export the var, and it is empty when neither the
        # dispatch input nor the repo variable is set. An empty value must not
        # mask the project's own config.
        monkeypatch.setenv("HYPERCI_SKIP_OPTIMIZE", "")
        assert skip_optimize(self._config(True)) is True


class TestOptimizeTier:
    """issue #257: a run asks for the release optimisation tier by name."""

    def test_unset_asks_for_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HYPERCI_OPTIMIZE_TIER", raising=False)
        assert optimize_tier() == ""

    def test_the_value_is_normalised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HYPERCI_OPTIMIZE_TIER", " Release ")
        assert optimize_tier() == "release"

    def test_the_release_tier_beats_every_skip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A repo-wide skip must not strip PGO and BOLT from a run that asked
        # for them by name, and the override is said out loud.
        warnings: list[str] = []
        monkeypatch.setattr(common, "warn", warnings.append)
        monkeypatch.setenv("HYPERCI_OPTIMIZE_TIER", "release")
        monkeypatch.setenv("HYPERCI_SKIP_OPTIMIZE", "true")
        config = CIConfig(_raw={"build": {"skip_optimize": True}})
        assert skip_optimize(config) is False
        assert warnings and "optimize-tier=release" in warnings[0]

    def test_the_release_tier_with_no_skip_is_quiet(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        warnings: list[str] = []
        monkeypatch.setattr(common, "warn", warnings.append)
        monkeypatch.setenv("HYPERCI_OPTIMIZE_TIER", "release")
        monkeypatch.delenv("HYPERCI_SKIP_OPTIMIZE", raising=False)
        assert skip_optimize() is False
        assert not warnings

    def test_an_unknown_tier_leaves_the_skip_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The build refuses the value; skip_optimize must not treat it as a yes.
        monkeypatch.setenv("HYPERCI_OPTIMIZE_TIER", "beta")
        monkeypatch.setenv("HYPERCI_SKIP_OPTIMIZE", "true")
        assert skip_optimize() is True


class TestReleaseUnoptimized:
    """issue #158: consent to release a skipped build is per-run, env only."""

    def test_default_is_no_consent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HYPERCI_RELEASE_UNOPTIMIZED", raising=False)
        assert release_unoptimized() is False

    def test_empty_env_is_no_consent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The workflows always export it, empty when the input is unset.
        monkeypatch.setenv("HYPERCI_RELEASE_UNOPTIMIZED", "")
        assert release_unoptimized() is False

    def test_true_is_consent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HYPERCI_RELEASE_UNOPTIMIZED", "true")
        assert release_unoptimized() is True


class TestIsPrereleaseBuild:
    """issue #144: version identity, independent of the optimisation tier."""

    def test_env_says_prerelease(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HYPERCI_PRERELEASE", "true")
        assert is_prerelease_build() is True

    def test_env_says_stable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HYPERCI_PRERELEASE", "false")
        monkeypatch.setenv("HYPERCI_VERSION", "1.2.0-beta.1")
        assert is_prerelease_build() is False

    def test_empty_env_falls_back_to_the_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The workflows always export it, empty when the plan produced no
        # version -- a local run then answers from the version itself.
        monkeypatch.setenv("HYPERCI_PRERELEASE", "")
        monkeypatch.setenv("HYPERCI_VERSION", "1.2.0-beta.1")
        assert is_prerelease_build() is True

    def test_a_stable_version_is_not_a_prerelease(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HYPERCI_PRERELEASE", raising=False)
        monkeypatch.setenv("HYPERCI_VERSION", "1.2.0")
        assert is_prerelease_build() is False


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
        # bytes -- which is exactly what was breaking `hyperi-ci logs`.
        result = run_cmd(
            [
                "python3",
                "-c",
                "import sys; sys.stdout.buffer.write(b'before\\xffafter')",
            ],
            capture=True,
        )
        # We don't pin the exact replacement char (�) -- just that no
        # exception was raised and the surrounding text is intact.
        assert "before" in result.stdout
        assert "after" in result.stdout


class TestRunCmdTimeout:
    """A caller that cannot wait forever bounds the child."""

    def test_a_child_past_its_timeout_is_killed(self) -> None:
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            run_cmd(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                capture=True,
                timeout=0.5,
            )
        assert time.monotonic() - started < 3


class TestStreamCmd:
    """A streamed step returns when its child exits, whatever inherited the pipe."""

    def test_a_grandchild_holding_the_pipe_does_not_hang_it(self) -> None:
        """The backgrounded sleep keeps stdout open for 30s after the child exits."""
        started = time.monotonic()
        rc, output = common.stream_cmd(
            ["bash", "-c", "echo parent; (sleep 30; echo late) & exit 3"],
            on_line=lambda _line: None,
        )
        elapsed = time.monotonic() - started
        assert rc == 3
        assert output == "parent"
        assert elapsed < 20


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
