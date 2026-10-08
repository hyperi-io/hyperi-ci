# Project:   HyperI CI
# File:      src/hyperi_ci/quality/findings.py
# Purpose:   Shared finding surface for the linting tools (annotations + job
#            summary + SARIF), with a step-global annotation budget
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Shared finding surface for the linting tools.

Each tool parses its output into :class:`Finding` objects and hands them to
:func:`surface`, which writes three layers:

1. GitHub annotations, errors first, within a budget.
2. The job summary table, capped at 1000 rows to stay under GitHub's 1 MiB
   step limit. It carries what the annotation budget drops.
3. SARIF, only when a path is given. Uploading it needs GitHub Code Security,
   so the upload is the workflow's job.

GitHub keeps 10 error and 10 warning annotations per step and silently drops
the rest, so every tool in one process draws from one :class:`_AnnotationBudget`.
``lint-iac`` is a separate step and process, so it gets a fresh budget.
"""

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from hyperi_ci.common import (
    error,
    escape_command_data,
    info,
    is_github_actions,
    run_cmd,
    success,
    warn,
)

# GitHub truncates a step summary at 1 MiB.
_MAX_SUMMARY_ROWS = 1000

# GitHub's per-step limit for error and warning; notice is capped to match.
_ANNOTATION_CAP = 10

_GH_COMMAND = {"error": "error", "warning": "warning", "notice": "notice"}

# SARIF spells `notice` as `note`.
_SARIF_LEVEL = {"error": "error", "warning": "warning", "notice": "note"}

_LEVEL_ALIASES = {
    "error": "error",
    "err": "error",
    "warning": "warning",
    "warn": "warning",
    "info": "notice",
    "information": "notice",
    "style": "notice",
    "note": "notice",
    "notice": "notice",
}


def normalise_level(raw: str) -> str:
    """Fold a tool's severity word to ``error``, ``warning`` or ``notice``.

    An unknown word becomes ``warning``.
    """
    return _LEVEL_ALIASES.get(str(raw).strip().lower(), "warning")


@dataclass(frozen=True)
class Finding:
    """One normalised finding from any linting tool.

    ``level`` is already folded by :func:`normalise_level`. ``line`` is
    1-indexed, or ``None`` when the tool reports no location.
    """

    tool: str
    path: str
    line: int | None
    level: str
    rule: str
    message: str
    url: str = ""


@dataclass
class _AnnotationBudget:
    """Remaining annotations per GitHub level, shared through :data:`_BUDGET`."""

    remaining: dict[str, int] = field(
        default_factory=lambda: {
            "error": _ANNOTATION_CAP,
            "warning": _ANNOTATION_CAP,
            "notice": _ANNOTATION_CAP,
        }
    )

    def take(self, level: str) -> bool:
        """Consume one annotation of ``level``; return False if none remain."""
        if self.remaining.get(level, 0) <= 0:
            return False
        self.remaining[level] -= 1
        return True


_BUDGET = _AnnotationBudget()

# Findings surfaced in this process, by tool, for the orchestrator's counts.
_TALLY: dict[str, int] = {}


def surfaced_count() -> int:
    """Return how many findings :func:`surface` has handled in this process."""
    return sum(_TALLY.values())


def reset_annotation_budget() -> None:
    """Restore the step-global annotation budget to full."""
    _BUDGET.remaining = {
        "error": _ANNOTATION_CAP,
        "warning": _ANNOTATION_CAP,
        "notice": _ANNOTATION_CAP,
    }


def _prop_val(value: str) -> str:
    """Blank the commas, ``::`` and newlines in a workflow-command property value.

    Repo-controlled content in a property could otherwise inject another one.
    """
    return (
        value.replace(",", " ").replace("::", " ").replace("\r", " ").replace("\n", " ")
    )


def _annotation_line(f: Finding) -> str:
    """Render one GitHub workflow-command annotation line for ``f``."""
    cmd = _GH_COMMAND[f.level]
    rule = _prop_val(f.rule)
    props = (
        [f"title=hyperi-ci {_prop_val(f.tool)}: {rule}"]
        if f.rule
        else [f"title=hyperi-ci {_prop_val(f.tool)}"]
    )
    if f.path:
        props.insert(0, f"file={_prop_val(f.path)}")
        if f.line is not None:
            props.append(f"line={f.line}")
    return f"::{cmd} {','.join(props)}::{escape_command_data(f.message)}"


def emit_annotations(findings: list[Finding]) -> int:
    """Emit GitHub annotations for ``findings``, errors first, within the budget.

    Returns:
        How many findings the budget dropped; 0 outside GitHub Actions, where
        nothing is emitted.
    """
    if not is_github_actions():
        return 0
    order = {"error": 0, "warning": 1, "notice": 2}
    dropped = 0
    for f in sorted(findings, key=lambda x: order.get(x.level, 3)):
        if _BUDGET.take(f.level):
            print(_annotation_line(f))
        else:
            dropped += 1
    return dropped


def _summary_table(findings: list[Finding]) -> str:
    """Render a markdown table of ``findings`` for the job summary."""
    rows = ["| Severity | Rule | Location | Message |", "| --- | --- | --- | --- |"]
    for f in findings:
        loc = f.path + (f":{f.line}" if f.line is not None else "")
        msg = f.message.replace("|", "\\|").replace("\n", " ")
        rule = f"[{f.rule}]({f.url})" if f.url else f.rule
        rows.append(f"| {f.level} | {rule} | {loc} | {msg} |")
    return "\n".join(rows)


def append_job_summary(tool: str, findings: list[Finding]) -> None:
    """Append a ``tool`` findings table to ``$GITHUB_STEP_SUMMARY``, when set.

    Capped at ``_MAX_SUMMARY_ROWS`` with a truncation note.
    """
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path or not findings:
        return
    shown = findings[:_MAX_SUMMARY_ROWS]
    heading = f"### {tool}: {len(findings)} finding(s)\n\n"
    block = heading + _summary_table(shown)
    if len(findings) > len(shown):
        block += f"\n\n_... {len(findings) - len(shown)} more findings truncated (see the log)._"
    block += "\n\n"
    # A write failure warns rather than failing the check.
    try:
        with Path(summary_path).open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(block)
    except OSError as exc:
        warn(f"  could not write job summary for {tool}: {exc}")


def write_sarif(tool: str, findings: list[Finding], path: str | Path) -> None:
    """Append a run for ``tool`` to the SARIF 2.1.0 file at ``path``.

    Tools sharing a ``path`` build one multi-run file for a single upload.
    """
    p = Path(path)
    doc: dict = {
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "runs": [],
    }
    if p.exists():
        try:
            existing = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and isinstance(existing.get("runs"), list):
                doc = existing
        except (OSError, json.JSONDecodeError):
            pass  # start fresh rather than fail the lint on a corrupt prior file

    rules: dict[str, dict] = {}
    results = []
    for f in findings:
        if f.rule and f.rule not in rules:
            rule: dict = {"id": f.rule}
            if f.url:
                rule["helpUri"] = f.url
            rules[f.rule] = rule
        region = {"startLine": f.line} if f.line is not None else {}
        results.append(
            {
                "ruleId": f.rule or tool,
                "level": _SARIF_LEVEL.get(f.level, "warning"),
                "message": {"text": f.message},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": f.path},
                            **({"region": region} if region else {}),
                        }
                    }
                ]
                if f.path
                else [],
            }
        )
    doc["runs"].append(
        {
            "tool": {"driver": {"name": tool, "rules": list(rules.values())}},
            "results": results,
        }
    )
    # A write failure warns rather than failing the check.
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(doc, indent=2), encoding="utf-8", newline="\n")
    except OSError as exc:
        warn(f"  could not write SARIF for {tool}: {exc}")


def parse_sarif(text: str, tool: str) -> list[Finding]:
    """Parse a tool's SARIF 2.1.0 output into :class:`Finding` objects.

    ``tool`` labels the findings, because driver names vary. Malformed or
    empty SARIF returns ``[]``.
    """
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(doc, dict):
        return []
    # SARIF folds `note`/`none` down here; unknown -> normalise_level's default.
    sarif_to_level = {
        "error": "error",
        "warning": "warning",
        "note": "notice",
        "none": "notice",
    }
    out: list[Finding] = []
    for run in doc.get("runs", []) or []:
        rule_urls: dict[str, str] = {}
        driver = (run.get("tool") or {}).get("driver") or {}
        for rule in driver.get("rules", []) or []:
            rid = rule.get("id")
            if rid and rule.get("helpUri"):
                rule_urls[rid] = rule["helpUri"]
        for res in run.get("results", []) or []:
            rule_id = res.get("ruleId") or ""
            level = sarif_to_level.get(str(res.get("level", "")).lower(), "")
            level = level or normalise_level(str(res.get("level", "warning")))
            message = ((res.get("message") or {}).get("text")) or ""
            path, line = "", None
            locs = res.get("locations") or []
            if locs:
                phys = (locs[0] or {}).get("physicalLocation") or {}
                path = (phys.get("artifactLocation") or {}).get("uri") or ""
                line = (phys.get("region") or {}).get("startLine")
            out.append(
                Finding(
                    tool=tool,
                    path=path,
                    line=line,
                    level=level,
                    rule=rule_id,
                    message=message,
                    url=rule_urls.get(rule_id, ""),
                )
            )
    return out


def log_findings(tool: str, findings: list[Finding]) -> None:
    """Log ``findings`` outside GitHub Actions, capped like the summary (issue #72)."""
    if not findings or is_github_actions():
        return
    shown = findings[:_MAX_SUMMARY_ROWS]
    for f in shown:
        loc = f.path + (f":{f.line}" if f.line is not None else "")
        line = f"  {tool}: {f.level} {f.rule} {loc} {f.message}".rstrip()
        if f.level == "error":
            error(line)
        elif f.level == "warning":
            warn(line)
        else:
            info(line)
    if len(findings) > len(shown):
        info(f"  {tool}: +{len(findings) - len(shown)} more finding(s) not shown")


def at_mode(findings: list[Finding], mode: str) -> list[Finding]:
    """Return ``findings`` as a check running at ``mode`` should surface them.

    Outside ``blocking`` an error becomes a warning, so a green job shows no
    error annotation. Gate decisions read the original list.
    """
    if mode == "blocking":
        return findings
    return [replace(f, level="warning") if f.level == "error" else f for f in findings]


def surface(
    tool: str, findings: list[Finding], *, sarif_path: str | Path | None = None
) -> int:
    """Surface ``findings`` as annotations, job summary, log and SARIF.

    Returns:
        How many findings the annotation budget dropped.
    """
    _TALLY[tool] = _TALLY.get(tool, 0) + len(findings)
    dropped = emit_annotations(findings)
    append_job_summary(tool, findings)
    log_findings(tool, findings)
    if sarif_path is not None:
        write_sarif(tool, findings, sarif_path)
    return dropped


def relpath(path: Path, root: Path = Path()) -> str:
    """Return ``path`` relative to ``root`` (default: the cwd), where annotations attach."""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def report(
    tool: str, findings: list[Finding], mode: str, *, sarif_path: str | Path | None
) -> None:
    """Surface ``findings`` as a check at ``mode``, noting any the budget dropped."""
    dropped = surface(tool, at_mode(findings, mode), sarif_path=sarif_path)
    if dropped:
        info(f"  {tool}: +{dropped} more finding(s) in the job summary")


def verdict(tool: str, errors: int, mode: str, ok: str) -> int:
    """Log a check's verdict on ``errors`` error-level findings; return its exit code."""
    if not errors:
        success(f"  {tool}: {ok}")
        return 0
    if mode == "blocking":
        error(f"  {tool}: {errors} finding(s) must be fixed")
        return 1
    warn(f"  {tool}: {errors} finding(s) (non-blocking)")
    return 0


def run_tool(
    cmd: list[str],
    fail: Callable[[str, str], Finding],
    *,
    timeout: float | None,
    **kwargs: Any,
) -> subprocess.CompletedProcess[str] | Finding:
    """Run a check's command; return its result, or ``fail(kind, why)`` if it never finished."""
    try:
        return run_cmd(
            cmd, check=False, capture=True, timeout=timeout, own_group=True, **kwargs
        )
    except subprocess.TimeoutExpired:
        return fail("timeout", f"no result within {timeout}s")
    except OSError as exc:
        return fail("unrunnable", f"could not run ({exc})")
