# Project:   HyperI CI
# File:      src/hyperi_ci/runner_report.py
# Purpose:   Table the runner-fit records of a fleet's latest Rust Builds
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""``hyperi-ci runner report``: one row per repo and arch from runner-fit records.

Each Rust Build uploads ``runner-fit.json`` as a ``runner-fit-<os_arch>``
artifact. The newest of a repo's latest runs that carries any is tabled.
"""

import json
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from hyperi_ci.common import error, warn
from hyperi_ci.gh import ApiResult, gh_api, gh_run
from hyperi_ci.languages.rust.runner_fit import RECORD_FILE

DEFAULT_OWNER = "hyperi-io"
RUNS_PER_REPO = 20
ARTIFACT_PREFIX = "runner-fit-"

COLUMNS = (
    "repo",
    "arch",
    "runner",
    "cpus",
    "limit GiB",
    "jobs",
    "total min",
    "max peak GiB",
    "per-job GiB",
    "status",
    "recommended",
    "move",
)

type Api = Callable[[list[str]], ApiResult]
type Download = Callable[[str, int, Path], None]


class ReportError(RuntimeError):
    """gh failed, so a repo's records could not be fetched."""


def qualify(repo: str) -> str:
    """Return ``owner/name``, with the default owner for a bare name."""
    return repo if "/" in repo else f"{DEFAULT_OWNER}/{repo}"


def run_ids(payload: object) -> list[int]:
    """Return the run ids in a ``actions/runs`` response, newest first."""
    runs = payload.get("workflow_runs") if isinstance(payload, Mapping) else None
    ids = [r.get("id") for r in runs or [] if isinstance(r, Mapping)]
    return [i for i in ids if isinstance(i, int)]


def has_records(payload: object) -> bool:
    """Return True when a run's ``artifacts`` response lists a live record."""
    listed = payload.get("artifacts") if isinstance(payload, Mapping) else None
    return any(
        isinstance(a, Mapping)
        and str(a.get("name", "")).startswith(ARTIFACT_PREFIX)
        and not a.get("expired")
        for a in listed or []
    )


def download_records(repo: str, run_id: int, dest: Path) -> None:
    """Download a run's ``runner-fit-*`` artifacts into ``dest``.

    Raises:
        ReportError: gh exited non-zero.

    """
    result = gh_run(
        [
            "run",
            "download",
            str(run_id),
            "--repo",
            repo,
            "--pattern",
            f"{ARTIFACT_PREFIX}*",
            "--dir",
            str(dest),
        ],
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or f"exit {result.returncode}"
        raise ReportError(f"gh run download {run_id} for {repo} failed: {detail}")


def read_records(directory: Path) -> list[dict[str, Any]]:
    """Return every ``runner-fit.json`` under ``directory``, bad files skipped."""
    records = []
    for path in sorted(directory.rglob(RECORD_FILE)):
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError) as exc:
            warn(f"  {path.name} in {path.parent.name} is unreadable: {exc}")
            continue
        if isinstance(data, dict):
            records.append(data)
    return records


def fetch_records(
    repo: str,
    run_id: int | None = None,
    *,
    api: Api = gh_api,
    download: Download = download_records,
) -> tuple[int | None, list[dict[str, Any]]]:
    """Return the run and records of the newest run with runner-fit artifacts.

    Args:
        repo: ``owner/name``.
        run_id: Look at this run only, instead of the latest ``RUNS_PER_REPO``.
        api: Calls ``gh api``, injectable for tests.
        download: Fetches a run's artifacts into a directory, injectable for tests.

    Returns:
        ``(run id, records)``, or ``(None, [])`` when no run carries a record.

    Raises:
        ReportError: gh failed.

    """
    if run_id is None:
        listing = api([f"repos/{repo}/actions/runs?per_page={RUNS_PER_REPO}"])
        if listing.reason:
            raise ReportError(listing.reason)
        candidates = run_ids(listing.data)
    else:
        candidates = [run_id]

    for candidate in candidates:
        artifacts = api([f"repos/{repo}/actions/runs/{candidate}/artifacts"])
        if artifacts.reason:
            raise ReportError(artifacts.reason)
        if not has_records(artifacts.data):
            continue
        with tempfile.TemporaryDirectory(prefix="runner-fit-") as scratch:
            download(repo, candidate, Path(scratch))
            records = read_records(Path(scratch))
        if records:
            return candidate, records
    return None, []


def _number(value: object, digits: int = 1) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def row_for(repo: str, record: Mapping[str, Any]) -> list[str]:
    """Return the table cells for one record."""
    verdict = record.get("verdict") or {}
    total = record.get("total_seconds")
    return [
        repo,
        str(record.get("arch") or "-"),
        str(record.get("runner_label") or "-"),
        str(record.get("cpu_budget") or "-"),
        _number(record.get("memory_limit_gib")),
        str(record.get("cargo_jobs") or "-"),
        _number(None if total is None else total / 60),
        _number(record.get("max_peak_gib")),
        _number(record.get("per_job_peak_gib")),
        str(verdict.get("status") or "-"),
        str(verdict.get("recommended") or "-"),
        "yes" if verdict.get("move") else "no",
    ]


def missing_row(repo: str, run_id: int | None) -> list[str]:
    """Return the table cells for a repo with no record."""
    where = f"run {run_id}" if run_id else f"the last {RUNS_PER_REPO} runs"
    cells = [repo, "-", f"no runner-fit record in {where}"]
    return cells + ["-"] * (len(COLUMNS) - len(cells))


def render_table(rows: Sequence[Sequence[str]]) -> str:
    """Align ``rows`` under ``COLUMNS``."""
    table = [COLUMNS, *rows]
    widths = [max(len(row[i]) for row in table) for i in range(len(COLUMNS))]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip()
        for row in table
    )


def report(
    repos: Sequence[str],
    *,
    run_id: int | None = None,
    as_json: bool = False,
    api: Api = gh_api,
    download: Download = download_records,
) -> tuple[str, int]:
    """Build the report for ``repos``.

    Args:
        repos: ``owner/name``, or a bare name for the default owner.
        run_id: Look at this run only. Needs a single repo.
        as_json: Return the records as JSON instead of a table.
        api: Calls ``gh api``, injectable for tests.
        download: Fetches a run's artifacts, injectable for tests.

    Returns:
        ``(text, exit code)``. The code is 1 when gh failed for any repo, and
        the repos that worked are still reported.

    """
    rows: list[list[str]] = []
    notes: list[str] = []
    found: list[dict[str, Any]] = []
    rc = 0
    for name in repos:
        repo = qualify(name)
        try:
            used, records = fetch_records(repo, run_id, api=api, download=download)
        except ReportError as exc:
            error(str(exc))
            rc = 1
            continue
        if not records:
            rows.append(missing_row(repo, run_id))
            continue
        for record in sorted(records, key=lambda r: str(r.get("arch"))):
            found.append({**record, "repo": repo, "run": used})
            rows.append(row_for(repo, record))
            reason = (record.get("verdict") or {}).get("move_reason")
            if reason:
                notes.append(f"{repo} {record.get('arch')}: {reason}")
    if as_json:
        return json.dumps(found, indent=2), rc
    text = render_table(rows)
    if notes:
        text += "\n\n" + "\n".join(notes)
    return text, rc
