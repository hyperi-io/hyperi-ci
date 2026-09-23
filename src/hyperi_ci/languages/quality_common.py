# Project:   HyperI CI
# File:      src/hyperi_ci/languages/quality_common.py
# Purpose:   Shared utilities for two-tier quality (production/test) rule splitting
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Shared quality check utilities for two-tier (production/test) rule splitting.

Quality checks run in two passes:
1. Production pass -- full strict rules on all code except test dirs
2. Test pass -- relaxed rules on test directories only

Test paths and ignore lists are configurable via defaults.yaml and
overridable per project in .hyperi-ci.yaml.
"""

import os
from pathlib import Path

from hyperi_ci.common import env_true, escape_command_data, is_ci, warn
from hyperi_ci.config import CIConfig, packaged_default

DEFAULT_TEST_PATHS = ["tests/"]

# The only valid quality-tool modes. An out-of-vocabulary value is a typo, not a
# silent request to disable the gate (see resolve_cross_tool_mode).
_VALID_MODES = {"blocking", "warn", "disabled"}

_MODE_STRENGTH = {"disabled": 0, "warn": 1, "blocking": 2}

# Tools whose findings are advisories about SECURITY - secrets, SAST, and the
# CVE/advisory feeds. Turning one of these below what hyperi-ci ships requires
# a stated reason (:func:`note_gate_downgrade`); every other tool may be
# turned down with a bare mode string.
SECURITY_TOOLS = frozenset(
    {
        "gitleaks",
        "semgrep",
        "bandit",
        "pip_audit",
        "audit",
        "deny",
        "osv_scanner",
        "gosec",
        "govulncheck",
    }
)


class GateReasonRequiredError(ValueError):
    """A security gate is relaxed in config and gives no reason for it.

    Carries the full operator-facing message, so a caller reports
    ``str(exc)`` and needs to know nothing about the gate that raised.
    """


def mode_and_reason(raw: object, default: str) -> tuple[str, str]:
    """Split a configured quality value into its mode and its stated reason.

    A quality key may be a bare mode string or a mapping carrying ``mode``
    plus options - the shape ``quality.rust.feature_matrix`` already uses,
    and the only one that can hold a ``reason`` beside the setting it
    explains.

    Args:
        raw: The configured value, of either shape.
        default: Mode to assume when a mapping carries no ``mode``.

    Returns:
        The mode, lowercased and stripped, and the reason (empty when none).

    """
    if isinstance(raw, dict):
        mode = str(raw.get("mode", default))
        reason = str(raw.get("reason") or "")
    else:
        mode = str(raw)
        reason = ""
    return mode.strip().lower(), reason.strip()


def strict_quality() -> bool:
    """Return True when strict quality mode is active.

    Strict mode upgrades ``warn``-tier findings to ``blocking`` so a
    developer sees - and then fixes or explicitly ignores - everything
    CI would surface BEFORE the push, not after. Enabled by
    ``hyperi-ci check --strict`` (which exports ``HYPERCI_QUALITY_STRICT``)
    or by exporting that env var directly.
    """
    return env_true("HYPERCI_QUALITY_STRICT")


def apply_strict(mode: str) -> str:
    """Upgrade a ``warn`` mode to ``blocking`` under :func:`strict_quality`.

    Shared by :func:`resolve_tool_mode` (per-language tools) and the
    dispatch-level cross-language scans (semgrep) so strict behaves
    identically whichever layer resolved the mode. ``disabled`` and
    ``blocking`` pass through unchanged.
    """
    if mode == "warn" and strict_quality():
        return "blocking"
    return mode


def quality_skip() -> frozenset[str]:
    """Tool names to forcibly skip this run (``HYPERCI_QUALITY_SKIP``).

    RARE edge-case escape hatch. When a tool's false positive halts CI
    - a semgrep rule misfiring on a dependency, an audit advisory with
    no fix yet - set ``HYPERCI_QUALITY_SKIP=semgrep`` (comma-separated
    for several) on the blocked runs to skip that tool WITHOUT a config
    commit, then remove it once the real fix (a rule ignore / version
    bump) lands. This is deliberately an env override, not a config knob:
    the reviewed config path (``quality.<tool>: disabled`` or the
    ``quality.ignore`` list) stays the normal way to silence a tool. A
    force-skip is logged loudly (:func:`is_skipped`).
    """
    raw = os.environ.get("HYPERCI_QUALITY_SKIP", "")
    return frozenset(t.strip().lower() for t in raw.split(",") if t.strip())


def is_skipped(tool: str) -> bool:
    """Return True if ``tool`` is force-skipped, surfacing it loudly.

    A force-skip is an emergency override that must NOT pass unnoticed -
    especially for a security scanner like gitleaks. In CI it emits a
    real GitHub ``::warning::`` annotation (which escapes the collapsed
    log group and lands in the run summary), not just a logger line that
    hides inside a folded group.
    """
    if tool.lower() not in quality_skip():
        return False
    msg = (
        f"{tool}: FORCE-SKIPPED via HYPERCI_QUALITY_SKIP - rare edge-case "
        f"override; remove it once the false positive is fixed"
    )
    warn(f"  {msg}")
    if is_ci():
        print(f"::warning title=hyperi-ci quality force-skip::{msg}")
    return True


def _mapping_example(key: str, mode: str) -> str:
    """Render the mapping form of ``key`` as a block the reader can paste."""
    parts = key.split(".")
    lines = [f"{'  ' * depth}{part}:" for depth, part in enumerate(parts)]
    indent = "  " * len(parts)
    lines.append(f"{indent}mode: {mode}")
    lines.append(
        f'{indent}reason: "<advisory id, why no fix exists, what mitigates it>"'
    )
    return "\n".join(lines)


def _reason_required_message(key: str, mode: str, shipped: str) -> str:
    """Build the message for a security gate relaxed without a reason."""
    tool = key.rsplit(".", 1)[-1]
    return "\n".join(
        (
            f"{key}: {tool} is a security gate, hyperi-ci ships '{shipped}', "
            f"and this repo turns it down to '{mode}' without saying why.",
            "  Name what the gate is waiting on, in the config beside the setting:",
            "",
            _mapping_example(key, mode),
            "",
            "  The reason prints in every run, so the decision can be re-checked "
            "later. A bare mode string stays valid for every non-security tool, "
            "and HYPERCI_QUALITY_SKIP is unaffected.",
            "  docs: docs/quality-gate.md#relaxing-a-security-gate",
        )
    )


def note_gate_downgrade(
    key: str, mode: str, reason: str = "", *, shipped_key: str | None = None
) -> None:
    """Say when a project has turned a gate below what hyperi-ci ships.

    A gate a repo relaxed and a gate that passed read the same in the log,
    so the relaxation has to announce itself where the run can be read.
    For a tool in :data:`SECURITY_TOOLS`, turning one below its shipped
    default with no ``reason`` beside it prints a warning naming the fix.
    Stage 2 of issue #259 turns that warning into
    :class:`GateReasonRequiredError` and a failed stage. The comparison is
    against that tool's OWN shipped default, whatever it is: semgrep ships
    ``warn``, so ``semgrep: warn`` is not a downgrade.

    Takes the CONFIGURED mode, not the post-``apply_strict`` one, so
    ``hyperi-ci check --strict`` reports the same config problem CI will.

    Args:
        key: Dotted config key, e.g. ``quality.python.pip_audit``.
        mode: Mode the config resolved to, before any strict upgrade.
        reason: Reason stated beside the setting, empty when none.
        shipped_key: Key carrying the shipped default, where the repo wrote
            its override somewhere else (semgrep's legacy per-language key).

    """
    shipped = packaged_default(shipped_key or key)
    if not isinstance(shipped, str):
        return
    shipped = shipped.strip().lower()
    if _MODE_STRENGTH.get(mode, 2) >= _MODE_STRENGTH.get(shipped, 0):
        return
    # Warns rather than failing until a wheel that parses the mapping is on
    # PyPI, because a consumer cannot state a reason before then (issue #259).
    if key.rsplit(".", 1)[-1] in SECURITY_TOOLS and not reason:
        owed = _reason_required_message(key, mode, shipped)
        warn(f"  {owed}")
        if is_ci():
            title = "hyperi-ci security gate needs a reason"
            print(f"::warning title={title}::{escape_command_data(owed)}")
        return
    msg = (
        f"{key}: this repo sets '{mode}', hyperi-ci ships '{shipped}' - "
        f"the gate is turned down here"
    )
    if reason:
        msg = f"{msg}; reason: {reason}"
    warn(f"  {msg}")
    if is_ci():
        print(f"::warning title=hyperi-ci gate turned down::{escape_command_data(msg)}")


def resolve_cross_tool_mode(
    config: CIConfig, tool: str, default: str = "blocking"
) -> str:
    """Resolve mode for a cross-language quality tool (``quality.<tool>``).

    Unlike :func:`resolve_tool_mode` (per-language ``quality.<lang>.<tool>``),
    this reads the top-level ``quality.<tool>`` key shared by gitleaks, semgrep,
    hadolint, droast, kubeconform, kube-linter and Checkov.

    ``quality.<tool>`` may be a plain mode string (``blocking`` / ``warn`` /
    ``disabled``) OR a dict carrying a ``mode`` plus tool options (Checkov's
    ``frameworks`` / ``skip``, kubeconform's ``schema_locations``, and the
    ``reason`` a relaxed security gate needs) - a bare string keeps the options
    at their defaults. A force-skip wins; otherwise strict upgrades a ``warn``
    to ``blocking``.
    """
    if is_skipped(tool):
        return "disabled"
    key = f"quality.{tool}"
    mode, reason = checked_mode(key, config.get(key, default), default)
    note_gate_downgrade(key, mode, reason)
    return apply_strict(mode)


def resolve_tool_mode(tool: str, config: CIConfig, language: str) -> str:
    """Resolve a quality tool's mode: ``blocking``, ``warn`` or ``disabled``.

    Reads ``quality.<language>.<tool>`` from config (default ``blocking``),
    which takes the same bare-string-or-mapping shapes as
    :func:`resolve_cross_tool_mode`. A force-skip (:func:`is_skipped`) wins -
    the tool is ``disabled`` for this run. Otherwise, under strict mode
    (:func:`strict_quality`) a ``warn`` tool is upgraded to ``blocking``;
    ``disabled`` is left untouched.
    """
    if is_skipped(tool):
        return "disabled"
    key = f"quality.{language}.{tool}"
    mode, reason = checked_mode(key, config.get(key, "blocking"), "blocking")
    note_gate_downgrade(key, mode, reason)
    return apply_strict(mode)


def checked_mode(key: str, raw: object, default: str) -> tuple[str, str]:
    """Split ``raw`` into mode and reason, rejecting an out-of-vocabulary mode.

    A typo (`block`, `enabled`, `true`) must NOT silently downgrade a gate to
    advisory - warn loudly and fall back to the tool's default instead.
    """
    mode, reason = mode_and_reason(raw, default)
    if mode not in _VALID_MODES:
        warn(
            f"{key}: unknown mode '{mode}' - expected "
            f"blocking / warn / disabled; using '{default}'"
        )
        mode = default
    return mode, reason


def get_test_paths(config: CIConfig) -> list[str]:
    """Get configured test directories that exist on disk.

    Reads quality.test_paths from config, defaults to ["tests/"].
    Only returns paths that actually exist as directories.
    """
    configured = config.get("quality.test_paths", DEFAULT_TEST_PATHS)
    if not isinstance(configured, list):
        configured = DEFAULT_TEST_PATHS
    return [p for p in configured if Path(p).is_dir()]


def get_test_ignore(language: str, config: CIConfig, defaults: list[str]) -> list[str]:
    """Get test_ignore rules for a language, with fallback to defaults.

    Projects override entirely via quality.<language>.test_ignore
    in .hyperi-ci.yaml. If not set, uses the provided defaults
    (which come from defaults.yaml).
    """
    configured = config.get(f"quality.{language}.test_ignore", None)
    if configured is not None and isinstance(configured, list):
        return [str(r) for r in configured]
    return defaults
