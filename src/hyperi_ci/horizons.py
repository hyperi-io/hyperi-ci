# Project:   HyperI CI
# File:      src/hyperi_ci/horizons.py
# Purpose:   Report release horizon labels that have outlived their releases
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Back end of ``hyperi-ci labels --stale``.

A horizon label is stale once it has waited ``issues.horizons`` releases of its
own scope since it was applied. A repo horizon counts the repo's GA ``v*``
tags, a program horizon counts the program's releases as
:func:`hyperi_ci.labels.program_releases_since` finds them. ``release-blocker``
holds a cut by design and ``backlog`` is unscheduled, so neither goes stale.

The report also lists gate conflicts and untriaged issues, through
:func:`hyperi_ci.labels_check.check_issues`, the same rule ``labels --check``
and hyperi-ai's ``pm backlog groom`` apply. It reports and never writes.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from hyperi_ci import gh
from hyperi_ci.common import info, success, warn
from hyperi_ci.labels import (
    PROGRAM_HORIZONS,
    NoReleaseYetError,
    ProgramError,
    ga_version,
    gate_of,
    load_manifest,
    program_released,
    program_releases_since,
)
from hyperi_ci.labels_check import IssueRef, Report, check_issues, is_work, label_names

CONFIG_KEY = "issues.horizons"

# Version of the `labels --stale --json` shape, raised when a key is removed or
# retyped.
JSON_SCHEMA = 1

_TAGS_QUERY = """
query($owner: String!, $name: String!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    refs(refPrefix: "refs/tags/", query: "v", first: 100, after: $endCursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        name
        target {
          ... on Tag { tagger { date } }
          ... on Commit { committedDate }
        }
      }
    }
  }
}
"""


class HorizonConfigError(ValueError):
    """``issues.horizons`` is malformed."""


class HorizonReadError(RuntimeError):
    """GitHub could not be read for the report."""


@dataclass(frozen=True, slots=True)
class Thresholds:
    """How many releases each horizon may wait before it is stale.

    Attributes:
        repo: Horizon -> release count, for repo horizons and as the default
            for every program.
        programs: Program -> horizon -> release count, overriding ``repo``.

    """

    repo: Mapping[str, int]
    programs: Mapping[str, Mapping[str, int]]

    def of(self, horizon: str, program: str = "") -> int | None:
        """Return the limit for ``horizon``, in ``program`` when one is named."""
        override = self.programs.get(program, {})
        return override.get(horizon, self.repo.get(horizon))


@dataclass(frozen=True, slots=True)
class StaleItem:
    """An open issue whose horizon label has outlived its releases.

    Attributes:
        number: Issue number.
        title: Issue title.
        label: The horizon label, e.g. ``next-patch`` or ``dfe:next-minor``.
        scope: ``repo`` or ``program``.
        releases_since: Releases of that scope since the label was applied.
        threshold: The configured limit, which ``releases_since`` reached.
        label_applied_at: When the label was last applied, ISO 8601 UTC.
        applied_from: ``labeled_event``, or ``issue_created`` where GitHub
            kept no labeled event for it.

    """

    number: int
    title: str
    label: str
    scope: str
    releases_since: int
    threshold: int
    label_applied_at: str
    applied_from: str


@dataclass(frozen=True, slots=True)
class ProgramStatus:
    """Whether a program's releases could be counted.

    Attributes:
        released_state: ``released``, ``none`` (no GA yet, so its horizons
            cannot go stale) or ``error``.
        released: The last released version, empty unless ``released``.
        error: Why the releases were not counted, empty unless ``error``.

    """

    released_state: str
    released: str
    error: str


@dataclass(slots=True)
class StaleReport:
    """Every finding for one repo.

    Attributes:
        repo: ``owner/name``.
        checked: Horizon labels evaluated.
        stale: Horizon labels past their limit.
        conflicts: Open issues with more than one gate label.
        untriaged: Open issues with no gate label.
        programs: Program -> status, for programs whose labels the repo's
            open issues carry.

    """

    repo: str
    checked: int = 0
    stale: list[StaleItem] = field(default_factory=list)
    conflicts: list[IssueRef] = field(default_factory=list)
    untriaged: list[IssueRef] = field(default_factory=list)
    programs: dict[str, ProgramStatus] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Candidate:
    number: int
    title: str
    created_at: str
    label: str
    horizon: str
    program: str


def _counts(raw: object, where: str, *, complete: bool) -> dict[str, int]:
    if not isinstance(raw, Mapping):
        raise HorizonConfigError(f"`{where}` must map a horizon to a release count")
    counts: dict[str, int] = {}
    for horizon, count in raw.items():
        if horizon not in PROGRAM_HORIZONS:
            raise HorizonConfigError(
                f"`{where}.{horizon}`: only {', '.join(PROGRAM_HORIZONS)} go stale"
            )
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise HorizonConfigError(
                f"`{where}.{horizon}` must be a whole number of releases, 1 or more"
            )
        counts[horizon] = count
    missing = [h for h in PROGRAM_HORIZONS if h not in counts]
    if complete and missing:
        raise HorizonConfigError(f"`{where}` has no `{missing[0]}`")
    return counts


def parse_thresholds(raw: object) -> Thresholds:
    """Validate a merged ``issues.horizons`` block.

    Args:
        raw: ``stale_after`` mapping each ``next-*`` horizon to a release
            count, and ``programs`` mapping a program to overrides of it.

    Returns:
        The :class:`Thresholds`.

    Raises:
        HorizonConfigError: A count is not a whole number of 1 or more, a key
            is not a ``next-*`` horizon, or a program is not in the manifest.

    """
    if not isinstance(raw, Mapping):
        raise HorizonConfigError(f"`{CONFIG_KEY}` must be a mapping")
    repo = _counts(raw.get("stale_after"), f"{CONFIG_KEY}.stale_after", complete=True)
    entries = raw.get("programs") or {}
    if not isinstance(entries, Mapping):
        raise HorizonConfigError(f"`{CONFIG_KEY}.programs` must be a mapping")
    known = load_manifest().programs
    programs: dict[str, Mapping[str, int]] = {}
    for name, entry in entries.items():
        where = f"{CONFIG_KEY}.programs.{name}"
        if name not in known:
            raise HorizonConfigError(f"`{where}`: no such program in the manifest")
        programs[name] = MappingProxyType(_counts(entry, where, complete=False))
    return Thresholds(MappingProxyType(repo), MappingProxyType(programs))


def overlay(shipped: Mapping[str, Any], project: object) -> dict[str, Any]:
    """Lay a repo's ``issues.horizons`` over the shipped one, a section at a time.

    For a repo read from GitHub, which :func:`hyperi_ci.config.load_config`
    never merged.

    Raises:
        HorizonConfigError: ``project`` is set and is not a mapping.

    """
    merged = {k: dict(v) if isinstance(v, Mapping) else v for k, v in shipped.items()}
    if project is None:
        return merged
    if not isinstance(project, Mapping):
        raise HorizonConfigError(f"`{CONFIG_KEY}` must be a mapping")
    for key, value in project.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def _when(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_list(endpoint: str) -> list[Any]:
    result = gh.gh_api([endpoint, "--paginate"])
    if not isinstance(result.data, list):
        raise HorizonReadError(result.reason or f"{endpoint} was not a JSON array")
    return result.data


def repo_release_dates(repo: str) -> list[datetime]:
    """Return when each GA ``v*`` tag of ``repo`` was made.

    An annotated tag's tagger date, else the tagged commit's date: GitHub keeps
    no creation time for a lightweight tag. Prerelease tags are skipped.

    Raises:
        HorizonReadError: The tags could not be read.

    """
    owner, name = repo.split("/", 1)
    result = gh.gh_api(
        [
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-f",
            f"query={_TAGS_QUERY}",
            "--paginate",
            "--slurp",
            "graphql",
        ]
    )
    if not isinstance(result.data, list):
        raise HorizonReadError(result.reason or f"the tags of {repo} were not JSON")
    dates: list[datetime] = []
    for page in result.data:
        try:
            nodes = page["data"]["repository"]["refs"]["nodes"]
        except (KeyError, TypeError) as exc:
            raise HorizonReadError(f"the tags of {repo} could not be read") from exc
        for node in nodes:
            tag = node.get("name") if isinstance(node, Mapping) else None
            if not isinstance(tag, str) or not tag.startswith("v"):
                continue
            if ga_version(tag) is None:
                continue
            target = node.get("target") or {}
            tagger = target.get("tagger") or {}
            stamp = tagger.get("date") or target.get("committedDate")
            if isinstance(stamp, str):
                dates.append(_when(stamp))
    return dates


def _candidates(
    issues: Iterable[Mapping[str, Any]], thresholds: Thresholds
) -> list[_Candidate]:
    """Return each issue's countable gate, and its strictest label per program."""
    programs = load_manifest().programs.values()
    found: list[_Candidate] = []
    for issue in issues:
        number = issue.get("number")
        if not is_work(issue) or not isinstance(number, int):
            continue
        title = str(issue.get("title") or "")
        created = str(issue.get("created_at") or "")
        names = label_names(issue)
        gate = gate_of(names)
        if gate is not None and thresholds.of(gate) is not None:
            found.append(_Candidate(number, title, created, gate, gate, ""))
        present = {name.casefold() for name in names}
        for program in programs:
            for label, horizon in zip(program.labels, program.horizons, strict=True):
                if label.name.casefold() in present:
                    found.append(
                        _Candidate(
                            number, title, created, label.name, horizon, program.name
                        )
                    )
                    break
    return found


def _program_status(name: str) -> ProgramStatus:
    try:
        return ProgramStatus("released", program_released(name), "")
    except NoReleaseYetError:
        return ProgramStatus("none", "", "")
    except ProgramError as exc:
        return ProgramStatus("error", "", str(exc))


def _applied(
    repo: str, item: _Candidate, events: dict[int, list[Any]]
) -> tuple[datetime, str]:
    """When ``item``'s label was last applied, and where that came from."""
    if item.number not in events:
        events[item.number] = _read_list(
            f"repos/{repo}/issues/{item.number}/events?per_page=100"
        )
    stamps = [
        event["created_at"]
        for event in events[item.number]
        if isinstance(event, Mapping)
        and event.get("event") == "labeled"
        and isinstance(event.get("label"), Mapping)
        and str(event["label"].get("name", "")).casefold() == item.label.casefold()
        and isinstance(event.get("created_at"), str)
    ]
    if stamps:
        return max(_when(stamp) for stamp in stamps), "labeled_event"
    return _when(item.created_at), "issue_created"


def stale_report(repo: str, thresholds: Thresholds) -> StaleReport:
    """Build the report for ``repo``, reading GitHub through ``gh api``.

    Reads the open issues, then the issue events of each one carrying a
    countable horizon, the repo's tags when a repo horizon is in play, and a
    program's release history from the oldest of its labels on.

    Args:
        repo: ``owner/name``.
        thresholds: The validated limits.

    Returns:
        The :class:`StaleReport`. A program whose releases could not be
        counted is reported in ``programs``, not raised.

    Raises:
        HorizonReadError: The issues, an issue's events or the tags could not
            be read.

    """
    issues = _read_list(f"repos/{repo}/issues?state=open&per_page=100")
    triage = Report()
    check_issues(issues, {}, triage)
    report = StaleReport(repo, conflicts=triage.conflict, untriaged=triage.no_gate)

    candidates = _candidates(issues, thresholds)
    for name in sorted({c.program for c in candidates if c.program}):
        report.programs[name] = _program_status(name)

    events: dict[int, list[Any]] = {}
    applied: dict[tuple[int, str], tuple[datetime, str]] = {}
    for item in candidates:
        if item.program and report.programs[item.program].released_state != "released":
            continue
        applied[(item.number, item.label)] = _applied(repo, item, events)

    releases: dict[str, list[datetime]] = {}
    if any(not c.program for c in candidates):
        releases[""] = repo_release_dates(repo)
    for name, status in report.programs.items():
        if status.released_state != "released":
            continue
        since = min(
            applied[(c.number, c.label)][0] for c in candidates if c.program == name
        )
        try:
            found = program_releases_since(name, _iso(since))
        except ProgramError as exc:
            report.programs[name] = replace(
                status, released_state="error", error=str(exc)
            )
            continue
        releases[name] = [_when(release.at) for release in found]

    for item in candidates:
        key = (item.number, item.label)
        if key not in applied or item.program not in releases:
            continue
        report.checked += 1
        when, source = applied[key]
        count = sum(1 for at in releases[item.program] if at > when)
        limit = thresholds.of(item.horizon, item.program)
        if limit is not None and count >= limit:
            report.stale.append(
                StaleItem(
                    number=item.number,
                    title=item.title,
                    label=item.label,
                    scope="program" if item.program else "repo",
                    releases_since=count,
                    threshold=limit,
                    label_applied_at=_iso(when),
                    applied_from=source,
                )
            )
    return report


def report_json(report: StaleReport) -> dict[str, Any]:
    """Return ``report`` as the ``labels --stale --json`` document."""
    return {
        "schema": JSON_SCHEMA,
        "repo": report.repo,
        "checked": report.checked,
        "stale": [
            {
                "number": item.number,
                "title": item.title,
                "label": item.label,
                "scope": item.scope,
                "releases_since": item.releases_since,
                "threshold": item.threshold,
                "label_applied_at": item.label_applied_at,
                "applied_from": item.applied_from,
            }
            for item in report.stale
        ],
        "conflicts": [
            {"number": ref.number, "title": ref.title, "labels": ref.detail.split(", ")}
            for ref in report.conflicts
        ],
        "untriaged": [
            {"number": ref.number, "title": ref.title} for ref in report.untriaged
        ],
        "programs": {
            name: {
                "released_state": status.released_state,
                "released": status.released,
                "error": status.error,
            }
            for name, status in report.programs.items()
        },
    }


def print_stale(report: StaleReport) -> None:
    """Log ``report``, one line per finding."""
    info(f"labels --stale: {report.repo}, {report.checked} horizon labels checked")
    for item in report.stale:
        warn(
            f"stale: #{item.number} {item.title} [{item.label}] waited "
            f"{item.releases_since} {item.scope} releases since "
            f"{item.label_applied_at[:10]}, limit {item.threshold}"
        )
    for ref in report.conflicts:
        warn(f"conflict: #{ref.number} {ref.title} [{ref.detail}], the strictest wins")
    for ref in report.untriaged:
        warn(f"untriaged: #{ref.number} {ref.title}, no gate label")
    for name, status in report.programs.items():
        if status.released_state == "none":
            info(f"program {name}: no GA release yet, so its horizons cannot go stale")
        elif status.released_state == "error":
            warn(f"program {name}: releases not counted: {status.error}")
    if not (report.stale or report.conflicts or report.untriaged):
        success("no stale horizons, gate conflicts or untriaged issues")
