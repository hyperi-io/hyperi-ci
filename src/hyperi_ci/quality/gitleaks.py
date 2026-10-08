# Project:   HyperI CI
# File:      src/hyperi_ci/quality/gitleaks.py
# Purpose:   Gitleaks secret scanning (cross-language)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Gitleaks secret scanning of the git history, before the language checks."""

import json
import os
import subprocess
import tempfile
import tomllib
from enum import StrEnum
from pathlib import Path

from hyperi_ci.common import error, info, run_cmd, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_tool_mode
from hyperi_ci.native_tools import ci_binary
from hyperi_ci.tools import missing_tool, missing_tool_notice
from hyperi_ci.versions import tool_version


def _supports_git_subcommand() -> bool:
    """Return whether the gitleaks on PATH has the `git` subcommand.

    Probed, because a distro build's `gitleaks version` prints no number.
    """
    try:
        probe = run_cmd(["gitleaks", "git", "--help"], check=False, capture=True)
    except OSError:
        return False
    return probe.returncode == 0


def _report_unusable(mode: str) -> int:
    """Report a gitleaks too old to run this scan as a tool problem, not a finding.

    Returns what a failed scan returns in this mode: 0 for ``warn``, else 1.
    """
    notice = "\n".join(
        (
            "gitleaks: no scan ran - the installed build has no `git` "
            "subcommand. This is a tool problem, not a finding.",
            "  needed: 8.19.0 or newer, which replaced `detect` with `git`;"
            f" hyperi-ci pins {tool_version('gitleaks')}.",
            "  Ubuntu universe ships 8.16.0, below that floor.",
            f"  {missing_tool_notice('gitleaks', head='`gitleaks` is installed but too old')}",
        )
    )
    if mode == "warn":
        warn(f"  {notice}")
        return 0
    error(f"  {notice}")
    return 1


def _find_config() -> str | None:
    """Find gitleaks config file in project."""
    for path in (".gitleaks.toml", "ci/.gitleaks.toml"):
        if Path(path).exists():
            return path
    return None


# gitleaks' config precedence: `--config`, then these two, then
# `(target path)/.gitleaks.toml`. GITLEAKS_CONFIG names a PATH;
# GITLEAKS_CONFIG_TOML carries the TOML itself.
_ENV_CONFIG_VARS = ("GITLEAKS_CONFIG", "GITLEAKS_CONFIG_TOML")


def _parse_config(text: str) -> dict[str, object] | None:
    """Parse a gitleaks config, lower-casing the top-level keys as viper does.

    Returns None when the text is not TOML, which gitleaks reports itself.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    return {str(k).lower(): v for k, v in data.items()}


def _read_config(cfg_path: str) -> dict[str, object] | None:
    """Parse the config at ``cfg_path``, or None when it cannot be read."""
    try:
        text = Path(cfg_path).read_text(encoding="utf-8")
    except OSError:
        return None
    return _parse_config(text)


def _extend_table(folded: dict[str, object]) -> dict[str, object]:
    """Return the `[extend]` table with its keys lower-cased, else empty."""
    extend = folded.get("extend") or {}
    if not isinstance(extend, dict):
        return {}
    return {str(k).lower(): v for k, v in extend.items()}


def _declares_no_ruleset(cfg_path: str) -> bool:
    """Return whether this config gives gitleaks no source of rules (issue #64).

    A config with neither `[[rules]]` nor `[extend]` replaces the defaults with
    nothing, and every scan passes. A source is `[[rules]]`, `[extend]
    useDefault` or `[extend] path`. A config that keeps rules but allowlists
    every hit passes here; :func:`_canary_rules_found` catches that. An
    unreadable or unparseable config returns False.
    """
    folded = _read_config(cfg_path)
    if folded is None:
        return False
    if folded.get("rules"):
        return False

    # `extend.url` loads nothing: extendURL() is an empty stub as of gitleaks 8.30.1.
    extend = _extend_table(folded)
    return not (extend.get("usedefault") or extend.get("path"))


def _report_no_ruleset(cfg: str, mode: str) -> int:
    """Emit the rule-less-config notice; return 1 in ``blocking`` mode, else 0."""
    notice = "\n".join(
        (
            f"gitleaks: {cfg} defines no rules and does not extend the "
            "defaults - every scan will pass regardless of content.",
            "  help: add this stanza to scan with the default ruleset:",
            "    [extend]",
            "    useDefault = true",
            "  docs: docs/quality-gate-tools.md#gitleaks-config",
        )
    )
    if mode == "blocking":
        error(f"  {notice}")
        error("  Refusing to report success from a rule-less scan.")
        return 1
    warn(f"  {notice}")
    return 0


# No directory and no extension, so only a catch-all `paths` allowlist matches it.
_CANARY_FILENAME = "hyperi-ci-secret-scan-canary"

# Synthetic values for default rules, re-proved by tests/unit/test_gitleaks.py.
# Only rule types GitHub push protection accepts as fakes are used. The
# `gitleaks:allow` marker stays outside the string, or it would suppress the canary.
_CANARY_SECRETS: dict[str, str] = {
    "github-pat": 'github_pat = "ghp_CANARYnotARealTokenR7kQ2xVm9Zb4Ld812"',  # gitleaks:allow nosemgrep
    "aws-access-token": 'aws_access_token = "AKIA2XVM5QZ7KDFT3WYB"',  # gitleaks:allow nosemgrep
}

_CANARY_HEADER = (
    "# Synthetic credentials planted by hyperi-ci to prove this gitleaks config\n"
    "# can still report a secret. Every value below is fake.\n"
)


def _canary_body() -> str:
    """Build the fixture: the header plus one planted secret per canary rule."""
    return _CANARY_HEADER + "".join(f"{line}\n" for line in _CANARY_SECRETS.values())


def _canary_rules_found(cfg: str | None) -> set[str] | None:
    """Return the rule ids of the planted canary secrets this config still finds.

    Any allowlist, ``disabledRules`` entry or stopword that hides a secret shows
    up as a missing id. The fixture is scanned with `dir` from its own temporary
    directory, so the repo's `.gitleaksignore` does not apply.

    Args:
        cfg: Repo config path to scan through, or None to leave the choice to
            gitleaks' precedence (GITLEAKS_CONFIG*, else the built-in defaults).

    Returns:
        The rule ids reported against the fixture, or None when the canary could
        not run.

    """
    with tempfile.TemporaryDirectory(prefix="hyperi-ci-gitleaks-") as tmp:
        fixture = Path(tmp) / _CANARY_FILENAME
        # The planted secrets are synthetic and written in the clear on purpose.
        fixture.write_text(  # codeql[py/clear-text-storage-sensitive-data]
            _canary_body(), encoding="utf-8", newline="\n"
        )
        report = Path(tmp) / "canary-report.json"
        cmd = [
            "gitleaks",
            "dir",
            _CANARY_FILENAME,
            "--no-banner",
            # Findings are expected; the report file carries the answer.
            "--exit-code",
            "0",
            "--report-format",
            "json",
            "--report-path",
            report.name,
        ]
        if cfg:
            cmd.extend(["--config", str(Path(cfg).resolve())])
        try:
            probe = run_cmd(cmd, check=False, capture=True, cwd=tmp)
        except OSError:
            return None
        if probe.returncode != 0 or not report.exists():
            return None
        try:
            findings = json.loads(report.read_text(encoding="utf-8") or "[]")
        except (OSError, json.JSONDecodeError):
            return None

    if not isinstance(findings, list):
        return None
    return {str(f.get("RuleID")) for f in findings if isinstance(f, dict)}


class _RuleSource(StrEnum):
    """Where the rules a scan runs under come from, so far as the TOML says."""

    DEFAULTS = "defaults"
    OWN = "own"
    UNKNOWN = "unknown"


def _rules_config(cfg: str | None) -> dict[str, object] | None:
    """Return the config gitleaks will load for this scan, keys lower-cased.

    The repo config wins, else GITLEAKS_CONFIG, else GITLEAKS_CONFIG_TOML.
    Returns an empty dict when there is none, so the built-in ruleset applies,
    and None when the config cannot be read.
    """
    if cfg:
        return _read_config(cfg)
    if path := os.environ.get("GITLEAKS_CONFIG"):
        return _read_config(path)
    if inline := os.environ.get("GITLEAKS_CONFIG_TOML"):
        return _parse_config(inline)
    return {}


def _canary_rule_source(cfg: str | None) -> _RuleSource:
    """Return where this config's rules come from, so far as the TOML says.

    The canary plants default rules, so only under ``DEFAULTS`` does a miss mean
    the config suppressed one.
    """
    folded = _rules_config(cfg)
    if folded is None:
        return _RuleSource.UNKNOWN
    if not folded:
        return _RuleSource.DEFAULTS
    if _extend_table(folded).get("usedefault"):
        return _RuleSource.DEFAULTS
    return _RuleSource.OWN


def _report_inconclusive_canary(
    source: str, rules_from: _RuleSource, suppressed: str
) -> int:
    """Warn that the canary could not evaluate this config, and why; return 0."""
    reason = (
        "it brings its own rules instead of extending the defaults"
        if rules_from is _RuleSource.OWN
        else "hyperi-ci cannot read which rules it brings"
    )
    notice = "\n".join(
        (
            f"gitleaks: the canary could not evaluate {source} - {reason}, so a "
            f"miss on {suppressed} says nothing about the scan.",
            "  Neither a pass nor a failure: whether a scan under this config can "
            "report a secret is unknown.",
            "  help: `[extend] useDefault = true` puts the default ruleset - and "
            "the canary with it - back in scope.",
            "  docs: docs/quality-gate-tools.md#gitleaks-config",
        )
    )
    warn(f"  {notice}")
    return 0


def _report_canary(cfg: str | None, mode: str) -> int:
    """Emit the canary notice; return 1 when it must block.

    All secrets found is a pass. A miss under the default ruleset means the
    config suppressed them, which blocks in ``blocking`` mode. A miss under a
    config that never carried those rules is inconclusive and only warns.
    """
    source = cfg or _env_config_override() or "the default gitleaks ruleset"
    found = _canary_rules_found(cfg)
    if found is None:
        warn(
            f"  gitleaks: the canary did not run against {source} - "
            "the scan below is unverified."
        )
        return 0

    missing = sorted(set(_CANARY_SECRETS) - found)
    if not missing:
        return 0

    suppressed = ", ".join(missing)
    rules_from = _canary_rule_source(cfg)
    if rules_from is not _RuleSource.DEFAULTS:
        return _report_inconclusive_canary(source, rules_from, suppressed)

    headline = (
        f"gitleaks: {source} suppresses every planted canary secret - a scan "
        "under it cannot report ANY secret."
        if len(missing) == len(_CANARY_SECRETS)
        else (
            f"gitleaks: {source} suppresses part of the canary - a scan under "
            f"it cannot report {suppressed}."
        )
    )
    notice = "\n".join(
        (
            headline,
            "  The canary is a synthetic fixture carrying one planted secret per "
            "rule - a config that scans reports all of them.",
            "  help: narrow `[allowlist] paths` / `regexes` to the real false "
            f"positives, and drop any `disabledRules` entry covering {suppressed}.",
            "  docs: docs/quality-gate-tools.md#gitleaks-config",
        )
    )
    if mode == "blocking":
        error(f"  {notice}")
        error("  Refusing to report success from a blinded scan.")
        return 1
    warn(f"  {notice}")
    return 0


def _env_config_override() -> str | None:
    """Return the first GITLEAKS_CONFIG* env var that is set, else None."""
    for name in _ENV_CONFIG_VARS:
        if os.environ.get(name):
            return name
    return None


def run(config: CIConfig) -> int:
    """Run gitleaks secret scanning.

    Args:
        config: Merged CI configuration.

    Returns:
        Exit code (0 = success).

    Raises:
        GateReasonRequiredError: The gate is turned below the shipped
            ``blocking`` with no reason beside it.

    """
    # The shared resolver applies --strict, which the rule-less guard depends on.
    mode = resolve_tool_mode("gitleaks", config, default="blocking")
    if mode == "disabled":
        info("  gitleaks: disabled")
        return 0

    # ci_binary puts an installed gitleaks on PATH, where every call below runs it.
    if ci_binary("gitleaks") is None:
        return missing_tool("gitleaks", mode)

    # `ci_binary` accepts any gitleaks on PATH, however old.
    if not _supports_git_subcommand():
        return _report_unusable(mode)

    # `git` replaced the deprecated `detect`, and takes the path positionally.
    cmd: list[str] = ["gitleaks", "git", ".", "--verbose"]

    # Current branch only. A detached HEAD prints nothing with exit 0, and an
    # empty `--log-opts` would scan every ref.
    result = run_cmd(["git", "branch", "--show-current"], check=False, capture=True)
    branch = result.stdout.strip() or "HEAD"
    cmd.extend(["--log-opts", branch])

    # An explicit --config stops gitleaks choosing a config by target path.
    cfg = _find_config()
    rule_less = False
    if cfg:
        rule_less = _declares_no_ruleset(cfg)
        if rule_less and _report_no_ruleset(cfg, mode) != 0:
            return 1
        cmd.extend(["--config", cfg])
    elif env_var := _env_config_override():
        # An org-level variable here could blind every repo, so it is announced.
        warn(
            f"  gitleaks: {env_var} is set and no repo .gitleaks.toml exists - "
            "the scan is running with a config hyperi-ci did not vet."
        )
        warn("  Prefer a committed .gitleaks.toml so the config is reviewable.")

    # Skipped for a rule-less config, whose notice is more precise.
    if not rule_less and _report_canary(cfg, mode) != 0:
        return 1

    env = dict(os.environ)
    gitleaks_key = os.environ.get("GITLEAKS_GH_ACTIONS_KEY")
    if gitleaks_key:
        env["GITLEAKS_LICENSE"] = gitleaks_key

    info("  gitleaks: scanning for secrets...")
    scan = subprocess.run(cmd, env=env)

    if scan.returncode == 0:
        success("  gitleaks: no secrets detected")
        return 0

    if mode == "warn":
        warn("  gitleaks: secrets detected (non-blocking)")
        return 0

    error("  gitleaks: secrets detected in repository!")
    error("  Review output above and remove/rotate exposed secrets.")
    error("  For false positives, add them to .gitleaks.toml")
    return 1
