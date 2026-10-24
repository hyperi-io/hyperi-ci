# Project:   HyperI CI
# File:      src/hyperi_ci/labels_check.py
# Purpose:   Compare a repo's labels and open issues against the label manifest
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Back end of ``hyperi-ci labels --check``.

Read-only. :func:`fetch` reads through ``gh api`` with the ambient token, and
:func:`check_labels` / :func:`check_issues` are pure, so the report is testable
without GitHub. Creating and fixing labels is hyperi-infra's reconcile, not
this.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import yaml

from hyperi_ci.common import info, success, warn
from hyperi_ci.gh import RepoFileError, gh_api, read_repo_file
from hyperi_ci.labels import (
    AreasError,
    Manifest,
    area_label,
    conflicts,
    expected_labels,
    gate_of,
    is_allowed,
    load_manifest,
    parse_areas,
    retired_description,
)
from hyperi_ci.project_config import CONFIG_FILES

# Renovate's dashboard is a findings list, not work, and dfe-sweep skips it too.
_DASHBOARD_TITLE = "Dependency Dashboard"


@dataclass(frozen=True, slots=True)
class Drift:
    """A label present with the wrong name case, colour or description.

    Attributes:
        name: The label as it exists on the repo.
        field: ``name``, ``color`` or ``description``.
        actual: What the repo has.
        expected: What the manifest says.

    """

    name: str
    field: str
    actual: str
    expected: str


@dataclass(frozen=True, slots=True)
class Extra:
    """A label nothing in the manifest accounts for.

    Attributes:
        name: The label as it exists on the repo.
        hint: What to do with it, empty when the manifest has no opinion.

    """

    name: str
    hint: str


@dataclass(frozen=True, slots=True)
class IssueRef:
    """An open issue named in a finding.

    Attributes:
        number: Issue number.
        title: Issue title.
        detail: The labels behind the finding, where there are any.

    """

    number: int
    title: str
    detail: str = ""


@dataclass(slots=True)
class Report:
    """Every finding for one repo.

    Attributes:
        missing: Expected labels the repo lacks.
        drift: Labels whose name case, colour or description is wrong.
        extra: Labels outside the manifest.
        no_gate: Open issues with no gate label.
        conflict: Open issues with more than one gate label.
        no_type: Open issues with no native issue type.
        no_area: Open issues with no area label, when the repo declares areas.
        retired: Open issues still carrying a retired label.

    """

    missing: list[str] = field(default_factory=list)
    drift: list[Drift] = field(default_factory=list)
    extra: list[Extra] = field(default_factory=list)
    no_gate: list[IssueRef] = field(default_factory=list)
    conflict: list[IssueRef] = field(default_factory=list)
    no_type: list[IssueRef] = field(default_factory=list)
    no_area: list[IssueRef] = field(default_factory=list)
    retired: list[IssueRef] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """True when there is no finding of any class."""
        return not any(
            (
                self.missing,
                self.drift,
                self.extra,
                self.no_gate,
                self.conflict,
                self.no_type,
                self.no_area,
                self.retired,
            )
        )


def _str(entry: Mapping[str, Any], key: str) -> str:
    value = entry.get(key)
    return value if isinstance(value, str) else ""


def check_labels(
    present: Iterable[Mapping[str, Any]],
    areas: Mapping[str, Iterable[str]],
    report: Report,
    *,
    manifest: Manifest | None = None,
) -> None:
    """Compare the repo's labels with the manifest, adding to ``report``.

    Community and program labels are checked for drift where present and
    never reported missing.

    Args:
        present: The repo's labels as `GET /repos/{repo}/labels` returns them.
        areas: The repo's validated ``issues.areas``.
        report: Collects the missing, drift and extra findings.
        manifest: The manifest to check against, :func:`load_manifest` when
            None.

    """
    if manifest is None:
        manifest = load_manifest()
    wanted = expected_labels(areas, manifest=manifest)
    expected = {label.name.casefold(): label for label in wanted}
    # Program labels stay on a repo that left the program, so none is extra.
    program_labels = [
        label for program in manifest.programs.values() for label in program.labels
    ]
    optional = {
        label.name.casefold(): label for label in (*manifest.community, *program_labels)
    }
    bots = {name.casefold() for name in manifest.bot_owned}
    retired = {name.casefold(): new for name, new in manifest.retired.items()}
    area_prefix = manifest.area_prefix.casefold()

    seen: set[str] = set()
    for entry in present:
        name = _str(entry, "name")
        if not name:
            continue
        key = name.casefold()
        seen.add(key)
        want = expected.get(key) or optional.get(key)
        if want is not None:
            _compare(name, entry, want.name, want.color, want.description, report)
        elif key in bots or is_allowed(name, manifest=manifest):
            continue
        elif key in retired:
            marked = retired_description(retired[key], manifest=manifest)
            if _str(entry, "description") != marked:
                report.extra.append(Extra(name, f'retired: set description "{marked}"'))
        elif key.startswith(area_prefix):
            report.extra.append(Extra(name, "area not declared in issues.areas"))
        else:
            report.extra.append(Extra(name, ""))

    report.missing.extend(
        label.name for key, label in expected.items() if key not in seen
    )


def _compare(
    name: str,
    entry: Mapping[str, Any],
    want_name: str,
    color: str,
    description: str,
    report: Report,
) -> None:
    if name != want_name:
        report.drift.append(Drift(name, "name", name, want_name))
    actual_color = _str(entry, "color").lower()
    if actual_color != color:
        report.drift.append(Drift(name, "color", actual_color, color))
    actual_description = _str(entry, "description")
    if actual_description != description:
        report.drift.append(Drift(name, "description", actual_description, description))


def label_names(issue: Mapping[str, Any]) -> list[str]:
    """Return the label names of an issue as the REST API returns it."""
    labels = issue.get("labels")
    if not isinstance(labels, list):
        return []
    names: list[str] = []
    for label in labels:
        if isinstance(label, Mapping):
            names.append(_str(label, "name"))
        elif isinstance(label, str):
            names.append(label)
    return names


def is_work(issue: Mapping[str, Any]) -> bool:
    """Return False for a pull request or Renovate's Dependency Dashboard.

    The issues endpoint returns pull requests too, and the dashboard is a
    findings list. hyperi-ai's ``pm backlog groom`` skips the same two.
    """
    if "pull_request" in issue:
        return False
    user = issue.get("user")
    is_bot = isinstance(user, Mapping) and user.get("type") == "Bot"
    return not (is_bot and _str(issue, "title") == _DASHBOARD_TITLE)


def check_issues(
    issues: Iterable[Mapping[str, Any]],
    areas: Mapping[str, Iterable[str]],
    report: Report,
    *,
    manifest: Manifest | None = None,
) -> None:
    """Triage-check the repo's open issues, adding to ``report``.

    Pull requests come back from the issues endpoint too and are skipped, as
    is Renovate's Dependency Dashboard.

    Args:
        issues: Open issues as `GET /repos/{repo}/issues` returns them.
        areas: The repo's validated ``issues.areas``. The area check runs only
            when it is non-empty.
        report: Collects the no-gate, conflict, no-type, no-area and retired
            findings.
        manifest: The manifest to check against, :func:`load_manifest` when
            None.

    """
    if manifest is None:
        manifest = load_manifest()
    area_names = {area_label(name, manifest=manifest).casefold() for name in areas}
    retired = {name.casefold(): new for name, new in manifest.retired.items()}
    for issue in issues:
        if not is_work(issue):
            continue
        number = issue.get("number")
        if not isinstance(number, int):
            continue
        title = _str(issue, "title")
        names = label_names(issue)
        clash = conflicts(names, manifest=manifest)
        if clash:
            report.conflict.append(IssueRef(number, title, ", ".join(clash)))
        elif gate_of(names, manifest=manifest) is None:
            report.no_gate.append(IssueRef(number, title))
        if not issue.get("type"):
            report.no_type.append(IssueRef(number, title))
        if area_names and not any(n.casefold() in area_names for n in names):
            report.no_area.append(IssueRef(number, title))
        stale = [
            f"{n} -> {retired[n.casefold()]}" for n in names if n.casefold() in retired
        ]
        if stale:
            report.retired.append(IssueRef(number, title, ", ".join(stale)))


def fetch(repo: str) -> tuple[list[dict], list[dict], str]:
    """Read every label and every open issue of ``repo``.

    Args:
        repo: ``owner/name``.

    Returns:
        ``(labels, issues, "")``, or ``([], [], reason)`` when either read
        failed.

    """
    labels = gh_api([f"repos/{repo}/labels?per_page=100", "--paginate"])
    if not isinstance(labels.data, list):
        return [], [], labels.reason or "the label list was not a JSON array"
    issues = gh_api([f"repos/{repo}/issues?state=open&per_page=100", "--paginate"])
    if not isinstance(issues.data, list):
        return [], [], issues.reason or "the issue list was not a JSON array"
    return labels.data, issues.data, ""


class RemoteConfigError(ValueError):
    """A repo's config on its default branch could not be read or parsed."""


def remote_issues(repo: str) -> dict[str, Any]:
    """Read the ``issues`` block of ``repo``'s config on its default branch.

    Args:
        repo: ``owner/name``.

    Returns:
        The block, empty when the repo has no config or no ``issues``.

    Raises:
        RemoteConfigError: The config could not be read or is not YAML.

    """
    for name in CONFIG_FILES:
        try:
            text = read_repo_file(repo, name)
        except RepoFileError as exc:
            raise RemoteConfigError(str(exc)) from exc
        if text is None:
            continue
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise RemoteConfigError(f"{repo}:{name} is not valid YAML: {exc}") from exc
        issues = doc.get("issues") if isinstance(doc, dict) else None
        return dict(issues) if isinstance(issues, dict) else {}
    return {}


def remote_areas(repo: str) -> dict[str, tuple[str, ...]]:
    """Read ``issues.areas`` from ``repo``'s config on its default branch.

    Args:
        repo: ``owner/name``.

    Returns:
        The validated areas, empty when the repo has no config or no areas.

    Raises:
        AreasError: The config could not be read, is not YAML, or its areas
            block is malformed.

    """
    try:
        issues = remote_issues(repo)
    except RemoteConfigError as exc:
        raise AreasError(str(exc)) from exc
    return parse_areas(issues.get("areas"))


def _section(title: str, lines: list[str]) -> None:
    if not lines:
        return
    warn(f"{title} ({len(lines)}):")
    for line in lines:
        warn(f"  {line}")


def _issue_line(ref: IssueRef) -> str:
    line = f"#{ref.number} {ref.title}"
    return f"{line} [{ref.detail}]" if ref.detail else line


def print_report(repo: str, report: Report) -> None:
    """Log ``report`` grouped by finding class, one line per finding."""
    info(f"labels --check: {repo}")
    _section("Missing labels", report.missing)
    _section(
        "Drifted labels",
        [
            f"{d.name}: {d.field} is {d.actual!r}, expected {d.expected!r}"
            for d in report.drift
        ],
    )
    _section(
        "Extra labels",
        [f"{e.name} -- {e.hint}" if e.hint else e.name for e in report.extra],
    )
    _section("Open issues with no gate label", [_issue_line(r) for r in report.no_gate])
    _section(
        "Open issues with more than one gate label",
        [_issue_line(r) for r in report.conflict],
    )
    _section("Open issues with no issue type", [_issue_line(r) for r in report.no_type])
    _section("Open issues with no area label", [_issue_line(r) for r in report.no_area])
    _section(
        "Open issues with a retired label",
        [_issue_line(r) for r in report.retired],
    )
    if report.clean:
        success("labels and open issues match the manifest")
