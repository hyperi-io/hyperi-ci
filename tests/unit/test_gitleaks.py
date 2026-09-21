# Project:   HyperI CI
# File:      tests/unit/test_gitleaks.py
# Purpose:   Tests for the dispatch-level gitleaks quality module
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for the centralised (dispatch-level) gitleaks module.

Three things are pinned down here:

* the **subcommand** (#64). `detect` is deprecated - gone from `--help` as of
  gitleaks 8.30.1, honoured only for back-compat. The replacement is not a
  rename: `git` takes the repo path POSITIONALLY where `detect` took it via
  `--source`, so a half-done migration is a plausible regression.
* the **rule-less-config guard** (#64). A repo `.gitleaks.toml` with allowlists
  but no `[[rules]]` and no `[extend]` leaves gitleaks with an empty ruleset: it
  reads every byte, matches nothing, and exits 0. A `blocking` gate silently
  becomes a no-op reporting success. The guard must refuse that, and its
  severity follows the gate's own mode.
* the **canary** (#67). A config can keep a ruleset and still allowlist every
  hit, which no amount of TOML reading decides. The canary scans a planted
  secret through the config and asks whether it comes back.

The canary tests run the REAL gitleaks binary against real files, because the
thing under test is what gitleaks does with a config, which a stub cannot tell
us. They skip when it is absent: the binary lives in the quality stage's
environment, not the test job's, and the gate's own refusal to pass without it
is enforced in `gitleaks.py` rather than here. Only the severity wiring is
stubbed, matching how the rule-less guard is tested above it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.quality import gitleaks

_SKIP = "HYPERCI_QUALITY_SKIP"
_STRICT = "HYPERCI_QUALITY_STRICT"
_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def real_gitleaks() -> str:
    """The gitleaks binary, or a skip when it is not installed."""
    found = shutil.which("gitleaks")
    if not found:
        pytest.skip("gitleaks is not installed")
    return found


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
    canary_found: set[str] | None = None,
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
    # Stubbed alongside the install check, and for the same reason: the probe
    # shells out to `gitleaks git --help`, which would otherwise be recorded as
    # the first gitleaks call and stand in for the scan in _scan_cmd.
    monkeypatch.setattr(gitleaks, "_supports_git_subcommand", lambda: True)
    # The canary shells out too, and its `gitleaks dir` call would otherwise be
    # recorded ahead of the scan; it is exercised against the real binary in
    # TestCanary, so here it reports a healthy config unless a test says else.
    rules = set(gitleaks._CANARY_SECRETS) if canary_found is None else canary_found
    monkeypatch.setattr(gitleaks, "_canary_rules_found", lambda _cfg: rules)
    monkeypatch.setattr(gitleaks, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(gitleaks.subprocess, "run", fake_run)
    return calls


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """A clean scan (gitleaks exits 0) with every command recorded."""
    return _fake_gitleaks(monkeypatch)


def _scan_cmd(calls: list[list[str]]) -> list[str]:
    return next(c for c in calls if c and c[0] == "gitleaks")


def _write_config(tmp_path: Path, body: str) -> str:
    path = tmp_path / ".gitleaks.toml"
    path.write_text(body, encoding="utf-8", newline="\n")
    return str(path)


class TestDeclaresNoRuleset:
    """`_declares_no_ruleset` decides whether a config names any rule SOURCE."""

    def test_allowlists_without_rules_or_extend_is_blind(self, tmp_path: Path) -> None:
        # The exact shape reported in #64.
        cfg = _write_config(
            tmp_path,
            "[[allowlists]]\ndescription = \"fixtures\"\npaths = ['''testdata/''']\n",
        )
        assert gitleaks._declares_no_ruleset(cfg) is True

    def test_extend_use_default_is_not_blind(self, tmp_path: Path) -> None:
        cfg = _write_config(tmp_path, "[extend]\nuseDefault = true\n")
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
        assert gitleaks._declares_no_ruleset(_write_config(tmp_path, body)) is False

    @pytest.mark.parametrize("body", ["[[Rules]]\nid = 'x'\nregex = '''s'''\n"])
    def test_rules_key_is_case_insensitive(self, tmp_path: Path, body: str) -> None:
        assert gitleaks._declares_no_ruleset(_write_config(tmp_path, body)) is False

    @pytest.mark.parametrize("key", ["path", "Path"])
    def test_extend_path_is_a_rule_source(self, tmp_path: Path, key: str) -> None:
        cfg = _write_config(tmp_path, f'[extend]\n{key} = "somewhere.toml"\n')
        assert gitleaks._declares_no_ruleset(cfg) is False

    def test_extend_url_is_blind_because_gitleaks_ignores_it(
        self, tmp_path: Path
    ) -> None:
        # NOT an oversight. gitleaks' extendURL() is an empty `// TODO` stub as
        # of 8.30.1 and nothing reads Extend.URL, so a url-only extend loads
        # ZERO rules and reports "no leaks found" on a repo full of secrets.
        # Blessing it would reintroduce the exact #64 failure this guard exists
        # to catch. If upstream ever implements it, this test fails - on purpose.
        cfg = _write_config(tmp_path, '[extend]\nurl = "https://example.test/x.toml"\n')
        assert gitleaks._declares_no_ruleset(cfg) is True

    def test_own_rules_are_not_blind(self, tmp_path: Path) -> None:
        cfg = _write_config(
            tmp_path,
            "[[rules]]\nid = \"x\"\ndescription = \"x\"\nregex = '''secret'''\n",
        )
        assert gitleaks._declares_no_ruleset(cfg) is False

    def test_extend_present_but_empty_is_blind(self, tmp_path: Path) -> None:
        # `[extend]` with nothing in it pulls in no ruleset.
        cfg = _write_config(tmp_path, "[extend]\n")
        assert gitleaks._declares_no_ruleset(cfg) is True

    def test_unparseable_config_is_not_flagged(self, tmp_path: Path) -> None:
        # gitleaks reports malformed TOML better than we can - don't turn a
        # syntax error into a spurious "your gate is blind".
        cfg = _write_config(tmp_path, "this is not = = toml\n")
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


class TestUnusableBuild:
    """An installed gitleaks too old to run the scan is a TOOL fault.

    Ubuntu universe ships 8.16.0, which predates the 8.19.0 split of `detect`
    into `git`/`dir`/`stdin`. `_install_gitleaks` accepts anything called
    gitleaks on PATH, so that build reached the scan, failed with
    `unknown command "git"`, and the non-zero exit was reported as
    "secrets detected in repository!".
    """

    @staticmethod
    def _too_old(monkeypatch: pytest.MonkeyPatch) -> list[str]:
        """Installed, but the `git` probe fails. Returns the messages emitted."""
        _fake_gitleaks(monkeypatch)
        monkeypatch.setattr(gitleaks, "_supports_git_subcommand", lambda: False)
        messages: list[str] = []
        monkeypatch.setattr(gitleaks, "error", messages.append)
        monkeypatch.setattr(gitleaks, "warn", messages.append)
        return messages

    def test_never_reported_as_a_finding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The whole point: do not send anyone hunting a secret that is not there."""
        messages = self._too_old(monkeypatch)
        gitleaks.run(_cfg({"quality": {"gitleaks": "blocking"}}))

        assert not any("secrets detected" in m for m in messages), messages
        assert any("no scan ran" in m for m in messages), messages

    def test_blocking_mode_still_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Exit code is unchanged from the old behaviour - only the reason moved."""
        self._too_old(monkeypatch)
        assert gitleaks.run(_cfg({"quality": {"gitleaks": "blocking"}})) == 1

    def test_warn_mode_still_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._too_old(monkeypatch)
        assert gitleaks.run(_cfg({"quality": {"gitleaks": "warn"}})) == 0

    def test_no_scan_is_attempted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Bailing before the scan is what keeps the exit code meaningful."""
        calls = _fake_gitleaks(monkeypatch)
        monkeypatch.setattr(gitleaks, "_supports_git_subcommand", lambda: False)
        gitleaks.run(_cfg({"quality": {"gitleaks": "blocking"}}))

        assert not [c for c in calls if c and c[0] == "gitleaks"], calls


class TestGitSubcommandProbe:
    """The probe itself, against the two exit codes real builds produce."""

    @pytest.mark.parametrize(
        ("returncode", "supported"),
        [(0, True), (1, False)],
        ids=["8.30.1-prints-help", "8.16.0-unknown-command"],
    )
    def test_probe_follows_the_exit_code(
        self, monkeypatch: pytest.MonkeyPatch, returncode: int, supported: bool
    ) -> None:
        def fake_run_cmd(
            cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess:
            assert cmd == ["gitleaks", "git", "--help"]
            return subprocess.CompletedProcess(cmd, returncode, "", "")

        monkeypatch.setattr(gitleaks, "run_cmd", fake_run_cmd)
        assert gitleaks._supports_git_subcommand() is supported

    def test_missing_binary_is_not_supported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """run_cmd does not catch OSError, and a probe must not raise out of run()."""

        def boom(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            raise FileNotFoundError(cmd[0])

        monkeypatch.setattr(gitleaks, "run_cmd", boom)
        assert gitleaks._supports_git_subcommand() is False


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


_ALLOWLIST_PATHS = "[extend]\nuseDefault = true\n[allowlist]\npaths = ['''.*''']\n"
_ALLOWLIST_REGEXES = "[extend]\nuseDefault = true\n[allowlist]\nregexes = ['''.*''']\n"
_DISABLED_RULES = '[extend]\nuseDefault = true\ndisabledRules = ["github-pat"]\n'

# The three shapes #67 measured against gitleaks 8.30.1, each of which reports
# "no leaks found" over a planted PAT while keeping a rule source.
_EVASIONS = [
    pytest.param(_ALLOWLIST_PATHS, id="catch-all-allowlist-paths"),
    pytest.param(_ALLOWLIST_REGEXES, id="catch-all-allowlist-regexes"),
    pytest.param(_DISABLED_RULES, id="disabled-rules"),
]


def _canary_misses(cfg: str | None) -> set[str]:
    """Planted rules this config fails to report; asserts the canary ran."""
    found = gitleaks._canary_rules_found(cfg)
    assert found is not None, "the canary did not run"
    return set(gitleaks._CANARY_SECRETS) - found


class TestCanary:
    """The canary, against the real binary: does this config still find a secret?"""

    def test_planted_secrets_all_trip_their_rule(self, real_gitleaks: str) -> None:
        # The fixture is evidence only while every planted value still matches
        # the rule it was chosen for, and gitleaks tightens rules between
        # releases (aws-access-token takes base32 alone, A-Z and 2-7).
        assert _canary_misses(None) == set()

    def test_repo_own_config_finds_the_whole_canary(self, real_gitleaks: str) -> None:
        # A canary a correct config cannot satisfy is a broken canary, so the
        # repo's real allowlist is run against it rather than assumed neutral.
        assert _canary_misses(str(_REPO_ROOT / ".gitleaks.toml")) == set()

    def test_suppression_markers_stay_out_of_the_fixture(self) -> None:
        # The planted values carry `gitleaks:allow nosemgrep` in the Python
        # comment beside them; inside the string they would ride into the
        # fixture and make the canary allowlist itself.
        body = gitleaks._canary_body()
        assert "gitleaks:allow" not in body
        assert "nosemgrep" not in body

    def test_this_module_does_not_trip_the_repo_gate(
        self, real_gitleaks: str, tmp_path: Path
    ) -> None:
        # The planted values are real enough for gitleaks to match, so the file
        # holding them would otherwise fail this repo's own blocking scan.
        report = tmp_path / "report.json"
        subprocess.run(
            [
                real_gitleaks,
                "dir",
                gitleaks.__file__,
                "--no-banner",
                "--exit-code",
                "0",
                "--config",
                str(_REPO_ROOT / ".gitleaks.toml"),
                "--report-format",
                "json",
                "--report-path",
                str(report),
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert json.loads(report.read_text(encoding="utf-8")) == []

    def test_sound_config_reports_nothing(
        self, real_gitleaks: str, tmp_path: Path
    ) -> None:
        cfg = _write_config(
            tmp_path,
            "[extend]\nuseDefault = true\n[allowlist]\npaths = ['''testdata/''']\n",
        )
        assert gitleaks._report_canary(cfg, "blocking") == 0

    def test_malformed_config_is_inconclusive(
        self, real_gitleaks: str, tmp_path: Path
    ) -> None:
        # gitleaks exits fatally on a config it cannot load, which says nothing
        # about whether the config blinds the scan.
        cfg = _write_config(tmp_path, "this is not = = toml\n")
        assert gitleaks._canary_rules_found(cfg) is None

    def test_inconclusive_canary_never_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gitleaks, "_canary_rules_found", lambda _cfg: None)
        assert gitleaks._report_canary("x.toml", "blocking") == 0


class TestKnownEvasions:
    """The three configs from #67, each blinding gitleaks a different way.

    Every one keeps a rule SOURCE, so `_declares_no_ruleset` passes them - that
    boundary is unchanged and still asserted here. The canary is what catches
    them, and it does so without knowing which of the three it is looking at.
    """

    @pytest.mark.parametrize("body", _EVASIONS)
    def test_rule_source_guard_still_passes_them(
        self, tmp_path: Path, body: str
    ) -> None:
        assert gitleaks._declares_no_ruleset(_write_config(tmp_path, body)) is False

    @pytest.mark.parametrize("body", _EVASIONS)
    def test_canary_catches_them(
        self, real_gitleaks: str, tmp_path: Path, body: str
    ) -> None:
        assert _canary_misses(_write_config(tmp_path, body))

    @pytest.mark.parametrize("body", _EVASIONS)
    def test_blocking_mode_refuses_the_scan(
        self, real_gitleaks: str, tmp_path: Path, body: str
    ) -> None:
        assert gitleaks._report_canary(_write_config(tmp_path, body), "blocking") == 1

    @pytest.mark.parametrize("body", _EVASIONS)
    def test_warn_mode_reports_and_proceeds(
        self, real_gitleaks: str, tmp_path: Path, body: str
    ) -> None:
        assert gitleaks._report_canary(_write_config(tmp_path, body), "warn") == 0

    @pytest.mark.parametrize("body", [_ALLOWLIST_PATHS, _ALLOWLIST_REGEXES])
    def test_catch_all_allowlists_suppress_everything(
        self, real_gitleaks: str, tmp_path: Path, body: str
    ) -> None:
        # Both allowlist shapes take out the whole ruleset rather than one rule,
        # which is what the "cannot report ANY secret" wording rests on.
        assert _canary_misses(_write_config(tmp_path, body)) == set(
            gitleaks._CANARY_SECRETS
        )

    def test_disabled_rules_names_only_the_rule_it_killed(
        self, real_gitleaks: str, tmp_path: Path
    ) -> None:
        cfg = _write_config(tmp_path, _DISABLED_RULES)
        assert _canary_misses(cfg) == {"github-pat"}


class TestCanaryWiring:
    """The canary's place in run(): it gates the scan, and mode sets severity."""

    @pytest.fixture(autouse=True)
    def _repo_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gitleaks, "_find_config", lambda: ".gitleaks.toml")
        monkeypatch.setattr(gitleaks, "_declares_no_ruleset", lambda _cfg: False)

    def test_blocking_mode_never_reaches_the_scan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _fake_gitleaks(monkeypatch, canary_found=set())
        cfg = _cfg({"quality": {"gitleaks": "blocking"}})
        assert gitleaks.run(cfg) == 1
        assert not [c for c in calls if c and c[0] == "gitleaks"]

    def test_warn_mode_proceeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _fake_gitleaks(monkeypatch, canary_found=set())
        cfg = _cfg({"quality": {"gitleaks": "warn"}})
        assert gitleaks.run(cfg) == 0
        assert _scan_cmd(calls)[1] == "git"

    def test_strict_upgrades_the_canary_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_gitleaks(monkeypatch, canary_found=set())
        monkeypatch.setenv(_STRICT, "1")
        assert gitleaks.run(_cfg({"quality": {"gitleaks": "warn"}})) == 1

    def test_rule_less_config_skips_the_canary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # _report_no_ruleset already named the same failure more precisely, and
        # in warn mode the pair would print two notices for one problem.
        _fake_gitleaks(monkeypatch, canary_found=set())
        monkeypatch.setattr(gitleaks, "_declares_no_ruleset", lambda _cfg: True)
        warns: list[str] = []
        monkeypatch.setattr(gitleaks, "warn", warns.append)
        assert gitleaks.run(_cfg({"quality": {"gitleaks": "warn"}})) == 0
        assert not [w for w in warns if "canary" in w], warns


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


def test_install_url_and_digest_come_from_the_ssot() -> None:
    """There is no source constant to drift from: the URL is built from the SSOT.

    The pin drifted ~9 months (v8.21.2 vs v8.30.1) because it lived in a copy.
    The copy is gone, so this asserts the install reads the real thing.
    """
    from hyperi_ci import versions as ssot

    assert ssot.tool_version("gitleaks") == "v8.30.1"
    assert len(ssot.tool_sha256("gitleaks", "x64")) == 64
