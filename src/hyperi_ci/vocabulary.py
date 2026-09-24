# Project:   HyperI CI
# File:      src/hyperi_ci/vocabulary.py
# Purpose:   One word for the release event, and the spellings that still work
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""One word for the event that ships an artefact: ``release``.

hyperi-ci spelled the same event two ways. Tag-on-publish had already collapsed
them by policy -- a tag exists iff the artefact is in the registry -- so the two
names described one thing, and readers conflated them because they ARE one
thing.

``release`` wins because semantic-release is the tagger and its whole vocabulary
is release: release rules, prerelease branches, the plugin namespace. ``publish``
was imported alongside it.

The apparent counter-example does not hold. ``channel: alpha`` looks like a
release that publishes nothing, but it publishes to a GitHub Release and an R2
channel path. That is a narrower DESTINATION SET, not the absence of publishing.
No version exists here without something being published.

Every old spelling keeps working and names its replacement. Nothing a consumer
has already written stops building.

Two things deliberately keep their old names, because a warning cannot reach
them: a reusable workflow's INPUT is validated by GitHub before any of our code
runs, and passing one the callee does not declare is a hard error rather than a
warning. ``publish-target`` and ``will-publish`` therefore stay declared.
"""

from __future__ import annotations

import os
from typing import Any

# The git trailer that marks a commit as one to release.
TRAILER_KEY = "Release"
LEGACY_TRAILER_KEY = "Publish"
TRAILER_VALUE = "true"

# Config namespaces: `publish:` folds into `release:` at load time.
CONFIG_NAMESPACE = "release"
LEGACY_CONFIG_NAMESPACE = "publish"

# `hyperi-ci release` is canonical again. It was marked for removal in v4 while
# `publish` was the live verb; that deprecation was the wrong way round and is
# withdrawn here. Said out loud wherever the deprecation is printed, because a
# reversal with no explanation reads as indecision.
REVERSAL_NOTE = (
    "`hyperi-ci release` was previously marked deprecated for removal. "
    "That was backwards and is withdrawn: `release` is the canonical verb."
)


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge ``override`` onto ``base``; ``override`` wins a conflict."""
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge(current, value)
        else:
            merged[key] = value
    return merged


def fold_legacy_config(doc: Any) -> tuple[Any, list[str]]:
    """Fold a ``publish:`` block into ``release:``, and name what was folded.

    Done at LOAD time rather than resolved at read time, because the two
    namespaces cannot coexist in the merged config: a shipped ``release.x``
    default would outrank a project's ``publish.x`` and silently never apply.

    ``release:`` wins any key set both ways -- a project mid-migration has
    already said which spelling it means.

    A removal-tier key written under ``release:`` is named too, because the
    rename alone would never report it.

    Returns:
        ``(document, deprecated_keys)``. The document is unchanged when there
        is no legacy block, and the list empty when nothing is deprecated.

    """
    if not isinstance(doc, dict):
        return doc, []
    canonical = doc.get(CONFIG_NAMESPACE)
    canonical = canonical if isinstance(canonical, dict) else {}
    removing = sorted(
        f"{CONFIG_NAMESPACE}.{key}" for key in canonical if key in _REMOVAL_TIER
    )
    legacy = doc.get(LEGACY_CONFIG_NAMESPACE)
    if not isinstance(legacy, dict):
        return doc, removing

    folded = dict(doc)
    folded[CONFIG_NAMESPACE] = _merge(legacy, canonical)
    folded.pop(LEGACY_CONFIG_NAMESPACE, None)
    renamed = sorted(f"{LEGACY_CONFIG_NAMESPACE}.{key}" for key in legacy)
    return folded, renamed + removing


def canonical_key(key: str) -> str:
    """Rewrite a dotted ``publish.*`` key to its ``release.*`` equivalent."""
    prefix = f"{LEGACY_CONFIG_NAMESPACE}."
    if key == LEGACY_CONFIG_NAMESPACE:
        return CONFIG_NAMESPACE
    if key.startswith(prefix):
        return f"{CONFIG_NAMESPACE}.{key[len(prefix) :]}"
    return key


def legacy_key(key: str) -> str:
    """Rewrite a dotted ``release.*`` key back to its ``publish.*`` spelling."""
    prefix = f"{CONFIG_NAMESPACE}."
    if key == CONFIG_NAMESPACE:
        return LEGACY_CONFIG_NAMESPACE
    if key.startswith(prefix):
        return f"{LEGACY_CONFIG_NAMESPACE}.{key[len(prefix) :]}"
    return key


def key_candidates(key: str) -> list[str]:
    """Return the spellings to try for ``key``, the one asked for first.

    Both directions, because a config reaches a reader two ways: folded by
    :func:`fold_legacy_config` into the canonical namespace, or handed over as a
    raw mapping that was never folded. Trying the literal key first means a
    caller holding an unfolded dict still gets its own value rather than a
    default.
    """
    seen: list[str] = []
    for candidate in (key, canonical_key(key), legacy_key(key)):
        if candidate not in seen:
            seen.append(candidate)
    return seen


def trailer_values(message: str, key: str) -> list[str]:
    """Return every value ``key`` carries in ``message``, in order.

    A list rather than one value: a trailer repeated with different values has
    no single answer, so the caller decides whether any occurrence counts.
    Key matching is case-insensitive.
    """
    wanted = key.strip().lower()
    values: list[str] = []
    for line in message.splitlines():
        stripped = line.strip()
        if not stripped or ":" not in stripped:
            continue
        name, _, value = stripped.partition(":")
        if name.strip().lower() == wanted:
            values.append(value.strip())
    return values


def has_release_trailer(message: str) -> bool:
    """Report whether a commit message carries the release trailer.

    Both spellings count. The trailer is also matched in shell by the
    predict-version composite, which runs where hyperi-ci is not installed --
    the two must accept the same set.
    """
    return any(
        value.lower() == TRAILER_VALUE
        for key in (TRAILER_KEY, LEGACY_TRAILER_KEY)
        for value in trailer_values(message, key)
    )


# These keys are removed rather than carried indefinitely: they name a
# destination choice the system no longer offers (issue #151).
REMOVAL_DATE = "December 2026"

# Keys scheduled for REMOVAL rather than carried indefinitely, spelled without
# a namespace so either namespace matches.
_REMOVAL_TIER: frozenset[str] = frozenset({"target", "destinations_oss"})

# A removal-tier key that still takes effect, and where its entries go. Deleting
# one without moving it turns every destination it opted out back on.
_MOVED_TO: dict[str, str] = {"destinations_oss": "release.destinations"}


def _is_removal_tier(key: str) -> bool:
    return key.rsplit(".", 1)[-1] in _REMOVAL_TIER


def deprecated_config_message(keys: list[str]) -> str:
    """Build the notice naming each legacy key, and what happens to it.

    Two tiers: a renamed key keeps working indefinitely, a legacy destination
    key is removed on a date (issue #151). Of those, an inert key is deleted,
    and one that still takes effect is moved.
    """
    renamed = [key for key in keys if not _is_removal_tier(key)]
    removing = [key for key in keys if _is_removal_tier(key)]
    moving = [key for key in removing if key.rsplit(".", 1)[-1] in _MOVED_TO]
    inert = [key for key in removing if key not in moving]

    parts: list[str] = []
    if renamed:
        pairs = ", ".join(f"{key} -> {canonical_key(key)}" for key in renamed)
        parts.append(
            f"Renamed config keys in .hyperi-ci.yaml: {pairs}. "
            "The old spelling keeps working; rename when convenient."
        )
    if moving:
        pairs = ", ".join(
            f"{key} -> {_MOVED_TO[key.rsplit('.', 1)[-1]]}" for key in moving
        )
        parts.append(
            f"Legacy destination keys in .hyperi-ci.yaml: {pairs}. "
            f"They still take effect and are REMOVED in {REMOVAL_DATE}. Move "
            "their entries across -- deleting them instead turns every "
            "destination they opt out of back on."
        )
    if inert:
        names = ", ".join(inert)
        parts.append(
            f"Legacy destination keys in .hyperi-ci.yaml: {names}. "
            f"These are inert now and are REMOVED in {REMOVAL_DATE} -- "
            "every artefact goes to an OSS destination. Delete them."
        )
    return " ".join(parts)


def report_deprecated_config(keys: list[str]) -> None:
    """Warn about legacy config keys, loudly enough to be read.

    A deprecation nobody sees teaches nobody, so this is a GitHub annotation in
    CI and a warn line locally -- ``hyperi-ci check`` prints the same list
    before a push.
    """
    if not keys:
        return
    from hyperi_ci.common import warn

    message = deprecated_config_message(keys)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning::{message}")
    warn(message)
