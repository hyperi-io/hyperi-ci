# Project:   HyperI CI
# File:      src/hyperi_ci/quality/gitleaks.py
# Purpose:   Gitleaks secret scanning (cross-language)
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Gitleaks secret scanning.

Scans repository git history for committed secrets. Runs before
language-specific quality checks on every project.

Ported from old CI: ci/scripts/core/gitleaks.sh
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from hyperi_ci.common import error, info, is_ci, run_cmd, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import apply_strict, is_skipped
from hyperi_ci.tools import missing_tool_notice

# Mirrors `tools.gitleaks` in config/versions.yaml - the SSoT. The pre-commit
# hook (scripts/update-versions.py --fix) rewrites the marked line, so do not
# hand-edit it. It lives here rather than being read from the YAML because
# config/ ships outside the wheel (pyproject packages = ["src/hyperi_ci"]).
# hyperi-ci:pin tools.gitleaks
_GITLEAKS_VERSION = "v8.30.1"


def _install_gitleaks() -> bool:
    """Install gitleaks binary on Linux CI runners.

    Returns:
        True if gitleaks is available after install attempt.

    """
    if shutil.which("gitleaks"):
        return True

    if not is_ci():
        return False

    if sys.platform != "linux":
        warn("  gitleaks auto-install only supported on Linux CI")
        return False

    import platform

    arch = "x64" if platform.machine() in ("x86_64", "AMD64") else "arm64"
    version_num = _GITLEAKS_VERSION.lstrip("v")
    url = (
        f"https://github.com/gitleaks/gitleaks/releases/download/"
        f"{_GITLEAKS_VERSION}/gitleaks_{version_num}_linux_{arch}.tar.gz"
    )

    info(f"  Installing gitleaks {_GITLEAKS_VERSION}...")
    with tempfile.TemporaryDirectory() as tmp:
        result = subprocess.run(
            ["curl", "-sSL", url],
            capture_output=True,
        )
        if result.returncode != 0:
            error("  Failed to download gitleaks")
            return False

        tar_path = Path(tmp) / "gitleaks.tar.gz"
        tar_path.write_bytes(result.stdout)
        subprocess.run(
            ["tar", "xzf", str(tar_path), "-C", tmp],
            check=True,
        )
        bin_path = Path(tmp) / "gitleaks"
        if bin_path.exists():
            dest = Path("/usr/local/bin/gitleaks")
            subprocess.run(
                ["sudo", "mv", str(bin_path), str(dest)],
                check=True,
            )
            subprocess.run(["sudo", "chmod", "+x", str(dest)], check=True)

    return shutil.which("gitleaks") is not None


def _find_config() -> str | None:
    """Find gitleaks config file in project."""
    for path in (".gitleaks.toml", "ci/.gitleaks.toml"):
        if Path(path).exists():
            return path
    return None


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
    report "no leaks found". Catching those needs evaluating the allowlist
    against the repo, not reading the TOML - see #67. Claiming more than the
    check delivers would be its own silent failure, so the notice only speaks
    about the rule SOURCE.

    Unparseable/unreadable configs return False: gitleaks itself will complain
    with a better message than we can, and we must not turn a malformed file
    into a spurious "your gate is blind" claim.
    """
    try:
        data = tomllib.loads(Path(cfg_path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False

    # gitleaks loads config through viper, which folds key case completely:
    # `UseDefault`, `[[Rules]]` and `[Extend]` all work. Fold once rather than
    # enumerating spellings - a half-done fold reads a WORKING config as blind
    # and, in `blocking` mode, hard-fails CI on a repo that is scanning fine.
    folded = {str(k).lower(): v for k, v in data.items()}
    if folded.get("rules"):
        return False

    extend = folded.get("extend") or {}
    if not isinstance(extend, dict):
        return True
    extend = {str(k).lower(): v for k, v in extend.items()}
    # Only `useDefault` and `path` pull in rules. `extend.url` deliberately does
    # NOT count: gitleaks' extendURL() is an empty `// TODO` stub as of 8.30.1
    # and nothing reads Extend.URL, so a url-only extend silently loads zero
    # rules - the very #64 failure this guard exists to catch.
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


def _env_config_override() -> str | None:
    """Name the GITLEAKS_CONFIG* env var in play, if any.

    gitleaks reads its config from (in precedence order) `--config`, then
    `GITLEAKS_CONFIG`, then `GITLEAKS_CONFIG_TOML`, then
    `(target path)/.gitleaks.toml`. We always beat the env vars WHEN the repo
    has a config to pass. When it does not, they silently take over and can
    blind the scan - so they must be surfaced rather than ignored.
    """
    for name in ("GITLEAKS_CONFIG", "GITLEAKS_CONFIG_TOML"):
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
    # apply_strict, like semgrep does: --strict upgrades warn -> blocking. The
    # rule-less guard rides on `mode`, so without this a developer who asked for
    # strict got a green "no secrets detected" out of an empty ruleset.
    mode = apply_strict(str(config.get("quality.gitleaks", "blocking")))
    if is_skipped("gitleaks"):
        return 0
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
    if cfg:
        if _declares_no_ruleset(cfg) and _report_no_ruleset(cfg, mode) != 0:
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
