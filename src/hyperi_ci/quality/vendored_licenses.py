# Project:   HyperI CI
# File:      src/hyperi_ci/quality/vendored_licenses.py
# Purpose:   Licence findings for source copied into the tree (reuse lint)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Licence findings for source copied into the tree.

A repo lists each copied-in directory under ``quality.dep_licenses.vendored``.
reuse reads each file's SPDX header, and every file is judged on its own, as
``vendored`` or ``modified``. A file with no header takes the licence a human
gave for its directory, or is ask.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from hyperi_ci.common import error, run_cmd
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import emit_tool_output
from hyperi_ci.quality.dep_licenses import (
    Finding,
    Mode,
    NoticeEntry,
    Policy,
    PolicyError,
    Verdict,
    fail,
    finish,
    judge,
)
from hyperi_ci.versions import tool_version

ECOSYSTEM = "vendored"

_COPIED_MODES = (Mode.VENDORED, Mode.MODIFIED)
_LICENCE_PREFIXES = ("LICENSE", "LICENCE", "COPYING")
_NOTICE_PREFIXES = ("NOTICE",)


@dataclass(frozen=True, slots=True)
class VendoredPath:
    """One copied-in directory and how it was copied."""

    path: str
    mode: Mode
    licence: str | None = None
    reason: str | None = None


def parse_vendored(raw: Any) -> list[VendoredPath]:
    """Parse ``quality.dep_licenses.vendored``.

    Args:
        raw: A list of ``{path, mode, licence?, reason?}`` mappings.

    Returns:
        The copied-in directories.

    Raises:
        PolicyError: An entry lacks a path, names another mode, or gives a
            licence with no reason.

    """
    entries = []
    for item in raw or []:
        if not isinstance(item, Mapping) or not str(item.get("path") or "").strip():
            raise PolicyError(f"dep_licenses.vendored: {item!r} needs a path")
        try:
            mode = Mode(str(item.get("mode") or Mode.VENDORED))
        except ValueError as exc:
            raise PolicyError(
                f"dep_licenses.vendored: unknown mode in {item!r}"
            ) from exc
        if mode not in _COPIED_MODES:
            raise PolicyError(
                f"dep_licenses.vendored: {item['path']} must be vendored or modified"
            )
        licence = str(item.get("licence") or "").strip() or None
        reason = str(item.get("reason") or "").strip() or None
        if licence and not reason:
            raise PolicyError(
                f"dep_licenses.vendored: {item['path']} gives a licence with no reason"
            )
        entries.append(VendoredPath(str(item["path"]).strip(), mode, licence, reason))
    return entries


def _reuse_cmd(root: Path) -> list[str]:
    spec = f"reuse=={tool_version('reuse')}"
    return ["uvx", "--from", spec, "reuse", "--root", str(root), "lint", "--json"]


def findings_for(
    entry: VendoredPath, lint: Mapping[str, Any], policy: Policy
) -> list[Finding]:
    """Judge each file reuse reported under one copied-in directory.

    Args:
        entry: The copied-in directory.
        lint: ``reuse lint --json`` output for it.
        policy: The effective policy.

    Returns:
        One finding per file.

    """
    findings = []
    for record in sorted(lint.get("files", []), key=lambda r: r.get("path", "")):
        name = (Path(entry.path) / str(record.get("path"))).as_posix()
        expressions = [e["value"] for e in record.get("spdx_expressions", [])]
        if expressions:
            raw = " AND ".join(dict.fromkeys(expressions))
            source, evidence = "file-header", "SPDX-License-Identifier"
        elif entry.licence:
            raw, source, evidence = entry.licence, "override", str(entry.reason)
        else:
            raw, source, evidence = None, "missing-header", "no SPDX header"
        spdx, verdict, obligations, rule = judge(raw, entry.mode, policy)
        findings.append(
            Finding(
                ECOSYSTEM,
                name,
                "-",
                spdx,
                source,
                evidence,
                entry.mode,
                verdict,
                obligations,
                rule,
            )
        )
    return findings


def _read_named(directory: Path, prefixes: Sequence[str]) -> str | None:
    if not directory.is_dir():
        return None
    texts = [
        p.read_text(encoding="utf-8", errors="replace").strip()
        for p in sorted(directory.iterdir())
        if p.is_file() and p.name.upper().startswith(tuple(prefixes))
    ]
    return "\n\n".join(t for t in texts if t) or None


def scan(
    config: CIConfig, policy: Policy
) -> tuple[list[Finding], list[NoticeEntry], list[str]]:
    """Judge every file in the repo's copied-in directories.

    Args:
        config: Merged CI configuration.
        policy: The effective policy.

    Returns:
        The findings, their notice entries, and a line per directory that
        could not be read.

    Raises:
        PolicyError: ``quality.dep_licenses.vendored`` is malformed.

    """
    settings = config.setting("quality.dep_licenses")
    raw = settings.get("vendored") if isinstance(settings, Mapping) else None
    findings: list[Finding] = []
    entries: list[NoticeEntry] = []
    problems: list[str] = []
    for entry in parse_vendored(raw):
        root = Path(entry.path)
        if not root.is_dir():
            problems.append(f"{entry.path} is not a directory")
            continue
        result = run_cmd(_reuse_cmd(root), check=False, capture=True)
        try:
            lint = json.loads(result.stdout)
        except json.JSONDecodeError:
            emit_tool_output("reuse", result.stderr)
            problems.append(f"reuse could not read {entry.path}")
            continue
        found = findings_for(entry, lint, policy)
        licence = _read_named(root, _LICENCE_PREFIXES)
        notice = _read_named(root, _NOTICE_PREFIXES)
        findings += found
        # Files share their directory's texts, so it is attributed once, by path.
        attributed = [f for f in found if f.verdict is not Verdict.ASK][:1]
        entries += [
            NoticeEntry(replace(f, name=entry.path), licence, notice)
            for f in attributed
        ]
    return findings, entries, problems


def finish_with_copied_in(
    config: CIConfig,
    findings: Sequence[Finding],
    entries: Sequence[NoticeEntry],
    policy: Policy,
    tools: Mapping[str, str],
    mode: str,
) -> bool:
    """Add the copied-in files to a language's findings, then finish the gate.

    Returns:
        True if the pipeline should continue, False on a blocking failure.

    """
    try:
        copied, copied_entries, problems = scan(config, policy)
    except PolicyError as exc:
        error(f"  dep licences: {exc}")
        return False
    held = [
        fail(f"  dep licences: {problem}, nothing checked there", mode)
        for problem in problems
    ]
    if copied:
        tools = {**tools, "reuse": tool_version("reuse")}
    passed = finish(
        [*findings, *copied], [*entries, *copied_entries], policy, tools, mode
    )
    return passed and all(held)
