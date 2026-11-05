# Project:   HyperI CI
# File:      src/hyperi_ci/languages/python/licenses.py
# Purpose:   Python dependency-licence gate (pip-licenses -> licence policy)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Python dependency-licence gate.

pip-licenses reads the project's environment from outside it, so its own
dependencies are never judged. Only the lock's runtime set (every extra, no
dev group, not the project itself) is judged, each as an unmodified import.
"""

import json
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from hyperi_ci.common import error, info, run_cmd, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import emit_tool_output, resolve_tool_mode
from hyperi_ci.quality.dep_licenses import (
    Finding,
    Mode,
    Override,
    Policy,
    PolicyError,
    Verdict,
    blocking,
    find_override,
    judge,
    load_policy,
    parse_overrides,
    report,
)
from hyperi_ci.versions import tool_version

REPORT_PATH = Path(".hyperi-ci") / "licenses.json"

# The lock's shipped set: every extra, no dependency group, not the project.
_RUNTIME_EXPORT = [
    "uv",
    "export",
    "--frozen",
    "--no-dev",
    "--all-extras",
    "--no-hashes",
    "--no-header",
    "--no-annotate",
    "--no-emit-project",
    "--no-emit-workspace",
    "--no-emit-local",
    "--format",
    "requirements-txt",
]

_PROJECT_PYTHON = [
    "uv",
    "run",
    "--no-sync",
    "python",
    "-c",
    "import sys; print(sys.executable)",
]

# Licence file lines quoted when metadata declares nothing.
_QUOTE_LINES = 3


def runtime_packages(export_text: str) -> tuple[dict[str, str], list[str]]:
    """Return the pins a ``uv export`` installs here, and those it does not.

    Args:
        export_text: requirements.txt text with ``name==version`` lines.

    Returns:
        Canonical name to version for every requirement this platform installs
        (empty for a git or URL reference), and the names whose environment
        marker excludes this platform.

    """
    pins: dict[str, str] = {}
    elsewhere: list[str] = []
    for line in export_text.splitlines():
        text = line.strip()
        if not text or text.startswith(("#", "-")):
            continue
        try:
            requirement = Requirement(text)
        except InvalidRequirement:
            continue
        name = canonicalize_name(requirement.name)
        if requirement.marker is not None and not requirement.marker.evaluate():
            elsewhere.append(name)
            continue
        # A git or URL reference has no `==` pin, and it still ships.
        versions = [s.version for s in requirement.specifier if s.operator == "=="]
        pins[name] = versions[0] if versions else ""
    return pins, elsewhere


def _quote(text: str | None) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return " / ".join(lines[:_QUOTE_LINES])


def findings_for(
    packages: Sequence[Mapping[str, Any]],
    runtime: Mapping[str, str],
    policy: Policy,
    overrides: Sequence[Override],
) -> list[Finding]:
    """Judge each runtime package pip-licenses reported.

    A runtime package pip-licenses did not report is ask: it was never seen.

    Args:
        packages: pip-licenses JSON records.
        runtime: Canonical name to version for the lock's shipped set.
        policy: The effective policy.
        overrides: The repo's per-dependency answers.

    Returns:
        One finding per runtime package.

    """
    by_name = {canonicalize_name(str(p.get("Name", ""))): p for p in packages}
    findings = []
    for name, version in sorted(runtime.items()):
        package = by_name.get(name)
        if package is None:
            findings.append(
                Finding(
                    "pypi",
                    name,
                    version,
                    None,
                    "missing",
                    "not in the environment",
                    Mode.IMPORT,
                    Verdict.ASK,
                    (),
                    "missing",
                )
            )
            continue
        version = version or str(package.get("Version") or "")
        override = find_override(overrides, "pypi", name, version)
        if override is not None:
            raw, source, evidence = override.licence, "override", override.reason
        else:
            raw, source = str(package.get("License") or ""), "metadata"
            evidence = "License"
        spdx, verdict, obligations, rule = judge(raw, Mode.IMPORT, policy)
        licence_text = package.get("LicenseText")
        if rule == "undeclared" and licence_text not in (None, "UNKNOWN"):
            source = "license-file"
            evidence = f"{package.get('LicenseFile')}: {_quote(licence_text)}"
        findings.append(
            Finding(
                "pypi",
                str(package.get("Name")),
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


def _pip_licenses_cmd(python: str, output: Path) -> list[str]:
    spec = f"pip-licenses=={tool_version('pip-licenses')}"
    return [
        "uvx",
        "--from",
        spec,
        "pip-licenses",
        "--python",
        python,
        "--format=json",
        "--from=mixed",
        "--with-license-file",
        "--with-notice-file",
        "--output-file",
        str(output),
    ]


def _fail(note: str, mode: str) -> bool:
    (warn if mode == "warn" else error)(note)
    return mode == "warn"


def _write_report(findings: Sequence[Finding], policy: Policy) -> None:
    REPORT_PATH.parent.mkdir(exist_ok=True)
    (REPORT_PATH.parent / ".gitignore").write_text(
        "*\n", encoding="utf-8", newline="\n"
    )
    tools = {"pip-licenses": tool_version("pip-licenses")}
    REPORT_PATH.write_text(
        json.dumps(report(findings, policy, tools), indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def run_gate(config: CIConfig) -> bool:
    """Run the dependency-licence gate for a Python project.

    Args:
        config: Merged CI configuration.

    Returns:
        True if the pipeline should continue, False on a blocking failure.

    """
    mode = resolve_tool_mode("dep_licenses", config, default="warn")
    if mode == "disabled":
        return True
    if not Path("uv.lock").is_file():
        info("  dep licences: no uv.lock, so there is no shipped set to judge")
        return True
    if not shutil.which("uv"):
        return _fail("  dep licences: uv is not installed, nothing checked", mode)

    # A bare mode string carries no overrides or policy.
    settings = config.setting("quality.dep_licenses")
    settings = settings if isinstance(settings, Mapping) else {}
    try:
        policy = load_policy(repo_policy=settings.get("policy"))
        overrides = parse_overrides(settings.get("overrides"))
    except PolicyError as exc:
        error(f"  dep licences: {exc}")
        return False

    exported = run_cmd(_RUNTIME_EXPORT, check=False, capture=True)
    if exported.returncode != 0:
        emit_tool_output("uv export", exported.stderr)
        return _fail("  dep licences: could not export uv.lock, nothing checked", mode)
    runtime, elsewhere = runtime_packages(exported.stdout)
    if elsewhere:
        info(f"  dep licences: not installed on this platform: {', '.join(elsewhere)}")

    # `uv run` resolves the project's own environment, where `uv python find`
    # follows an inherited VIRTUAL_ENV.
    found = run_cmd(_PROJECT_PYTHON, check=False, capture=True)
    python = found.stdout.strip()
    if found.returncode != 0 or not python:
        return _fail("  dep licences: no project interpreter, nothing checked", mode)

    with tempfile.TemporaryDirectory(prefix="hyperi-ci-licences-") as tmp:
        output = Path(tmp) / "licences.json"
        result = run_cmd(_pip_licenses_cmd(python, output), check=False, capture=True)
        if result.returncode != 0 or not output.is_file():
            emit_tool_output("pip-licenses", result.stderr)
            return _fail("  dep licences: pip-licenses failed, nothing checked", mode)
        packages = json.loads(output.read_text(encoding="utf-8"))

    findings = findings_for(packages, runtime, policy, overrides)
    _write_report(findings, policy)
    info(f"  dep licences: {len(findings)} runtime packages judged, see {REPORT_PATH}")
    held = blocking(findings)
    for finding in held:
        (warn if mode == "warn" else error)(f"  {finding.line()}")
        info(f"    evidence: {finding.evidence}")
    if held:
        return _fail(
            f"  dep licences: {len(held)} package(s) denied or need a human answer. "
            "Answer one in quality.dep_licenses.overrides (name, licence, reason).",
            mode,
        )
    return True
