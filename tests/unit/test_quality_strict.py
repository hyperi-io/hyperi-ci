# Project:   HyperI CI
# File:      tests/unit/test_quality_strict.py
# Purpose:   Tests for strict quality mode (warn-tier findings -> blocking)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for strict quality mode.

Covers `hyperi_ci.languages.quality_common.strict_quality` and
`resolve_tool_mode` - the shared machinery behind `hyperi-ci check
--strict`, which upgrades warn-tier findings (ty, semgrep, docstrings)
to blocking so they surface before a push instead of after.
"""

import pytest

from hyperi_ci.config import CIConfig, packaged_default
from hyperi_ci.languages import quality_common
from hyperi_ci.languages.quality_common import (
    checked_mode,
    is_skipped,
    mode_and_reason,
    quality_skip,
    resolve_cross_tool_mode,
    resolve_tool_mode,
    strict_quality,
)

_ENV = "HYPERCI_QUALITY_STRICT"
_SKIP = "HYPERCI_QUALITY_SKIP"
_REASON = "GHSA-0000 has no patched release; mitigated by pod isolation"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate every test from ambient strict/skip env vars and from a CI runner.

    In CI a relaxed gate is an annotation instead of a log line, so a test
    reading the log line has to run as it would on a workstation.
    """
    monkeypatch.delenv(_ENV, raising=False)
    monkeypatch.delenv(_SKIP, raising=False)
    monkeypatch.setattr(quality_common, "is_ci", lambda: False)


def _config(tool: str, mode: object, language: str = "python") -> CIConfig:
    """Config with one tool set, as a bare mode string or a mode+reason mapping."""
    return CIConfig(_raw={"quality": {language: {tool: mode}}})


# Shipped gates whose findings are not security advisories, so a bare mode
# string may relax them.
_NOT_SECURITY = frozenset(
    {
        "charset",
        "hadolint",
        "droast",
        "kubeconform",
        "kube_linter",
        "compose_config",
        "compose_pins",
        "cargo_flags",
        "doc_paths",
        "doc_links",
        "mermaid_parse",
        "markdownlint",
        "docs_touched",
        "ty",
        "pyright",
        "ruff_format",
        "ruff_docstrings",
        "vulture",
        "eslint",
        "prettier",
        "tsc",
        "gofmt",
        "govet",
        "golangci_lint",
        "fmt",
        "clippy",
        "semver_checks",
    }
)
# Arguably security, and not yet decided: checkov is an IaC security scanner,
# and ruff carries the S (bandit) rules that replaced bandit.
_UNDECIDED = frozenset({"checkov", "ruff"})


def _shipped_gates() -> set[str]:
    """Every tool defaults.yaml ships a quality mode for, at either level."""
    modes = {"blocking", "warn", "disabled"}
    quality = packaged_default("quality")
    gates: set[str] = set()
    for key, value in quality.items():
        if isinstance(value, str) and value in modes:
            gates.add(key)
        elif isinstance(value, dict):
            gates.update(
                tool
                for tool, mode in value.items()
                if isinstance(mode, str) and mode in modes
            )
    return gates


class TestStrictQuality:
    """The env-driven strict switch."""

    def test_unset_is_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(_ENV, raising=False)
        assert strict_quality() is False

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "Yes", "on", " on "])
    def test_truthy_values(self, monkeypatch: pytest.MonkeyPatch, val: str) -> None:
        monkeypatch.setenv(_ENV, val)
        assert strict_quality() is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "", "off", "maybe"])
    def test_falsey_values(self, monkeypatch: pytest.MonkeyPatch, val: str) -> None:
        monkeypatch.setenv(_ENV, val)
        assert strict_quality() is False


class TestResolveToolMode:
    """Mode resolution, with and without strict."""

    def test_default_is_blocking(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(_ENV, raising=False)
        assert resolve_tool_mode("ty", CIConfig(_raw={}), "python") == "blocking"

    def test_configured_mode_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(_ENV, raising=False)
        warn_cfg = _config("ty", "warn")
        off_cfg = _config("bandit", "disabled")
        assert resolve_tool_mode("ty", warn_cfg, "python") == "warn"
        assert resolve_tool_mode("bandit", off_cfg, "python") == "disabled"

    def test_strict_upgrades_warn_to_blocking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_ENV, "1")
        assert resolve_tool_mode("ty", _config("ty", "warn"), "python") == "blocking"

    def test_strict_leaves_disabled_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Strict enforces warnings; it must NOT resurrect a disabled tool.
        monkeypatch.setenv(_ENV, "1")
        off_cfg = _config("bandit", "disabled")
        assert resolve_tool_mode("bandit", off_cfg, "python") == "disabled"

    def test_strict_leaves_blocking(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_ENV, "1")
        block_cfg = _config("ruff", "blocking")
        assert resolve_tool_mode("ruff", block_cfg, "python") == "blocking"

    @pytest.mark.parametrize("language", ["python", "rust", "golang", "typescript"])
    def test_strict_applies_across_languages(
        self, monkeypatch: pytest.MonkeyPatch, language: str
    ) -> None:
        monkeypatch.setenv(_ENV, "1")
        cfg = _config("semgrep", "warn", language)
        assert resolve_tool_mode("semgrep", cfg, language) == "blocking"


class TestGateDowngradeIsAnnounced:
    """A gate a repo relaxed reads the same as one that passed, unless it says so."""

    @staticmethod
    def _warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
        said: list[str] = []
        monkeypatch.setattr(quality_common, "warn", said.append)
        return said

    def test_warn_below_a_shipped_blocking_is_announced(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said = self._warnings(monkeypatch)
        cfg = _config("pip_audit", {"mode": "warn", "reason": _REASON})
        assert resolve_tool_mode("pip_audit", cfg, "python") == "warn"
        assert any("turned down" in w for w in said), said
        assert any("pip_audit" in w for w in said), said

    def test_the_stated_reason_is_printed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said = self._warnings(monkeypatch)
        cfg = _config("pip_audit", {"mode": "warn", "reason": _REASON})
        resolve_tool_mode("pip_audit", cfg, "python")
        assert any(_REASON in w for w in said), said

    def test_matching_the_shipped_default_says_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said = self._warnings(monkeypatch)
        assert resolve_tool_mode("vulture", _config("vulture", "warn"), "python") == (
            "warn"
        )
        assert said == []

    def test_raising_above_the_shipped_default_says_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said = self._warnings(monkeypatch)
        assert resolve_tool_mode(
            "vulture", _config("vulture", "blocking"), "python"
        ) == ("blocking")
        assert said == []

    def test_a_tool_with_no_shipped_default_says_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said = self._warnings(monkeypatch)
        resolve_tool_mode("not_a_tool", _config("not_a_tool", "disabled"), "python")
        assert said == []


class TestSecurityGateNeedsAReason:
    """Turning a CVE/secret/SAST gate down has to say what it is waiting on.

    `feature_matrix` has required a reason for its opt-out since it shipped,
    while a security gate took any mode with none -- dead-code coverage held to
    a higher standard than CVE scanning (issue #250).
    """

    @staticmethod
    def _warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
        said: list[str] = []
        monkeypatch.setattr(quality_common, "warn", said.append)
        return said

    def test_bare_warn_on_a_security_tool_is_named(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said = self._warnings(monkeypatch)
        assert resolve_tool_mode(
            "pip_audit", _config("pip_audit", "warn"), "python"
        ) == ("warn")
        message = " ".join(said)
        assert "quality.python.pip_audit" in message
        assert "security gate" in message
        # The fix has to be copyable from the message, not looked up in source.
        assert "mode: warn" in message
        assert "reason:" in message

    def test_bare_disabled_on_a_security_tool_is_named(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said = self._warnings(monkeypatch)
        resolve_tool_mode("audit", _config("audit", "disabled", "rust"), "rust")
        assert any("security gate" in w for w in said), said

    def test_a_stated_reason_lets_it_through(self) -> None:
        cfg = _config("audit", {"mode": "warn", "reason": _REASON}, "rust")
        assert resolve_tool_mode("audit", cfg, "rust") == "warn"

    def test_a_whitespace_only_reason_is_no_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said = self._warnings(monkeypatch)
        cfg = _config("audit", {"mode": "warn", "reason": "   "}, "rust")
        resolve_tool_mode("audit", cfg, "rust")
        assert any("security gate" in w for w in said), said

    def test_case_does_not_dodge_the_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said = self._warnings(monkeypatch)
        resolve_tool_mode("deny", _config("deny", " WARN ", "rust"), "rust")
        assert any("security gate" in w for w in said), said

    def test_matching_the_shipped_default_needs_no_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # osv_scanner ships `warn`, so writing it is not a downgrade.
        said = self._warnings(monkeypatch)
        cfg = _config("osv_scanner", "warn", "rust")
        assert resolve_tool_mode("osv_scanner", cfg, "rust") == "warn"
        assert said == []

    def test_a_security_tool_at_its_shipped_blocking_is_silent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said = self._warnings(monkeypatch)
        assert resolve_tool_mode("audit", _config("audit", "blocking", "rust"), "rust")
        assert said == []

    def test_below_a_shipped_warn_still_needs_a_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Shipped `warn` is not a licence to reach `disabled` unexplained.
        said = self._warnings(monkeypatch)
        resolve_tool_mode(
            "osv_scanner", _config("osv_scanner", "disabled", "rust"), "rust"
        )
        assert any("security gate" in w for w in said), said

    def test_a_non_security_tool_only_warns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said = self._warnings(monkeypatch)
        cfg = _config("ruff_format", "warn")
        assert resolve_tool_mode("ruff_format", cfg, "python") == "warn"
        assert any("turned down" in w for w in said), said

    def test_a_shipped_disabled_tool_has_nothing_below_it(self) -> None:
        # bandit ships `disabled` (superseded by ruff S rules) -- no downgrade.
        assert resolve_tool_mode("bandit", _config("bandit", "disabled"), "python") == (
            "disabled"
        )

    def test_the_cross_language_resolver_checks_it_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The generic turned-down warning names the key too, so only the
        # security-gate text proves the reason rule was applied.
        said = self._warnings(monkeypatch)
        cfg = CIConfig(_raw={"quality": {"gitleaks": "warn"}})
        resolve_cross_tool_mode(cfg, "gitleaks", "blocking")
        owed = [w for w in said if "security gate" in w]
        assert any("quality.gitleaks" in w for w in owed), said

    def test_strict_does_not_hide_a_missing_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # --strict upgrades warn to blocking, so checking the post-strict mode
        # would let a local strict run pass a config CI then reports on.
        monkeypatch.setenv(_ENV, "1")
        said = self._warnings(monkeypatch)
        resolve_tool_mode("pip_audit", _config("pip_audit", "warn"), "python")
        assert any("security gate" in w for w in said), said

    @pytest.mark.parametrize("tool", ["gosec", "govulncheck"])
    def test_the_go_security_gates_need_a_reason(
        self, monkeypatch: pytest.MonkeyPatch, tool: str
    ) -> None:
        # Both ship `blocking`; govulncheck is Go's CVE gate.
        said = self._warnings(monkeypatch)
        resolve_tool_mode(tool, _config(tool, "warn", "golang"), "golang")
        assert any("security gate" in w for w in said), said

    def test_every_security_tool_is_a_key_hyperi_ci_ships(self) -> None:
        # A name no config key uses guards nothing and hides the gap beside it.
        scopes = ("python", "typescript", "golang", "rust")
        for tool in quality_common.SECURITY_TOOLS:
            keys = [f"quality.{tool}", *(f"quality.{s}.{tool}" for s in scopes)]
            assert any(packaged_default(k) is not None for k in keys), tool

    def test_every_shipped_gate_is_classified(self) -> None:
        # A new CVE or secret scanner added to defaults.yaml must fail here until
        # someone decides which side of the reason rule it sits on.
        shipped = _shipped_gates()
        security = quality_common.SECURITY_TOOLS
        assert not (security & _NOT_SECURITY), security & _NOT_SECURITY
        assert not (security & _UNDECIDED), security & _UNDECIDED
        unclassified = shipped - security - _NOT_SECURITY - _UNDECIDED
        assert not unclassified, (
            f"classify these in SECURITY_TOOLS or here: {unclassified}"
        )
        stale = (_NOT_SECURITY | _UNDECIDED) - shipped
        assert not stale, f"no longer shipped: {stale}"

    def test_force_skip_needs_no_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The incident escape hatch exists for a CI that is already broken.
        monkeypatch.setenv(_SKIP, "pip_audit")
        assert resolve_tool_mode(
            "pip_audit", _config("pip_audit", "warn"), "python"
        ) == ("disabled")


class TestDowngradeAnnotationIsOneCommand:
    """The runner reads a raw newline as the end of a workflow command.

    The reason-owed message is multi-line, and a stated reason is whatever the
    repo wrote, so either one unescaped cuts the annotation short and hands
    the rest of the line to the runner as further input.
    """

    @staticmethod
    def _annotations(capsys: pytest.CaptureFixture[str]) -> list[str]:
        out = capsys.readouterr().out
        return [line for line in out.splitlines() if line.startswith("::")]

    @pytest.fixture(autouse=True)
    def logged(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        """Run in CI, recording what reaches the logger.

        Under GitHub Actions the logger's warning is an annotation too, so in
        CI anything it records is a second annotation for the same event.
        """
        monkeypatch.setattr(quality_common, "is_ci", lambda: True)
        said: list[str] = []
        monkeypatch.setattr(quality_common, "warn", said.append)
        return said

    def test_a_missing_reason_is_annotated_in_ci(
        self, capsys: pytest.CaptureFixture[str], logged: list[str]
    ) -> None:
        # A logger line sits inside a folded group; the annotation reaches the
        # run summary, which is where a reader sees a relaxed security gate.
        resolve_tool_mode("pip_audit", _config("pip_audit", "warn"), "python")
        annotations = self._annotations(capsys)
        assert len(annotations) == 1, annotations
        assert annotations[0].startswith(
            "::warning title=hyperi-ci security gate needs a reason::"
        )
        assert "quality.python.pip_audit" in annotations[0]
        assert logged == []

    def test_a_turned_down_gate_is_annotated_in_ci(
        self, capsys: pytest.CaptureFixture[str], logged: list[str]
    ) -> None:
        resolve_tool_mode("vulture", _config("vulture", "disabled"), "python")
        annotations = self._annotations(capsys)
        assert len(annotations) == 1, annotations
        assert annotations[0].startswith("::warning title=hyperi-ci gate turned down::")
        assert "quality.python.vulture" in annotations[0]
        assert logged == []

    def test_outside_ci_it_is_a_log_line_and_no_annotation(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        logged: list[str],
    ) -> None:
        monkeypatch.setattr(quality_common, "is_ci", lambda: False)
        resolve_tool_mode("pip_audit", _config("pip_audit", "warn"), "python")
        assert self._annotations(capsys) == []
        assert any("quality.python.pip_audit" in w for w in logged), logged

    def test_the_multi_line_reason_owed_message_stays_one_line(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        resolve_tool_mode("pip_audit", _config("pip_audit", "warn"), "python")
        out = capsys.readouterr().out
        assert out.count("\n") == 1, out
        assert "%0A" in out

    def test_a_stated_reason_cannot_start_a_second_command(
        self, capsys: pytest.CaptureFixture[str], logged: list[str]
    ) -> None:
        reason = "no fix yet\n::error::planted"
        cfg = _config("pip_audit", {"mode": "warn", "reason": reason})
        resolve_tool_mode("pip_audit", cfg, "python")
        out = capsys.readouterr().out
        assert out.count("\n") == 1, out
        assert out.startswith("::warning title=hyperi-ci gate turned down::")
        assert "reason: no fix yet ::error::planted" in out
        assert logged == []


class TestConfigTextComesBackOnOneLine:
    """A line break in a quality value would reach the log as a line of its own.

    Under GitHub Actions the runner reads such a line as a workflow command, and
    the value is whatever the repo's config says.
    """

    @pytest.mark.parametrize("line_break", ["\n", "\r\n", "\r"])
    def test_a_multi_line_reason(self, line_break: str) -> None:
        raw = {"mode": "warn", "reason": f" no fix yet{line_break}::error::planted "}
        assert mode_and_reason(raw, "blocking") == (
            "warn",
            "no fix yet ::error::planted",
        )

    def test_a_multi_line_mode(self) -> None:
        mode, _ = mode_and_reason("warn\n::ERROR::planted", "blocking")
        assert mode == "warn ::error::planted"

    def test_an_unknown_multi_line_mode_is_named_on_one_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        said: list[str] = []
        monkeypatch.setattr(quality_common, "warn", said.append)
        raw = "block\n::error::planted"
        assert checked_mode("quality.python.ruff", raw, "blocking") == ("blocking", "")
        assert len(said) == 1, said
        assert "\n" not in said[0]


class TestRuffFormatHasItsOwnMode:
    """`ruff format` is configured apart from `ruff check`.

    Adopting the formatter on an established tree is a whole-repo decision, so a
    project must be able to defer it without relaxing the real lint gate.
    """

    def test_it_blocks_by_default(self) -> None:
        assert resolve_tool_mode("ruff_format", CIConfig(_raw={}), "python") == (
            "blocking"
        )

    def test_relaxing_it_leaves_ruff_check_blocking(self) -> None:
        cfg = _config("ruff_format", "warn")
        assert resolve_tool_mode("ruff_format", cfg, "python") == "warn"
        assert resolve_tool_mode("ruff", cfg, "python") == "blocking"

    def test_relaxing_ruff_check_does_not_relax_it(self) -> None:
        cfg = _config("ruff", "warn")
        assert resolve_tool_mode("ruff_format", cfg, "python") == "blocking"


class TestQualitySkip:
    """HYPERCI_QUALITY_SKIP: the rare force-skip escape hatch."""

    def test_unset_is_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(_SKIP, raising=False)
        assert quality_skip() == frozenset()

    def test_parses_comma_separated_lowercased(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_SKIP, "Semgrep, Bandit ,")
        assert quality_skip() == frozenset({"semgrep", "bandit"})

    def test_is_skipped_is_case_insensitive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_SKIP, "semgrep")
        assert is_skipped("semgrep") is True
        assert is_skipped("SEMGREP") is True
        assert is_skipped("ruff") is False

    def test_skip_disables_even_a_blocking_tool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(_ENV, raising=False)
        monkeypatch.setenv(_SKIP, "ruff")
        assert resolve_tool_mode("ruff", _config("ruff", "blocking"), "python") == (
            "disabled"
        )

    def test_skip_wins_over_strict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Both set: skip wins - the tool is disabled, not upgraded to blocking.
        monkeypatch.setenv(_ENV, "1")
        monkeypatch.setenv(_SKIP, "ty")
        assert resolve_tool_mode("ty", _config("ty", "warn"), "python") == "disabled"
