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

from packaging.licenses import InvalidLicenseExpression, canonicalize_license_expression
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from hyperi_ci.common import error, info, run_cmd
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import emit_tool_output, resolve_tool_mode
from hyperi_ci.quality.bsd_text import BARE_BSD, bsd_licence
from hyperi_ci.quality.dep_licenses import (
    Finding,
    Mode,
    NoticeEntry,
    Override,
    Policy,
    PolicyError,
    Verdict,
    fail,
    find_override,
    judge,
    load_settings,
    project_path,
    quote,
)
from hyperi_ci.quality.upstream_licences import pick_repository
from hyperi_ci.quality.vendored_licenses import finish_with_copied_in
from hyperi_ci.versions import tool_version

# Run in the project interpreter with -I, so no project code is imported.
_PROJECT_URLS_SCRIPT = (
    "import importlib.metadata as m, json\n"
    "urls = {}\n"
    "for d in m.distributions():\n"
    "    name = d.metadata['Name']\n"
    "    if name:\n"
    "        urls[name] = d.metadata.get_all('Project-URL') or []\n"
    "print(json.dumps(urls))\n"
)

# Project-URL labels that name the code, and those that never do.
_SOURCE_LABELS = ("source", "code", "repo", "git")
_TRACKER_LABELS = ("issue", "bug", "tracker")

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

_UNKNOWN = "UNKNOWN"

_PROJECT_PYTHON = [
    "uv",
    "run",
    "--no-sync",
    "python",
    "-c",
    "import sys; print(sys.executable)",
]


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


def declared_licence(record: Mapping[str, Any]) -> str:
    """Return the most specific licence a pip-licenses ``--from=all`` record declares.

    ``License-Expression`` first, then a ``License`` field that parses as SPDX,
    then the trove classifiers, then whatever ``License`` holds. pip-licenses'
    own ``mixed`` order puts the classifiers first, so ``License: BSD-3-Clause``
    reads as a bare ``BSD License``.

    Args:
        record: One pip-licenses JSON record.

    Returns:
        The licence to judge, ``UNKNOWN`` when the record declares none.

    """
    expression = str(record.get("License-Expression") or _UNKNOWN)
    if expression != _UNKNOWN:
        return expression
    metadata = str(record.get("License-Metadata") or _UNKNOWN)
    try:
        canonicalize_license_expression(metadata)
    except InvalidLicenseExpression:
        classifier = str(record.get("License-Classifier") or _UNKNOWN)
        return classifier if classifier != _UNKNOWN else metadata
    return metadata


def findings_for(
    packages: Sequence[Mapping[str, Any]],
    runtime: Mapping[str, str],
    policy: Policy,
    overrides: Sequence[Override],
) -> list[Finding]:
    """Judge each runtime package pip-licenses reported.

    A runtime package pip-licenses did not report is ask: it was never seen.
    A bare ``BSD License`` is judged as the BSD licence its file's full text is.

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
                    "in uv.lock but not installed: an extra the venv was not synced with? Run `uv sync --all-extras`",
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
        named = None
        if source == "metadata" and raw.strip().upper() in BARE_BSD:
            named = bsd_licence(licence_text)
        if named is not None:
            spdx, verdict, obligations, rule = judge(named, Mode.IMPORT, policy)
            source = "license-file"
            evidence = f"{project_path(package.get('LicenseFile'))}: {named} text"
        elif rule == "undeclared" and licence_text not in (None, "UNKNOWN"):
            source = "license-file"
            evidence = (
                f"{project_path(package.get('LicenseFile'))}: {quote(licence_text)}"
            )
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
        "--from=all",
        "--with-license-file",
        "--with-notice-file",
        "--with-urls",
        # pip-licenses otherwise drops its own dependencies (wcwidth, prettytable).
        "--with-system",
        "--output-file",
        str(output),
    ]


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
    try:
        policy, overrides = load_settings(config)
    except PolicyError as exc:
        error(f"  dep licences: {exc}")
        return False
    if not Path("uv.lock").is_file():
        info("  dep licences: no uv.lock, so only copied-in files are judged")
        return finish_with_copied_in(config, [], [], policy, {}, mode, overrides)
    if not shutil.which("uv"):
        return fail("  dep licences: uv is not installed, nothing checked", mode)

    exported = run_cmd(_RUNTIME_EXPORT, check=False, capture=True)
    if exported.returncode != 0:
        emit_tool_output("uv export", exported.stderr)
        return fail("  dep licences: could not export uv.lock, nothing checked", mode)
    runtime, elsewhere = runtime_packages(exported.stdout)
    if elsewhere:
        info(f"  dep licences: not installed on this platform: {', '.join(elsewhere)}")

    # `uv run` resolves the project's own environment, where `uv python find`
    # follows an inherited VIRTUAL_ENV.
    found = run_cmd(_PROJECT_PYTHON, check=False, capture=True)
    python = found.stdout.strip()
    if found.returncode != 0 or not python:
        return fail("  dep licences: no project interpreter, nothing checked", mode)

    with tempfile.TemporaryDirectory(prefix="hyperi-ci-licences-") as tmp:
        output = Path(tmp) / "licences.json"
        result = run_cmd(_pip_licenses_cmd(python, output), check=False, capture=True)
        if result.returncode != 0 or not output.is_file():
            emit_tool_output("pip-licenses", result.stderr)
            return fail("  dep licences: pip-licenses failed, nothing checked", mode)
        packages = [
            {**record, "License": declared_licence(record)}
            for record in json.loads(output.read_text(encoding="utf-8"))
        ]

    findings = findings_for(packages, runtime, policy, overrides)
    tools = {"pip-licenses": tool_version("pip-licenses")}
    entries = notice_entries(packages, findings, project_urls(python))
    return finish_with_copied_in(
        config, findings, entries, policy, tools, mode, overrides
    )


def project_urls(python: str) -> dict[str, list[tuple[str, str]]]:
    """Return each installed distribution's ``Project-URL`` links.

    Args:
        python: The project interpreter.

    Returns:
        Canonical name to ``(label, url)`` pairs, empty when the interpreter
        could not be asked.

    """
    result = run_cmd(
        [python, "-I", "-c", _PROJECT_URLS_SCRIPT], check=False, capture=True
    )
    if result.returncode != 0:
        return {}
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    links: dict[str, list[tuple[str, str]]] = {}
    for name, entries in raw.items():
        pairs = []
        for entry in entries:
            label, _, url = str(entry).partition(",")
            pairs.append((label.strip(), url.strip()))
        links[canonicalize_name(name)] = pairs
    return links


def repository(links: Sequence[tuple[str, str]], url: str | None) -> str | None:
    """Return the link most likely to be the package's repository.

    A source-code link comes first, then the homepage pip-licenses reports,
    then any other link but an issue tracker.
    """

    def labelled(label: str, words: tuple[str, ...]) -> bool:
        return any(word in label.lower() for word in words)

    source = [u for label, u in links if labelled(label, _SOURCE_LABELS)]
    rest = [
        u for label, u in links if not labelled(label, _SOURCE_LABELS + _TRACKER_LABELS)
    ]
    return pick_repository([*source, url, *rest])


def notice_entries(
    packages: Sequence[Mapping[str, Any]],
    findings: Sequence[Finding],
    links: Mapping[str, Sequence[tuple[str, str]]] | None = None,
) -> list[NoticeEntry]:
    """Pair each finding with the licence and NOTICE text its package shipped."""
    by_name = {canonicalize_name(str(p.get("Name", ""))): p for p in packages}
    entries = []
    for finding in findings:
        name = canonicalize_name(finding.name)
        package = by_name.get(name, {})
        found = repository((links or {}).get(name, ()), package.get("URL"))
        entries.append(
            NoticeEntry(
                finding,
                package.get("LicenseText"),
                package.get("NoticeText"),
                found,
            )
        )
    return entries
