# Project:   HyperI CI
# File:      src/hyperi_ci/workflows.py
# Purpose:   Read a repo's workflows and which of them hyperi-ci scaffolded
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Workflow inventory, and the ownership reading light touch turns on.

hyperi-ci scaffolds exactly one workflow into a consumer repo: a
``.github/workflows/ci.yml`` whose job calls a
``hyperi-io/hyperi-ci/.github/workflows/<lang>-ci.yml`` reusable
workflow. That ``uses:`` line is the ownership marker, and it is read
from the file rather than from a declaration, so a repo needs no new
config key to be understood.

Every other workflow in the repo is FOREIGN: upstream's, another team's,
or hand-written. Against a foreign workflow hyperi-ci is a convenience
over ``gh``, never a replacement that fails closed -- it forwards what it
can and stands down, naming the workflow, where it cannot help.

Ownership is per WORKFLOW, not per repo. dfe-hyperdx carries a
hyperi-ci-scaffolded ``ci.yml`` alongside six workflows it wrote itself;
both readings are true of the same repo at the same time.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

# The reusable-workflow prefix a scaffolded ci.yml calls. A workflow
# carrying it in a `uses:` is one hyperi-ci owns.
OWNED_USES_PREFIX = "hyperi-io/hyperi-ci/.github/workflows/"

_WORKFLOW_DIR = Path(".github/workflows")
_WORKFLOW_SUFFIXES = (".yml", ".yaml")


@dataclass(frozen=True, slots=True)
class Workflow:
    """One workflow file, and whether hyperi-ci scaffolded it.

    Attributes:
        path: Absolute path to the workflow file.
        filename: Base name, as ``gh workflow run`` accepts it.
        name: The display name ``gh run list`` reports as
            ``workflowName`` -- the ``name:`` key, or the repo-relative
            path when the file declares none.
        owned: The file calls a hyperi-ci reusable workflow.

    """

    path: Path
    filename: str
    name: str
    owned: bool


def _declared_name(data: object) -> str | None:
    """Return the workflow's ``name:`` key, or None when it declares none."""
    if not isinstance(data, dict):
        return None
    name = data.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def _calls_hyperi_ci(data: object, text: str) -> bool:
    """Return True when any job delegates to a hyperi-ci reusable workflow.

    Reads the parsed jobs where the YAML is well-formed, and falls back to
    scanning the raw text so an unparseable workflow is still recognised.
    """
    if isinstance(data, dict):
        jobs = data.get("jobs")
        if isinstance(jobs, dict):
            return any(
                isinstance(job, dict)
                and isinstance(job.get("uses"), str)
                and job["uses"].startswith(OWNED_USES_PREFIX)
                for job in jobs.values()
            )
    return OWNED_USES_PREFIX in text


def read_workflow(path: Path) -> Workflow:
    """Read one workflow file into a :class:`Workflow`.

    Args:
        path: Path to the workflow file.

    Returns:
        The Workflow. An unreadable or unparseable file still yields a
        record, named after its path, so it appears in the inventory
        rather than vanishing from it.

    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        data = None

    fallback = f"{_WORKFLOW_DIR.as_posix()}/{path.name}"
    return Workflow(
        path=path,
        filename=path.name,
        name=_declared_name(data) or fallback,
        owned=_calls_hyperi_ci(data, text),
    )


def inventory(project_dir: Path | None = None) -> list[Workflow]:
    """List the repo's workflows, sorted by filename.

    Args:
        project_dir: Repo root (default: process cwd).

    Returns:
        Every ``.yml`` / ``.yaml`` under ``.github/workflows``, empty when
        the directory is absent.

    """
    root = (project_dir or Path.cwd()) / _WORKFLOW_DIR
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    return [
        read_workflow(entry)
        for entry in entries
        if entry.is_file() and entry.suffix in _WORKFLOW_SUFFIXES
    ]


def find(workflows: list[Workflow], token: str) -> Workflow | None:
    """Match a workflow by filename or display name, case-insensitively.

    Callers name a workflow either way -- ``upstream-sync.yml`` on the
    command line, ``upstream-sync`` in a run listing -- so both resolve,
    with the bare stem accepted too.

    Args:
        workflows: The inventory to search.
        token: Filename, filename stem, or display name.

    Returns:
        The single match, or None when nothing or several match.

    """
    wanted = token.strip().lower()
    if not wanted:
        return None
    for attr in ("filename", "name"):
        hits = [wf for wf in workflows if getattr(wf, attr).lower() == wanted]
        if len(hits) == 1:
            return hits[0]
    stems = [wf for wf in workflows if wf.path.stem.lower() == wanted]
    if len(stems) == 1:
        return stems[0]
    return None


def owned_names(workflows: list[Workflow]) -> list[str]:
    """Display names of the workflows hyperi-ci scaffolded."""
    return [wf.name for wf in workflows if wf.owned]
