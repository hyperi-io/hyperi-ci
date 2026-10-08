# Project:   HyperI CI
# File:      src/hyperi_ci/release_mode.py
# Purpose:   Release-mode resolution -- SSOT for the push/validate decision
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Release-mode resolution, the single source of truth for the push decision.

* ``release``  -- GA release run (``will-release`` true / dispatch). Full
  tag set, pushed to every configured registry.
* ``dev``      -- branch dev-image push. Mutable ``branch-<slug>`` + immutable
  ``sha-<short>`` tags to GHCR ONLY, behind the ``release.container.dev_push``
  opt-in. Never version tags, ``latest`` or other registries.
* ``validate`` -- build and discard (the push-to-main / local default).

:func:`is_release_mode` reads a dev-mode run as not a release.

The mode comes from ``HYPERCI_RELEASE_MODE`` (set by the workflows from the plan
job's ``will-release`` output) plus the GitHub Actions event context.
``HYPERCI_PUBLISH_MODE`` is read when the canonical variable is unset, because
workflows and the CLI are versioned independently and a consumer pinned at
``@main`` may pair the old name with a newer CLI.

Local invocations resolve to ``validate`` unless the mode is set to ``dev``.
"""

import os
import re
from collections.abc import Mapping

RELEASE = "release"
DEV = "dev"
VALIDATE = "validate"

# Deprecated spelling of RELEASE, kept for out-of-tree comparisons.
PUBLISH = RELEASE

_MODE_ENV = "HYPERCI_RELEASE_MODE"
_LEGACY_MODE_ENV = "HYPERCI_PUBLISH_MODE"

# Docker tag grammar: [A-Za-z0-9_][A-Za-z0-9._-]{0,127}. Anything else in a
# branch name collapses to '-'; leading '.'/'-' are invalid and stripped.
_TAG_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")
_SLUG_MAX = 100  # leaves room for the "branch-" prefix within 128


def _mode_flag(env: Mapping[str, str]) -> str:
    """Read the mode flag, canonical variable first.

    An empty canonical value falls through to the legacy one.
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

    The mode flag wins: ``true``-ish -> release, ``dev`` -> dev, ``false``-ish
    -> dev when opted in on a branch/PR CI run, else validate. With no flag
    (older workflows, local runs) ``workflow_dispatch`` implies release and
    anything else resolves like ``false``.

    """
    e = os.environ if env is None else env
    flag = _mode_flag(e)
    if flag in ("true", "1", "yes"):
        return RELEASE
    if flag == "dev":
        return DEV
    if flag not in ("false", "0", "no"):
        # No or unknown flag: workflow_dispatch means release.
        if e.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
            return RELEASE
    if dev_push and is_branch_ci_context(env=e):
        return DEV
    return VALIDATE


def is_branch_ci_context(*, env: Mapping[str, str] | None = None) -> bool:
    """Report whether this is a CI run for a branch.

    True for a pull_request event, or a push to any ref other than main.
    Local (non-Actions) runs never are.
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
    """Bool view for a caller with no dev mode: release or not."""
    return resolve_push_mode(env=env) == RELEASE


def is_publish_mode(*, env: Mapping[str, str] | None = None) -> bool:
    """Return :func:`is_release_mode` under its deprecated spelling."""
    return is_release_mode(env=env)


def dev_branch_slug(*, env: Mapping[str, str] | None = None) -> str:
    """Docker-tag-safe slug of the branch under CI.

    ``GITHUB_HEAD_REF`` (the PR source branch) wins over ``GITHUB_REF_NAME``
    (``<n>/merge`` on pull_request events). Empty when neither is set.
    """
    e = os.environ if env is None else env
    ref = e.get("GITHUB_HEAD_REF") or e.get("GITHUB_REF_NAME", "")
    slug = _TAG_UNSAFE.sub("-", ref).strip("-.").lower()
    return slug[:_SLUG_MAX]
