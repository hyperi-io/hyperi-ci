# Project:   HyperI CI
# File:      src/hyperi_ci/languages/typescript/licenses.py
# Purpose:   npm dependency-licence gate (license-checker -> licence policy)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""npm dependency-licence gate.

license-checker-rseidelsohn reads the installed ``node_modules``, so npm, pnpm
and yarn projects are judged the same way, and ``--production`` leaves dev
dependencies out. Each package is loaded unmodified at runtime, so it is judged
as an ``import``.
"""

import json
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from hyperi_ci.common import error, run_cmd
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import emit_tool_output, resolve_tool_mode
from hyperi_ci.quality.dep_licenses import (
    Finding,
    Mode,
    NoticeEntry,
    Override,
    Policy,
    PolicyError,
    fail,
    find_override,
    judge,
    load_settings,
    project_path,
    quote,
)
from hyperi_ci.quality.vendored_licenses import finish_with_copied_in
from hyperi_ci.versions import tool_version

# license-checker falls back to a README when a package has no licence file.
_LICENCE_PREFIXES = ("LICENSE", "LICENCE", "COPYING", "UNLICENSE")
_NOTICE_PREFIXES = ("NOTICE",)

# license-checker marks a licence it guessed from file text with a trailing `*`.
_GUESSED = "*"


def _split_key(key: str) -> tuple[str, str]:
    # `@scope/name@1.2.3` splits on its last `@`.
    name, _, version = key.rpartition("@")
    return name, version


def _licence_file(record: Mapping[str, Any]) -> Path | None:
    raw = record.get("licenseFile")
    if not raw:
        return None
    path = Path(str(raw))
    if not path.name.upper().startswith(_LICENCE_PREFIXES) or not path.is_file():
        return None
    return path


def _read(path: Path | None) -> str | None:
    if path is None:
        return None
    return path.read_text(encoding="utf-8", errors="replace").strip() or None


def _licence_expression(record: Mapping[str, Any]) -> str | None:
    raw = record.get("licenses")
    # An old-style `licenses` array lists alternatives.
    expression = " OR ".join(raw) if isinstance(raw, list) else raw
    if not expression or str(expression).endswith(_GUESSED):
        return None
    return str(expression)


def findings_for(
    records: Mapping[str, Mapping[str, Any]],
    policy: Policy,
    overrides: Sequence[Override],
) -> list[Finding]:
    """Judge each production package license-checker reported.

    Args:
        records: license-checker JSON, keyed ``name@version``.
        policy: The effective policy.
        overrides: The repo's per-dependency answers.

    Returns:
        One finding per package.

    """
    findings = []
    for key in sorted(records):
        record = records[key]
        name, version = _split_key(key)
        override = find_override(overrides, "npm", name, version)
        if override is not None:
            raw, source, evidence = override.licence, "override", override.reason
        else:
            raw, source, evidence = _licence_expression(record), "metadata", "license"
        spdx, verdict, obligations, rule = judge(raw, Mode.IMPORT, policy)
        licence_file = _licence_file(record)
        if rule == "undeclared" and licence_file is not None:
            source = "license-file"
            evidence = f"{project_path(licence_file)}: {quote(_read(licence_file))}"
        findings.append(
            Finding(
                "npm",
                name,
                version,
                spdx,
                source,
                evidence,
                Mode.IMPORT,
                verdict,
                obligations,
                rule,
            )
        )
    return findings


def notice_entries(
    records: Mapping[str, Mapping[str, Any]], findings: Sequence[Finding]
) -> list[NoticeEntry]:
    """Pair each finding with the licence and NOTICE text in its package dir."""
    entries = []
    for finding in findings:
        record = records.get(f"{finding.name}@{finding.version}", {})
        package_dir = Path(str(record.get("path", "")))
        notices = (
            sorted(
                p
                for p in package_dir.iterdir()
                if p.is_file() and p.name.upper().startswith(_NOTICE_PREFIXES)
            )
            if record.get("path") and package_dir.is_dir()
            else []
        )
        notice = "\n\n".join(filter(None, (_read(p) for p in notices))) or None
        entries.append(NoticeEntry(finding, _read(_licence_file(record)), notice))
    return entries


def _checker_cmd(output: Path) -> list[str]:
    package = "license-checker-rseidelsohn"
    return [
        "npx",
        "--yes",
        f"{package}@{tool_version(package)}",
        "--start",
        ".",
        "--production",
        "--json",
        "--excludePrivatePackages",
        "--out",
        str(output),
    ]


def run_gate(config: CIConfig) -> bool:
    """Run the dependency-licence gate for an npm, pnpm or yarn project.

    Args:
        config: Merged CI configuration.

    Returns:
        True if the pipeline should continue, False on a blocking failure.

    """
    mode = resolve_tool_mode("dep_licenses", config, default="warn")
    if mode == "disabled":
        return True
    if not Path("node_modules").is_dir():
        return fail("  dep licences: no node_modules, nothing checked", mode)
    try:
        policy, overrides = load_settings(config)
    except PolicyError as exc:
        error(f"  dep licences: {exc}")
        return False

    with tempfile.TemporaryDirectory(prefix="hyperi-ci-licences-") as tmp:
        output = Path(tmp) / "licences.json"
        try:
            result = run_cmd(_checker_cmd(output), check=False, capture=True)
        except OSError:
            return fail("  dep licences: npx is not installed, nothing checked", mode)
        if result.returncode != 0 or not output.is_file():
            emit_tool_output("license-checker", result.stderr)
            return fail("  dep licences: license-checker failed, nothing checked", mode)
        records = json.loads(output.read_text(encoding="utf-8"))

    # A project without `"private": true` lists itself, and it is not a dependency.
    own = json.loads(Path("package.json").read_text(encoding="utf-8"))
    records.pop(f"{own.get('name')}@{own.get('version')}", None)
    findings = findings_for(records, policy, overrides)
    tools = {"license-checker-rseidelsohn": tool_version("license-checker-rseidelsohn")}
    entries = notice_entries(records, findings)
    return finish_with_copied_in(config, findings, entries, policy, tools, mode)
