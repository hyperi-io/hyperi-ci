# Project:   HyperI CI
# File:      src/hyperi_ci/publish_mode.py
# Purpose:   Publish-mode resolution — SSOT for the push/validate decision
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Publish-mode resolution — the single source of truth.

Previously three identical ``_is_publish_mode()`` copies lived in
``container/stage.py``, ``helm/stage.py`` and ``argocd/stage.py``.
Centralised here as part of branch-mode (docs/plans/2026-07-branch-mode,
decision 3 + the abstraction constraint), and extended from a bool to a
tri-state mode:

* ``publish``  — GA publish run (``will-publish`` true / dispatch). Full
  tag set, pushed to every configured registry.
* ``dev``      — branch dev-image push: a DIFFERENT artifact class from a
  GA publish. Mutable ``branch-<slug>`` + immutable ``sha-<short>`` tags,
  GHCR ONLY, behind the ``publish.container.dev_push`` opt-in. Never
  version tags, never ``latest``, never other registries — main stays the
  sole GA publish path.
* ``validate`` — build and discard (the push-to-main / local default).

Helm and ArgoCD stages consume only the bool view (:func:`is_publish_mode`)
— a dev-mode run behaves as validate for them; dev artifacts are container
images only (plan decision 3).

The mode is resolved from ``HYPERCI_PUBLISH_MODE`` (set by the workflows
from the plan job's ``will-publish`` output) plus the standard GitHub
Actions event context. Local invocations resolve to ``validate`` unless
``HYPERCI_PUBLISH_MODE=dev`` is set explicitly — offline behaviour follows
the same rules as CI, just without the CI context.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

PUBLISH = "publish"
DEV = "dev"
VALIDATE = "validate"

# Docker tag grammar: [A-Za-z0-9_][A-Za-z0-9._-]{0,127}. Anything else in a
# branch name collapses to '-'; leading '.'/'-' are invalid and stripped.
_TAG_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")
_SLUG_MAX = 100  # leaves room for the "branch-" prefix within 128


def resolve_push_mode(
    *, dev_push: bool = False, env: Mapping[str, str] | None = None
) -> str:
    """Resolve the push mode: ``publish`` | ``dev`` | ``validate``.

    Args:
        dev_push: The project's ``publish.container.dev_push`` opt-in.
        env: Environment mapping (defaults to ``os.environ``; injectable
            for tests).

    ``HYPERCI_PUBLISH_MODE`` wins: ``true``-ish → publish, ``dev`` →
    forced dev (local/rehearsal use), ``false``-ish → not a publish (dev
    when opted in on a branch/PR CI run, else validate). With no flag at
    all (older workflows or local runs), ``workflow_dispatch`` implies
    publish — the legacy fallback — and anything else resolves like
    ``false``.

    """
    e = os.environ if env is None else env
    flag = e.get("HYPERCI_PUBLISH_MODE", "").strip().lower()
    if flag in ("true", "1", "yes"):
        return PUBLISH
    if flag == "dev":
        return DEV
    if flag not in ("false", "0", "no"):
        # No/unknown flag — legacy event-based fallback (older workflows,
        # local invocations): workflow_dispatch == publish.
        if e.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
            return PUBLISH
    if dev_push and is_branch_ci_context(env=e):
        return DEV
    return VALIDATE


def is_branch_ci_context(*, env: Mapping[str, str] | None = None) -> bool:
    """Report whether this is a CI run for a branch.

    True for a pull_request event, or a push to any ref other than main.
    Local (non-Actions) runs are never a branch CI context — a dev push
    from a laptop must be an explicit ``HYPERCI_PUBLISH_MODE=dev``.
    """
    e = os.environ if env is None else env
    if e.get("GITHUB_ACTIONS") != "true":
        return False
    event = e.get("GITHUB_EVENT_NAME", "")
    if event == "pull_request":
        return True
    return event == "push" and e.get("GITHUB_REF", "") not in (
        "",
        "refs/heads/main",
    )


def is_publish_mode(*, env: Mapping[str, str] | None = None) -> bool:
    """Bool view for stages with no dev mode (helm, argocd): publish or not."""
    return resolve_push_mode(env=env) == PUBLISH


def dev_branch_slug(*, env: Mapping[str, str] | None = None) -> str:
    """Docker-tag-safe slug of the branch under CI.

    ``GITHUB_HEAD_REF`` (the PR source branch) wins over ``GITHUB_REF_NAME``
    (which is ``<n>/merge`` on pull_request events). Empty when neither is
    set — callers then fall back to the sha tag alone.
    """
    e = os.environ if env is None else env
    ref = e.get("GITHUB_HEAD_REF") or e.get("GITHUB_REF_NAME", "")
    slug = _TAG_UNSAFE.sub("-", ref).strip("-.").lower()
    return slug[:_SLUG_MAX]
