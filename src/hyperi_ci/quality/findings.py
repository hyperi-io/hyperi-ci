# Project:   HyperI CI
# File:      src/hyperi_ci/quality/findings.py
# Purpose:   Shared finding surface for the linting tools (annotations + job
#            summary + SARIF), with a step-global annotation budget
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Shared finding surface for the container / k8s / IaC linting tools.

hadolint, droast, kubeconform, kube-linter and Checkov each emit their own
JSON. Rather than let each one surface findings its own way (native
annotations here, SARIF there, plain log elsewhere), every tool module parses
its JSON into a normalised :class:`Finding` list and hands it to :func:`surface`.
One code path then decides HOW findings appear, uniformly, in three layers:

1. **GitHub annotations** (``::error::`` / ``::warning::`` / ``::notice::``) -
   a BOUNDED set of inline pointers, errors first. Portable, needs no token,
   works on every repo including private and forks.
2. **Job summary** (``$GITHUB_STEP_SUMMARY`` markdown table) - the findings
   list, bounded at 1000 rows with a truncation note (to stay under GitHub's
   1MiB/step ceiling). This is the safety net for the annotation cap.
3. **SARIF** - written only when a path is configured (opt-in). Writing the
   file is always safe; UPLOADING it into code scanning needs GitHub Code
   Security (paid on private repos), so the upload is the workflow's job, not
   ours - we never try to upload and never trigger the "must enable" error.

The annotation budget is the subtle part. GitHub caps annotations at **10
error + 10 warning per STEP** (50 per job), and SILENTLY drops the rest with no
feedback - a static analyser that emits 40 findings looks like it emitted 10.
Every tool that surfaces through here draws from ONE process-global budget (a
module-level :class:`_AnnotationBudget`), so the tools sharing a process cannot
between them exceed the per-step cap. When it is exhausted the remaining
findings still land in the job summary, which is uncapped - nothing is ever
silently lost.

In practice the budget couples the ``surface()`` users that run in the same
process: hadolint + droast inside ``hyperi-ci run quality`` (gitleaks/semgrep
emit their own output and do not use this surface), and kubeconform +
kube-linter + Checkov inside the separate ``lint-manifests`` process - which,
being a distinct step, correctly starts with its own fresh budget.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from hyperi_ci.common import is_github_actions, warn

# Cap the rows written to one job-summary section. GitHub truncates a step
# summary at 1 MiB; a bounded table plus a "truncated" note keeps a pathological
# findings count from silently hitting that ceiling (the annotation cap's twin).
_MAX_SUMMARY_ROWS = 1000

# GitHub Actions per-step annotation limits (10 error + 10 warning). We budget
# each level to its own ceiling and prioritise errors. `notice` has no
# documented separate ceiling, but is bounded here too so an advisory tool
# cannot flood the run summary annotations.
_ANNOTATION_CAP = 10

# Normalised severity -> GitHub workflow-command keyword. Tools speak
# error/warning/info/style/note; we fold everything to the three GitHub levels.
_GH_COMMAND = {"error": "error", "warning": "warning", "notice": "notice"}

# Normalised severity -> SARIF result level (SARIF uses `note`, not `notice`).
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
    """Fold a tool's severity word to one of ``error`` / ``warning`` / ``notice``.

    Unknown severities default to ``warning`` - visible but not build-failing,
    the safe middle for an unrecognised signal.
    """
    return _LEVEL_ALIASES.get(str(raw).strip().lower(), "warning")


@dataclass(frozen=True)
class Finding:
    """One normalised finding from any linting tool.

    ``level`` is already folded to ``error`` / ``warning`` / ``notice`` (use
    :func:`normalise_level` at parse time). ``line`` is 1-indexed or ``None``
    when the tool reports no location.
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
    """Step-global remaining-annotation counters, one per GitHub level.

    Module-level singleton (:data:`_BUDGET`) so every tool in the one quality
    process draws from the same pool. :func:`reset_annotation_budget` restores
    it (used by tests and available if a caller wants a clean step).
    """

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


def reset_annotation_budget() -> None:
    """Restore the step-global annotation budget to full."""
    _BUDGET.remaining = {
        "error": _ANNOTATION_CAP,
        "warning": _ANNOTATION_CAP,
        "notice": _ANNOTATION_CAP,
    }


def _prop_val(value: str) -> str:
    """Sanitise a workflow-command PROPERTY value (file / title / rule).

    Property fields are comma- and ``::``-delimited, so repo-controlled content
    (a manifest ``kind``, a filename with a comma) could otherwise inject or
    spoof another property. Strip the delimiters and newlines - cosmetic fields,
    so replacing is fine.
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
    # Newlines in the message would break the single-line command; encode them
    # the way GitHub expects (%0A) so a multi-line message survives intact.
    msg = f.message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::{cmd} {','.join(props)}::{msg}"


def emit_annotations(findings: list[Finding]) -> int:
    """Emit up-to-budget GitHub annotations for ``findings``, errors first.

    Returns the number of findings that could NOT be annotated because the
    step-global budget was exhausted - the caller notes "+N more, see summary"
    so an exhausted budget is visible rather than a silent drop. Outside GitHub
    Actions this is a no-op (returns 0); the job summary and log carry the
    findings there.
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
        # Escape pipes so a message containing `|` does not break the table.
        msg = f.message.replace("|", "\\|").replace("\n", " ")
        rule = f"[{f.rule}]({f.url})" if f.url else f.rule
        rows.append(f"| {f.level} | {rule} | {loc} | {msg} |")
    return "\n".join(rows)


def append_job_summary(tool: str, findings: list[Finding]) -> None:
    """Append a ``tool`` section (full findings table) to the job summary.

    Writes to ``$GITHUB_STEP_SUMMARY`` when set (GitHub Actions); a no-op
    otherwise. The list goes here bounded at ``_MAX_SUMMARY_ROWS`` (to stay
    under the 1MiB/step ceiling), so it is the authoritative record even when
    the annotation budget truncated.
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
    # Best-effort surfacing: a write failure (unwritable summary path) must never
    # crash the tool - especially an advisory, which is contractually non-fatal.
    try:
        with Path(summary_path).open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(block)
    except OSError as exc:
        warn(f"  could not write job summary for {tool}: {exc}")


def write_sarif(tool: str, findings: list[Finding], path: str | Path) -> None:
    """Write (or append a run to) a SARIF 2.1.0 file at ``path``.

    Multiple tools in one stage can target the same ``path``; each call appends
    its own ``run`` so the result is a single multi-run SARIF the workflow
    uploads ONCE. Writing the file is always safe - the code-scanning UPLOAD
    (which needs GitHub Code Security, paid on private repos) is the workflow's
    responsibility, gated there, never attempted here.
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
    # Best-effort: an unwritable sarif path must not crash the tool (advisories
    # are contractually non-fatal; a gate should fail on findings, not on a
    # surfacing IO error).
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    except OSError as exc:
        warn(f"  could not write SARIF for {tool}: {exc}")


def parse_sarif(text: str, tool: str) -> list[Finding]:
    """Parse a tool's SARIF 2.1.0 output into normalised :class:`Finding` s.

    Several tools (droast, kube-linter, Checkov) emit SARIF, whose schema is
    fixed and standard - so parsing SARIF is far more robust than each tool's
    bespoke JSON, whose field names we cannot always pin down. One parser
    serves them all. ``tool`` labels the findings; the SARIF driver name is not
    trusted for that (it varies).

    Tolerant by design: a malformed or empty SARIF yields ``[]`` rather than
    raising, because an advisory tool must never turn a parse hiccup into a
    failed lint.
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
        # Rule helpUri lookup, so a finding can carry its docs link.
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


def surface(
    tool: str, findings: list[Finding], *, sarif_path: str | Path | None = None
) -> int:
    """Surface ``findings`` across all layers: annotations, summary, optional SARIF.

    Returns the count of findings that overflowed the annotation budget (so the
    caller can log "+N more, see summary"). The full list is always in the job
    summary and, when ``sarif_path`` is set, in the SARIF file.
    """
    dropped = emit_annotations(findings)
    append_job_summary(tool, findings)
    if sarif_path is not None:
        write_sarif(tool, findings, sarif_path)
    return dropped
