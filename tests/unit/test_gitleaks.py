# Project:   HyperI CI
# File:      tests/unit/test_gitleaks.py
# Purpose:   Tests for the dispatch-level gitleaks quality module
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for the centralised (dispatch-level) gitleaks module.

Two things are pinned down here, both from issue #64:

* the **subcommand**. `detect` is deprecated - gone from `--help` as of
  gitleaks 8.30.1, honoured only for back-compat. The replacement is not a
  rename: `git` takes the repo path POSITIONALLY where `detect` took it via
  `--source`, so a half-done migration is a plausible regression.
* the **rule-less-config guard**. A repo `.gitleaks.toml` with allowlists but
  no `[[rules]]` and no `[extend]` leaves gitleaks with an empty ruleset: it
  reads every byte, matches nothing, and exits 0. A `blocking` gate silently
  becomes a no-op reporting success. The guard must refuse that, and its
  severity follows the gate's own mode.

`TestKnownEvasions` deliberately asserts what the guard does NOT catch. Those
are not aspirational tests - they pin the documented scope so nobody reads the
guard as "the scan is definitely not blind" (see #67). If someone widens the
check, those tests fail loudly and get updated on purpose.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.quality import gitleaks

_SKIP = "HYPERCI_QUALITY_SKIP"
_STRICT = "HYPERCI_QUALITY_STRICT"


def _cfg(raw: dict | None = None) -> CIConfig:
    return CIConfig(_raw=raw or {})


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with skip + strict unset, whatever the shell had."""
    monkeypatch.delenv(_SKIP, raising=False)
    monkeypatch.delenv(_STRICT, raising=False)


def _fake_gitleaks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    scan_rc: int = 0,
    branch: str = "main",
) -> list[list[str]]:
    """Pretend gitleaks is installed; record commands; control the scan's exit.

    Two seams, because the module uses two: `run_cmd` for the branch probe (the
    house wrapper, which pins UTF-8 decoding) and raw `subprocess.run` for the
    scan itself (it streams gitleaks' output rather than capturing it).

    `scan_rc` exists so the leaks-FOUND path is reachable. With it hard-coded to
    0, the warn/blocking branches at the end of run() were never exercised and
    would have passed even if deleted.
    """
    calls: list[list[str]] = []

    def fake_run_cmd(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, f"{branch}\n", "")

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, scan_rc, "", "")

    monkeypatch.setattr(gitleaks, "_install_gitleaks", lambda: True)
    monkeypatch.setattr(gitleaks, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(gitleaks.subprocess, "run", fake_run)
    return calls


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """A clean scan (gitleaks exits 0) with every command recorded."""
    return _fake_gitleaks(monkeypatch)


def _scan_cmd(calls: list[list[str]]) -> list[str]:
    return next(c for c in calls if c and c[0] == "gitleaks")


class TestDeclaresNoRuleset:
    """`_declares_no_ruleset` decides whether a config names any rule SOURCE."""

    def _write(self, tmp_path: Path, body: str) -> str:
        path = tmp_path / ".gitleaks.toml"
        path.write_text(body, encoding="utf-8", newline="\n")
        return str(path)

    def test_allowlists_without_rules_or_extend_is_blind(self, tmp_path: Path) -> None:
        # The exact shape reported in #64.
        cfg = self._write(
            tmp_path,
            "[[allowlists]]\ndescription = \"fixtures\"\npaths = ['''testdata/''']\n",
        )
        assert gitleaks._declares_no_ruleset(cfg) is True

    def test_extend_use_default_is_not_blind(self, tmp_path: Path) -> None:
        cfg = self._write(tmp_path, "[extend]\nuseDefault = true\n")
        assert gitleaks._declares_no_ruleset(cfg) is False

    @pytest.mark.parametrize(
        "body",
        [
            "[extend]\nusedefault = true\n",
            "[extend]\nUseDefault = true\n",  # the natural spelling
            "[extend]\nUSEDEFAULT = true\n",
            "[Extend]\nuSeDeFaUlT = true\n",
            "[EXTEND]\nUseDefault = true\n",
        ],
    )
    def test_extend_is_case_insensitive(self, tmp_path: Path, body: str) -> None:
        # viper folds key case COMPLETELY, so all of these are working configs
        # that really do find secrets. Calling any of them blind would hard-fail
        # CI on a repo whose scanner is fine - a false positive, in the default
        # blocking mode. Verified against the real binary during #64 review.
        assert gitleaks._declares_no_ruleset(self._write(tmp_path, body)) is False

    @pytest.mark.parametrize("body", ["[[Rules]]\nid = 'x'\nregex = '''s'''\n"])
    def test_rules_key_is_case_insensitive(self, tmp_path: Path, body: str) -> None:
        assert gitleaks._declares_no_ruleset(self._write(tmp_path, body)) is False

    @pytest.mark.parametrize("key", ["path", "Path"])
    def test_extend_path_is_a_rule_source(self, tmp_path: Path, key: str) -> None:
        cfg = self._write(tmp_path, f'[extend]\n{key} = "somewhere.toml"\n')
        assert gitleaks._declares_no_ruleset(cfg) is False

    def test_extend_url_is_blind_because_gitleaks_ignores_it(
        self, tmp_path: Path
    ) -> None:
        # NOT an oversight. gitleaks' extendURL() is an empty `// TODO` stub as
        # of 8.30.1 and nothing reads Extend.URL, so a url-only extend loads
        # ZERO rules and reports "no leaks found" on a repo full of secrets.
        # Blessing it would reintroduce the exact #64 failure this guard exists
        # to catch. If upstream ever implements it, this test fails - on purpose.
        cfg = self._write(tmp_path, '[extend]\nurl = "https://example.test/x.toml"\n')
        assert gitleaks._declares_no_ruleset(cfg) is True

    def test_own_rules_are_not_blind(self, tmp_path: Path) -> None:
        cfg = self._write(
            tmp_path,
            "[[rules]]\nid = \"x\"\ndescription = \"x\"\nregex = '''secret'''\n",
        )
        assert gitleaks._declares_no_ruleset(cfg) is False

    def test_extend_present_but_empty_is_blind(self, tmp_path: Path) -> None:
        # `[extend]` with nothing in it pulls in no ruleset.
        cfg = self._write(tmp_path, "[extend]\n")
        assert gitleaks._declares_no_ruleset(cfg) is True

    def test_unparseable_config_is_not_flagged(self, tmp_path: Path) -> None:
        # gitleaks reports malformed TOML better than we can - don't turn a
        # syntax error into a spurious "your gate is blind".
        cfg = self._write(tmp_path, "this is not = = toml\n")
        assert gitleaks._declares_no_ruleset(cfg) is False

    def test_missing_config_is_not_flagged(self, tmp_path: Path) -> None:
        assert gitleaks._declares_no_ruleset(str(tmp_path / "absent.toml")) is False


class TestScanCommand:
    """Regression guard for the #64 subcommand migration."""

    def test_uses_git_subcommand_not_deprecated_detect(
        self, captured: list[list[str]]
    ) -> None:
        assert gitleaks.run(_cfg()) == 0
        cmd = _scan_cmd(captured)
        assert cmd[1] == "git", f"expected the `git` subcommand, got {cmd[1]!r}"
        assert "detect" not in cmd

    def test_repo_path_is_positional_not_source_flag(
        self, captured: list[list[str]]
    ) -> None:
        # `--source` is a detect-only flag; `git` takes [repo] positionally.
        assert gitleaks.run(_cfg()) == 0
        cmd = _scan_cmd(captured)
        assert cmd[:3] == ["gitleaks", "git", "."]
        assert "--source" not in cmd

    def test_restricts_to_current_branch(self, captured: list[list[str]]) -> None:
        assert gitleaks.run(_cfg()) == 0
        cmd = _scan_cmd(captured)
        assert cmd[cmd.index("--log-opts") + 1] == "main"


class TestBlindingGuard:
    """The guard's severity follows the gate's own mode."""

    @pytest.fixture(autouse=True)
    def _blind_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gitleaks, "_find_config", lambda: ".gitleaks.toml")
        monkeypatch.setattr(gitleaks, "_declares_no_ruleset", lambda _cfg: True)

    def test_blocking_mode_refuses_to_run(self, captured: list[list[str]]) -> None:
        cfg = _cfg({"quality": {"gitleaks": "blocking"}})
        assert gitleaks.run(cfg) == 1
        # It must not report success off a rule-less scan - so it never scans.
        assert not [c for c in captured if c and c[0] == "gitleaks"]

    def test_warn_mode_proceeds(self, captured: list[list[str]]) -> None:
        cfg = _cfg({"quality": {"gitleaks": "warn"}})
        assert gitleaks.run(cfg) == 0
        assert _scan_cmd(captured)[1] == "git"

    def test_sound_config_is_passed_through(
        self, captured: list[list[str]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gitleaks, "_declares_no_ruleset", lambda _cfg: False)
        assert gitleaks.run(_cfg()) == 0
        cmd = _scan_cmd(captured)
        assert cmd[cmd.index("--config") + 1] == ".gitleaks.toml"


class TestLeaksFound:
    """The findings path - gitleaks exits non-zero because it found something."""

    def test_blocking_mode_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_gitleaks(monkeypatch, scan_rc=1)
        cfg = _cfg({"quality": {"gitleaks": "blocking"}})
        assert gitleaks.run(cfg) == 1

    def test_warn_mode_downgrades_a_finding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Distinct from a clean scan also returning 0: here gitleaks FOUND a
        # secret (rc=1) and warn mode is what turns that into a pass.
        _fake_gitleaks(monkeypatch, scan_rc=1)
        cfg = _cfg({"quality": {"gitleaks": "warn"}})
        assert gitleaks.run(cfg) == 0

    def test_strict_upgrades_warn_to_blocking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # --strict must reach gitleaks like it reaches semgrep, or a developer
        # who asked for strict gets a green stage out of a real finding.
        _fake_gitleaks(monkeypatch, scan_rc=1)
        monkeypatch.setenv("HYPERCI_QUALITY_STRICT", "1")
        cfg = _cfg({"quality": {"gitleaks": "warn"}})
        assert gitleaks.run(cfg) == 1


class TestDetachedHead:
    def test_empty_branch_falls_back_to_head(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `git branch --show-current` exits 0 with EMPTY stdout on a detached
        # HEAD, so a returncode-based guard never fires. `--log-opts ""` would
        # make gitleaks scan every ref - the opposite of the intended restriction.
        calls = _fake_gitleaks(monkeypatch, branch="")
        assert gitleaks.run(_cfg()) == 0
        cmd = next(c for c in calls if c and c[0] == "gitleaks")
        assert cmd[cmd.index("--log-opts") + 1] == "HEAD"


class TestKnownEvasions:
    """Pin the guard's documented SCOPE - what it deliberately does not catch.

    Each config here keeps a ruleset (so the guard passes it) while allowlisting
    every possible hit, which makes gitleaks report "no leaks found" anyway.
    Verified against the real binary during review of #64. Catching these needs
    evaluating the allowlist against the repo rather than reading TOML - #67.
    """

    def _write(self, tmp_path: Path, body: str) -> str:
        path = tmp_path / ".gitleaks.toml"
        path.write_text(body, encoding="utf-8", newline="\n")
        return str(path)

    @pytest.mark.parametrize(
        ("label", "body"),
        [
            (
                "catch-all allowlist paths",
                "[extend]\nuseDefault = true\n[allowlist]\npaths = ['''.*''']\n",
            ),
            (
                "catch-all allowlist regexes",
                "[extend]\nuseDefault = true\n[allowlist]\nregexes = ['''.*''']\n",
            ),
            (
                "disabledRules",
                '[extend]\nuseDefault = true\ndisabledRules = ["github-pat"]\n',
            ),
        ],
    )
    def test_broad_allowlist_is_not_caught(
        self, tmp_path: Path, label: str, body: str
    ) -> None:
        # NOT a bug being enshrined - a boundary being stated. The guard speaks
        # only about the rule SOURCE, and its notice must not claim more.
        assert gitleaks._declares_no_ruleset(self._write(tmp_path, body)) is False, (
            label
        )


class TestEnvConfigOverride:
    """GITLEAKS_CONFIG* must never apply unannounced."""

    @pytest.fixture(autouse=True)
    def _no_repo_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gitleaks, "_find_config", lambda: None)
        for var in ("GITLEAKS_CONFIG", "GITLEAKS_CONFIG_TOML"):
            monkeypatch.delenv(var, raising=False)

    def test_no_override_is_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert gitleaks._env_config_override() is None

    @pytest.mark.parametrize("var", ["GITLEAKS_CONFIG", "GITLEAKS_CONFIG_TOML"])
    def test_override_is_detected(
        self, monkeypatch: pytest.MonkeyPatch, var: str
    ) -> None:
        monkeypatch.setenv(var, "whatever")
        assert gitleaks._env_config_override() == var

    def test_override_warns_when_no_repo_config(
        self, captured: list[list[str]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # With no repo config there is no --config to beat the env var, so the
        # scan runs under a config hyperi-ci never saw. It must say so.
        monkeypatch.setenv("GITLEAKS_CONFIG", "/tmp/blind.toml")
        warns: list[str] = []
        monkeypatch.setattr(gitleaks, "warn", lambda m: warns.append(m))
        assert gitleaks.run(_cfg()) == 0
        assert any("GITLEAKS_CONFIG" in w for w in warns), warns

    def test_repo_config_beats_env_and_is_passed(
        self, captured: list[list[str]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # --config wins over GITLEAKS_CONFIG in gitleaks' own precedence, so
        # when we have a config to pass there is nothing to warn about.
        monkeypatch.setenv("GITLEAKS_CONFIG", "/tmp/blind.toml")
        monkeypatch.setattr(gitleaks, "_find_config", lambda: ".gitleaks.toml")
        monkeypatch.setattr(gitleaks, "_declares_no_ruleset", lambda _cfg: False)
        assert gitleaks.run(_cfg()) == 0
        cmd = _scan_cmd(captured)
        assert cmd[cmd.index("--config") + 1] == ".gitleaks.toml"


class TestShortCircuits:
    def test_disabled_returns_early(self, captured: list[list[str]]) -> None:
        cfg = _cfg({"quality": {"gitleaks": "disabled"}})
        assert gitleaks.run(cfg) == 0
        assert not captured

    def test_force_skip_returns_early(
        self, captured: list[list[str]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_SKIP, "gitleaks")
        assert gitleaks.run(_cfg()) == 0
        assert not captured


def test_pinned_version_matches_versions_yaml() -> None:
    """The source constant must track the tools SSoT in config/versions.yaml.

    The pin drifted ~9 months (v8.21.2 vs v8.30.1) precisely because it lived
    outside the SSoT that update-versions.py maintains. This fails loudly if
    the two ever diverge again.
    """
    import yaml

    root = Path(__file__).resolve().parents[2]
    versions = yaml.safe_load((root / "config" / "versions.yaml").read_text("utf-8"))
    assert versions["tools"]["gitleaks"]["version"] == gitleaks._GITLEAKS_VERSION
