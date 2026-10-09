# Project:   HyperI CI
# File:      src/hyperi_ci/vocabulary.py
# Purpose:   One word for the release event, and the spellings that still work
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""One word for the event that ships an artefact: ``release``.

``release`` and ``publish`` named one event, since a tag exists iff the
artefact is in the registry. ``release`` wins because semantic-release is the
tagger and its vocabulary is release (release rules, prerelease branches, the
plugin namespace). ``channel: alpha`` still publishes, to a GitHub Release and
an R2 channel path, so it is a narrower DESTINATION SET and not a release that
publishes nothing.

Every old spelling keeps working and names its replacement.

``publish-target`` and ``will-publish`` keep their old names because a warning
cannot reach them: GitHub validates a reusable workflow's INPUT before our code
runs, and passing one the callee does not declare is a hard error.
"""

import os
from typing import Any

# The git trailer that marks a commit as one to release.
TRAILER_KEY = "Release"
LEGACY_TRAILER_KEY = "Publish"
TRAILER_VALUE = "true"

# Config namespaces: `publish:` folds into `release:` at load time.
CONFIG_NAMESPACE = "release"
LEGACY_CONFIG_NAMESPACE = "publish"

# Printed wherever the deprecation is printed, so the reversal is explained.
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

    Done at LOAD time because a shipped ``release.x`` default would outrank a
    project's ``publish.x`` in the merged config. ``release:`` wins any key set
    both ways. A removal-tier key written under ``release:`` is named too, as
    the rename alone would never report it.

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

    Both directions, because a config arrives folded by
    :func:`fold_legacy_config` or as a raw mapping. Trying the literal key first
    means a caller holding an unfolded dict gets its own value.
    """
    seen: list[str] = []
    for candidate in (key, canonical_key(key), legacy_key(key)):
        if candidate not in seen:
            seen.append(candidate)
    return seen


def trailer_values(message: str, key: str) -> list[str]:
    """Return every value ``key`` carries in ``message``, in order.

    A list, because a trailer repeated with different values has no single
    answer. Key matching is case-insensitive.
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

    Both spellings count, and must match the predict-version composite's shell
    check, which runs where hyperi-ci is not installed.
    """
    return any(
        value.lower() == TRAILER_VALUE
        for key in (TRAILER_KEY, LEGACY_TRAILER_KEY)
        for value in trailer_values(message, key)
    )


REMOVAL_DATE = "December 2026"

# Keys naming a destination choice the system no longer offers (issue #151),
# spelled without a namespace so either matches.
_REMOVAL_TIER: frozenset[str] = frozenset({"target", "destinations_oss"})

# A removal-tier key that still takes effect, and where its entries go. Deleting
# one without moving it turns every destination it opted out back on.
_MOVED_TO: dict[str, str] = {"destinations_oss": "release.destinations"}


def _is_removal_tier(key: str) -> bool:
    return key.rsplit(".", 1)[-1] in _REMOVAL_TIER


def deprecated_config_message(keys: list[str]) -> str:
    """Build the notice naming each legacy key, and what happens to it.

    A renamed key keeps working. A legacy destination key is removed on a date
    (issue #151): an inert one is deleted and one that still takes effect is
    moved.
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

    A GitHub annotation in CI and a warn line locally. ``hyperi-ci check``
    prints the same list before a push.
    """
    if not keys:
        return
    from hyperi_ci.common import warn

    message = deprecated_config_message(keys)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning::{message}")
    warn(message)


# Keys nothing reads, each naming its subtree: setting one warns, never fails.
REMOVED_KEYS: tuple[str, ...] = (
    "build.type",
    "build.typescript.package_manager",
    "ci_min_python_version",
    "container",
    "deployment",
    "golang",
    "quality.python.bandit",
    "quality.python.bandit_exclude_tests",
    "quality.python.pyright",
    "release.argocd",
    "release.binaries",
    "release.container.binary_name",
    "release.container.cmd",
    "release.container.entrypoint",
    "release.container.health_path",
    "release.container.mode",
    "release.container.node_version",
    "release.container.overlays",
    "release.container.port",
    "release.container.python_version",
    "release.container.registry",
    "release.destinations.helm",
    "release.destinations_oss.helm",
    "runners",
    "typescript.package_manager",
    "workspace",
)

_reported_removed_keys: set[str] = set()


def _is_removed_path(dotted: str) -> bool:
    """Report whether ``dotted`` is a removed key or under one, either spelling."""
    for key in REMOVED_KEYS:
        for spelled in key_candidates(key):
            if dotted == spelled or dotted.startswith(f"{spelled}."):
                return True
    return False


def _fully_removed(node: Any, dotted: str) -> bool:
    if _is_removed_path(dotted):
        return True
    if not isinstance(node, dict) or not node:
        return False
    return all(
        _fully_removed(child, f"{dotted}.{name}") for name, child in node.items()
    )


def _node_at(doc: dict[str, Any], dotted: str) -> Any:
    node: Any = doc
    for part in dotted.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return node


def drop_removed_notices(keys: list[str], doc: Any) -> list[str]:
    """Drop the rename or move notice for a legacy key whose every setting is removed.

    Otherwise ``publish.binaries`` is told to rename and then to delete. ``doc``
    is the project config as written, before the ``publish:`` fold.
    """
    if not isinstance(doc, dict):
        return keys
    kept: list[str] = []
    for key in keys:
        spellings = [s for s in key_candidates(key) if _has_path(doc, s)]
        if spellings and all(_fully_removed(_node_at(doc, s), s) for s in spellings):
            continue
        kept.append(key)
    return kept


def _has_path(doc: dict[str, Any], dotted: str) -> bool:
    node: Any = doc
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def find_removed_keys(doc: Any) -> list[str]:
    """Return each removed key a project config sets, spelled as it is written.

    Checked before the ``publish:`` fold, so ``publish.binaries`` is named as
    written. One entry per removed key, however many of its children are set.
    """
    if not isinstance(doc, dict):
        return []
    found: list[str] = []
    for key in REMOVED_KEYS:
        spelled = next((c for c in key_candidates(key) if _has_path(doc, c)), None)
        if spelled is not None:
            found.append(spelled)
    return found


def removed_key_message(key: str) -> str:
    """Build the one-line notice for a removed key."""
    return f"{key} is no longer read by hyperi-ci and can be deleted"


# A removed security gate set to `blocking` moves that mode to the gate that
# replaced it, so dropping the tool never relaxes a repo's security gate.
_CARRIED_TO: dict[str, str] = {"quality.python.bandit": "quality.python.ruff_security"}

_reported_carry_overs: set[str] = set()


def _configured_mode(raw: Any) -> str:
    """Return the mode a bare string or a ``mode`` mapping sets, lowercased."""
    value = raw.get("mode") if isinstance(raw, dict) else raw
    return str(value or "").strip().lower()


def carry_over_removed_gates(doc: Any) -> tuple[Any, list[str]]:
    """Move a removed security gate's ``blocking`` onto the key that replaced it.

    Only where the project sets the removed key to ``blocking`` and leaves the
    successor unset, so an explicit successor setting always wins.

    Args:
        doc: The project config as written.

    Returns:
        ``(document, carried_keys)``. The document is a copy with each successor
        set to ``blocking``, or unchanged when nothing is carried.

    """
    if not isinstance(doc, dict):
        return doc, []
    carried: list[str] = []
    result = doc
    for removed, successor in _CARRIED_TO.items():
        *parents, leaf = removed.split(".")
        section = _node_at(result, ".".join(parents))
        successor_leaf = successor.rsplit(".", 1)[-1]
        if not isinstance(section, dict) or successor_leaf in section:
            continue
        if _configured_mode(section.get(leaf)) != "blocking":
            continue
        result = _with_value(result, successor, "blocking")
        carried.append(removed)
    return result, carried


def _with_value(doc: dict[str, Any], dotted: str, value: Any) -> dict[str, Any]:
    """Return a copy of ``doc`` with ``dotted`` set, leaving ``doc`` untouched."""
    head, _, rest = dotted.partition(".")
    if not rest:
        return {**doc, head: value}
    child = doc.get(head)
    return {
        **doc,
        head: _with_value(child if isinstance(child, dict) else {}, rest, value),
    }


def carry_over_message(key: str) -> str:
    """Build the one-line notice for a carried-over ``blocking`` mode."""
    successor = _CARRIED_TO[key].rsplit(".", 1)[-1]
    return f"{key}: blocking is carried over to {successor}: blocking"


def report_carry_overs(keys: list[str]) -> None:
    """Announce each carried-over gate once per process, as a removed key is."""
    fresh = [key for key in keys if key not in _reported_carry_overs]
    if not fresh:
        return
    from hyperi_ci.common import announce

    for key in fresh:
        _reported_carry_overs.add(key)
        announce(
            carry_over_message(key),
            "Removed config key in .hyperi-ci.yaml",
            level="warning",
        )


def report_removed_keys(keys: list[str]) -> None:
    """Warn once per removed key per process, as an annotation under GitHub Actions.

    A stage reloads the config several times.
    """
    fresh = [key for key in keys if key not in _reported_removed_keys]
    if not fresh:
        return
    from hyperi_ci.common import announce

    for key in fresh:
        _reported_removed_keys.add(key)
        announce(
            removed_key_message(key),
            "Removed config key in .hyperi-ci.yaml",
            level="warning",
        )
