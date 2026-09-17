# Project:   HyperI CI
# File:      src/hyperi_ci/release_mode.py
# Purpose:   Release-mode resolution — SSOT for the push/validate decision
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Release-mode resolution -- the single source of truth.

One tri-state mode, resolved here so the container, helm and argocd stages
cannot drift apart on the push decision:

* ``release``  -- GA release run (``will-release`` true / dispatch). Full
  tag set, pushed to every configured registry.
* ``dev``      -- branch dev-image push: a DIFFERENT artifact class from a
  GA release. Mutable ``branch-<slug>`` + immutable ``sha-<short>`` tags,
  GHCR ONLY, behind the ``release.container.dev_push`` opt-in. Never
  version tags, never ``latest``, never other registries -- main stays the
  sole GA release path.
* ``validate`` -- build and discard (the push-to-main / local default).

Helm and ArgoCD stages consume only the bool view (:func:`is_release_mode`)
-- a dev-mode run behaves as validate for them; dev artifacts are container
images only (plan decision 3).

The mode is resolved from ``HYPERCI_RELEASE_MODE`` (set by the workflows
from the plan job's ``will-release`` output) plus the standard GitHub
Actions event context. ``HYPERCI_PUBLISH_MODE`` is still read when the
canonical variable is unset, because workflows and the CLI are versioned
independently: a consumer pinned at ``@main`` runs a workflow that sets the
old name against whatever CLI version PyPI last published.

Local invocations resolve to ``validate`` unless the mode is set to ``dev``
explicitly -- offline behaviour follows the same rules as CI, just without
the CI context.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

RELEASE = "release"
DEV = "dev"
VALIDATE = "validate"

# The mode token's previous spelling, still exported so an out-of-tree
# comparison against it keeps matching.
PUBLISH = RELEASE

_MODE_ENV = "HYPERCI_RELEASE_MODE"
_LEGACY_MODE_ENV = "HYPERCI_PUBLISH_MODE"

# Docker tag grammar: [A-Za-z0-9_][A-Za-z0-9._-]{0,127}. Anything else in a
# branch name collapses to '-'; leading '.'/'-' are invalid and stripped.
_TAG_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")
_SLUG_MAX = 100  # leaves room for the "branch-" prefix within 128


def _mode_flag(env: Mapping[str, str]) -> str:
    """Read the mode flag, canonical variable first.

    An empty canonical value falls through to the legacy one rather than
    winning, so a workflow that sets only the old name still decides.
    """
    flag = env.get(_MODE_ENV, "").strip().lower()
    if flag:
        return flag
    return env.get(_LEGACY_MODE_ENV, "").strip().lower()


def resolve_push_mode(
    *, dev_push: bool = False, env: Mapping[str, str] | None = None
) -> str:
    """Resolve the push mode: ``release`` | ``dev`` | ``validate``.

    Args:
        dev_push: The project's ``release.container.dev_push`` opt-in.
        env: Environment mapping (defaults to ``os.environ``; injectable
            for tests).

    The mode flag wins: ``true``-ish -> release, ``dev`` -> forced dev
    (local/rehearsal use), ``false``-ish -> not a release (dev when opted
    in on a branch/PR CI run, else validate). With no flag at all (older
    workflows or local runs), ``workflow_dispatch`` implies release -- the
    legacy fallback -- and anything else resolves like ``false``.

    """
    e = os.environ if env is None else env
    flag = _mode_flag(e)
    if flag in ("true", "1", "yes"):
        return RELEASE
    if flag == "dev":
        return DEV
    if flag not in ("false", "0", "no"):
        # No/unknown flag — legacy event-based fallback (older workflows,
        # local invocations): workflow_dispatch == release.
        if e.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
            return RELEASE
    if dev_push and is_branch_ci_context(env=e):
        return DEV
    return VALIDATE


def is_branch_ci_context(*, env: Mapping[str, str] | None = None) -> bool:
    """Report whether this is a CI run for a branch.

    True for a pull_request event, or a push to any ref other than main.
    Local (non-Actions) runs are never a branch CI context -- a dev push
    from a laptop must set the mode to ``dev`` explicitly.
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


def is_release_mode(*, env: Mapping[str, str] | None = None) -> bool:
    """Bool view for stages with no dev mode (helm, argocd): release or not."""
    return resolve_push_mode(env=env) == RELEASE


def is_publish_mode(*, env: Mapping[str, str] | None = None) -> bool:
    """Deprecated spelling of :func:`is_release_mode`."""
    return is_release_mode(env=env)


def dev_branch_slug(*, env: Mapping[str, str] | None = None) -> str:
    """Docker-tag-safe slug of the branch under CI.

    ``GITHUB_HEAD_REF`` (the PR source branch) wins over ``GITHUB_REF_NAME``
    (which is ``<n>/merge`` on pull_request events). Empty when neither is
    set -- callers then fall back to the sha tag alone.
    """
    e = os.environ if env is None else env
    ref = e.get("GITHUB_HEAD_REF") or e.get("GITHUB_REF_NAME", "")
    slug = _TAG_UNSAFE.sub("-", ref).strip("-.").lower()
    return slug[:_SLUG_MAX]
