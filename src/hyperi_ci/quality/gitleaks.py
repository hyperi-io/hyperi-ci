# Project:   HyperI CI
# File:      src/hyperi_ci/quality/gitleaks.py
# Purpose:   Gitleaks secret scanning (cross-language)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Gitleaks secret scanning.

Scans repository git history for committed secrets. Runs before
language-specific quality checks on every project.

Ported from old CI: ci/scripts/core/gitleaks.sh
"""

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import tomllib
from enum import StrEnum
from pathlib import Path

from hyperi_ci.common import error, info, is_ci, run_cmd, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_cross_tool_mode
from hyperi_ci.quality.install import install_ci_binary
from hyperi_ci.tools import missing_tool_notice
from hyperi_ci.versions import tool_sha256, tool_version


def _install_gitleaks() -> bool:
    """Install the pinned gitleaks binary on a Linux CI runner.

    Returns:
        True if gitleaks is available after the install attempt.

    """
    if shutil.which("gitleaks"):
        return True

    if not is_ci():
        return False

    if sys.platform != "linux":
        warn("  gitleaks auto-install only supported on Linux CI")
        return False

    arch = "x64" if platform.machine() in ("x86_64", "AMD64") else "arm64"
    version = tool_version("gitleaks")
    url = (
        f"https://github.com/gitleaks/gitleaks/releases/download/"
        f"{version}/gitleaks_{version.lstrip('v')}_linux_{arch}.tar.gz"
    )
    return (
        install_ci_binary(
            "gitleaks",
            url,
            tar_member="gitleaks",
            expected_sha256=tool_sha256("gitleaks", arch),
        )
        is not None
    )


def _supports_git_subcommand() -> bool:
    """Whether the gitleaks on PATH understands the `git` subcommand.

    Probed rather than parsed from `gitleaks version`, which prints
    "version is set by build process" on a distro build - no number to compare.
    """
    try:
        probe = run_cmd(["gitleaks", "git", "--help"], check=False, capture=True)
    except OSError:
        return False
    return probe.returncode == 0


def _report_unusable(mode: str) -> int:
    """Report a gitleaks that cannot run this scan, and never as a finding.

    gitleaks exits non-zero for a usage error as well as for a leak, so the
    exit code alone cannot tell "no scan ran" from "your repo has a secret".
    Returns what the scan-failure path returns for this mode, so the outcome is
    unchanged and only the reason differs.
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
    """Parse a gitleaks config, folding the top-level keys the way viper does.

    Case is folded once here rather than per caller: `UseDefault`, `[[Rules]]`
    and `[Extend]` all work in a real config, and a half-done fold reads a
    WORKING config as blind. None means the text is not TOML - gitleaks reports
    that better than we can, and nothing here may turn a syntax error into a
    claim about the scan.
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
    """The `[extend]` table with its keys folded, or empty when there is none."""
    extend = folded.get("extend") or {}
    if not isinstance(extend, dict):
        return {}
    return {str(k).lower(): v for k, v in extend.items()}


def _declares_no_ruleset(cfg_path: str) -> bool:
    """Report whether this config gives gitleaks NO SOURCE OF RULES at all.

    A `.gitleaks.toml` that declares allowlists but neither `[[rules]]` nor
    `[extend]` does not narrow the default rules - it REPLACES them with
    nothing. gitleaks then reads every byte, matches none of them, and exits 0
    with "no leaks found". A `blocking` gate silently becomes a no-op that
    reports success, indistinguishable from a clean repo - the worst failure
    mode a secret scanner has. This is the shape reported in #64.

    A rule source means `[[rules]]`, `[extend] useDefault`, or `[extend] path`.
    NOT `[extend] url` - gitleaks' extendURL() is an empty `// TODO` stub in
    8.30.1 and Extend.URL is a struct field nothing reads, so a url-only extend
    loads nothing and scans blind. Treat it as no source.

    SCOPE - this is deliberately narrow, and is NOT a general "is the scan
    blind?" oracle. A config can still neuter itself while passing here:

        [extend]
        useDefault = true
        [allowlist]
        paths = ['''.*''']          # or regexes, or a broad disabledRules

    all of which keep a ruleset but allowlist every hit, and all of which
    report "no leaks found". Those are the canary's job (`_canary_rules_found`),
    which measures the config against a planted secret instead of reading the
    TOML. Claiming more than this check delivers would be its own silent
    failure, so the notice only speaks about the rule SOURCE.

    Unparseable/unreadable configs return False: gitleaks itself will complain
    with a better message than we can, and we must not turn a malformed file
    into a spurious "your gate is blind" claim.
    """
    folded = _read_config(cfg_path)
    if folded is None:
        return False
    if folded.get("rules"):
        return False

    # Only `useDefault` and `path` pull in rules. `extend.url` deliberately does
    # NOT count: gitleaks' extendURL() is an empty `// TODO` stub as of 8.30.1
    # and nothing reads Extend.URL, so a url-only extend silently loads zero
    # rules - the very #64 failure this guard exists to catch.
    extend = _extend_table(folded)
    return not (extend.get("usedefault") or extend.get("path"))


def _report_no_ruleset(cfg: str, mode: str) -> int:
    """Emit the rule-less-config notice. Returns 1 when it must block.

    Severity follows the gate's own mode: a repo that asked for `blocking` and
    then hands gitleaks nothing to match on is not a passing repo, it is an
    unscanned one.
    """
    notice = "\n".join(
        (
            f"gitleaks: {cfg} defines no rules and does not extend the "
            "defaults - every scan will pass regardless of content.",
            "  help: add this stanza to scan with the default ruleset:",
            "    [extend]",
            "    useDefault = true",
            "  docs: docs/quality-gate.md#gitleaks-config",
        )
    )
    if mode == "blocking":
        error(f"  {notice}")
        error("  Refusing to report success from a rule-less scan.")
        return 1
    warn(f"  {notice}")
    return 0


# A bare filename, no directory part and no extension: a `paths` allowlist aimed
# at real repo content (`testdata/`, `vendor/`, `\.lock$`) cannot reach it
# without being a catch-all.
_CANARY_FILENAME = "hyperi-ci-secret-scan-canary"

# One planted secret per default rule, drawn from credential types a repo has no
# cause to put in `disabledRules`. Every value is synthetic and every one is
# re-proved against the installed binary by tests/unit/test_gitleaks.py.
#
# The set is two rules rather than ten because a planted value has to SURVIVE
# living in a git repo: a well-formed Slack or Stripe token is rejected outright
# by GitHub push protection, and the fix for that would be to structure the
# source so a scanner cannot read it - the very thing this guard exists to
# catch. A GitHub PAT and an AWS key id pass because a real one of either needs
# a checksum or a paired secret that these do not carry.
#
# `gitleaks:allow nosemgrep` keeps the planted values out of this repo's own
# secret gate. The marker stays in the Python comment - inside the string it
# would travel into the fixture and suppress the canary itself.
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
    """Report which planted canary secrets this config still finds.

    Answers what reading the TOML cannot: would this config, as gitleaks applies
    it, report a secret at all? A catch-all `[allowlist] paths`, a catch-all
    `regexes`, a `disabledRules` entry, a stopword list and every future spelling
    of the same mistake all surface identically - the planted secret goes
    unreported - so no pattern has to be enumerated in advance.

    The fixture is scanned with `dir` from its own temporary directory, which
    makes the path an allowlist is matched against the bare filename and leaves
    the repo's `.gitleaksignore` out of scope. Config selection mirrors the real
    scan: `--config` where the repo has one, gitleaks' own precedence otherwise.

    Args:
        cfg: Repo config path to scan through, or None to leave the choice to
            gitleaks' precedence (GITLEAKS_CONFIG*, else the built-in defaults).

    Returns:
        The rule ids reported against the fixture, or None when the canary could
        not run. A gitleaks that errors out proves nothing about the config, so
        it must not become a "your gate is blind" claim.

    """
    with tempfile.TemporaryDirectory(prefix="hyperi-ci-gitleaks-") as tmp:
        fixture = Path(tmp) / _CANARY_FILENAME
        # The planted secret is synthetic and exists to be found by a scanner, so
        # writing it in the clear is the behaviour under test rather than a leak.
        fixture.write_text(  # codeql[py/clear-text-storage-sensitive-data]
            _canary_body(), encoding="utf-8", newline="\n"
        )
        report = Path(tmp) / "canary-report.json"
        cmd = [
            "gitleaks",
            "dir",
            _CANARY_FILENAME,
            "--no-banner",
            # Findings are the expected outcome, so `--exit-code 0` keeps a leak
            # from reading as a failed run - the report file carries the answer.
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
    """The config gitleaks will load for this scan, folded.

    An empty dict means there is no config anywhere, so gitleaks uses its own
    ruleset. None means a config exists and could not be read. `--config` beats
    the env vars in gitleaks' precedence, so a repo config answers first; with
    no repo config the env vars ARE the config and get read rather than written
    off, or an org-level override could blind every repo and only ever be
    reported as inconclusive.
    """
    if cfg:
        return _read_config(cfg)
    if path := os.environ.get("GITLEAKS_CONFIG"):
        return _read_config(path)
    if inline := os.environ.get("GITLEAKS_CONFIG_TOML"):
        return _parse_config(inline)
    return {}


def _canary_rule_source(cfg: str | None) -> _RuleSource:
    """Report whether the canary's planted rules were ever in this config's scope.

    The canary plants `github-pat` and `aws-access-token`, which are gitleaks'
    OWN rules. A config extending the defaults therefore carries them, so a miss
    there means the config suppressed them. A config bringing only its own
    `[[rules]]`, or extending a path whose contents are not read here, may never
    have carried them at all - and a miss then measures the canary's choice of
    fixture, not the config.
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
    """Say the canary could not evaluate this config, and why. Never blocks.

    The third outcome. Failing here would hard-fail a repo whose config is fine
    but narrow; staying silent would report the canary's own blind spot as a
    pass. Both destroy the same information, so it says which.
    """
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
            "  docs: docs/quality-gate.md#gitleaks-config",
        )
    )
    warn(f"  {notice}")
    return 0


def _report_canary(cfg: str | None, mode: str) -> int:
    """Emit the canary notice. Returns 1 when it must block.

    Three outcomes, not two. The planted secrets come back and the config can
    see. They do not come back from a config extending the DEFAULT ruleset,
    where they live, so it suppressed them - and severity follows the gate's own
    mode, as the rule-less notice does: a repo that asked for `blocking` and
    hands gitleaks a config that cannot report a planted GitHub PAT is not a
    passing repo, it is an unscanned one. Or they do not come back from a config
    whose rules never carried them, where the canary has learned nothing and
    must not pass judgement either way.
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
            "  docs: docs/quality-gate.md#gitleaks-config",
        )
    )
    if mode == "blocking":
        error(f"  {notice}")
        error("  Refusing to report success from a blinded scan.")
        return 1
    warn(f"  {notice}")
    return 0


def _env_config_override() -> str | None:
    """Name the GITLEAKS_CONFIG* env var in play, if any.

    We always beat the env vars WHEN the repo has a config to pass. When it does
    not, they silently take over and can blind the scan - so they must be
    surfaced rather than ignored. Precedence is spelled out at
    :data:`_ENV_CONFIG_VARS`.
    """
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

    """
    # The shared resolver applies --strict (warn -> blocking), which the
    # rule-less guard rides on: without it a developer who asked for strict got
    # a green "no secrets detected" out of an empty ruleset.
    mode = resolve_cross_tool_mode(config, "gitleaks", "blocking")
    if mode == "disabled":
        info("  gitleaks: disabled")
        return 0

    if not _install_gitleaks():
        if is_ci():
            if mode == "blocking":
                error("  gitleaks: not installed (required)")
                return 1
            warn("  gitleaks: not installed (skipping)")
            return 0
        # tools.py is the SSoT for install guidance - don't restate it here.
        # The hand-rolled copy had drifted into recommending
        # `go install ...@latest`: unpinned AND compiled from source, i.e. the
        # exact pattern the rest of this change removed.
        warn("  gitleaks: skipping secret scanning")
        warn(f"  {missing_tool_notice('gitleaks')}")
        return 0

    # `_install_gitleaks` is satisfied by anything named gitleaks on PATH, so
    # the build still has to be checked before the scan is built around `git`.
    if not _supports_git_subcommand():
        return _report_unusable(mode)

    # Scan git history. `git` supersedes the deprecated `detect` subcommand
    # (gone from --help as of 8.30.1, still honoured for back-compat); the repo
    # path is positional here, where `detect` took it via --source.
    cmd: list[str] = ["gitleaks", "git", ".", "--verbose"]

    # Restrict to current branch to avoid scanning unmerged branches. On a
    # detached HEAD `git branch --show-current` exits 0 with EMPTY stdout, so
    # test the output, not the exit code: `--log-opts ""` makes gitleaks scan
    # every ref, which is the opposite of the restriction intended here.
    result = run_cmd(["git", "branch", "--show-current"], check=False, capture=True)
    branch = result.stdout.strip() or "HEAD"
    cmd.extend(["--log-opts", branch])

    # Use custom config if present. Passing --config explicitly also pins down
    # which file gitleaks uses: left to itself it auto-discovers
    # `(target path)/.gitleaks.toml`, so the same repo scans differently
    # depending on the path you point it at.
    cfg = _find_config()
    rule_less = False
    if cfg:
        rule_less = _declares_no_ruleset(cfg)
        if rule_less and _report_no_ruleset(cfg, mode) != 0:
            return 1
        cmd.extend(["--config", cfg])
    elif env_var := _env_config_override():
        # No repo config to pass, so this env var IS the config and we cannot
        # vet it (GITLEAKS_CONFIG_TOML is inline content, not a path). Never
        # let it apply unannounced: an org-level Actions variable could blind
        # every repo's scanner and the stage would still print "no secrets
        # detected". Same class of override as HYPERCI_QUALITY_SKIP, so it gets
        # the same loud treatment rather than silence.
        warn(
            f"  gitleaks: {env_var} is set and no repo .gitleaks.toml exists - "
            "the scan is running with a config hyperi-ci did not vet."
        )
        warn("  Prefer a committed .gitleaks.toml so the config is reviewable.")

    # The canary measures what reading the TOML cannot: whether this config, as
    # gitleaks applies it, still reports a planted secret. It runs with no repo
    # config too, where it proves the binary's own default ruleset works. A
    # config already named rule-less is skipped - that notice is more precise.
    if not rule_less and _report_canary(cfg, mode) != 0:
        return 1

    env = dict(os.environ)
    # GITLEAKS_LICENSE key if available (org secret)
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
