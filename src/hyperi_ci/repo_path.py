# Project:   HyperI CI
# File:      src/hyperi_ci/repo_path.py
# Purpose:   Keep a path a repo's config names inside the project root
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Resolve a repo-supplied path and refuse one that leaves the project root.

A path in ``.hyperi-ci.yaml`` or ``pyproject.toml`` is whatever the repo says,
and the Container job holds ``~/.docker/config.json`` after its logins. Callers:
``release.container.dockerfile`` and ``.context``, ``[tool.hatch.version]
path`` and the ``VERSION`` file. Other repo-named paths are not routed here.
"""

from pathlib import Path


class RepoPathError(ValueError):
    """A repo-supplied path resolves outside the root it must stay in."""


def confine(path: str | Path, root: Path, *, key: str) -> Path:
    """Resolve ``path`` against ``root`` and refuse it when it lands outside.

    An absolute path is held to the same rule, and symlinks are resolved so a
    link pointing out of the checkout is refused.

    Args:
        path: The configured path, relative to ``root`` or absolute.
        root: The directory the path must stay inside.
        key: Where the path came from, for the error message.

    Returns:
        The resolved absolute path.

    Raises:
        RepoPathError: The resolved path is not ``root`` or inside it.

    """
    base = root.resolve()
    resolved = (base / path).resolve()
    if not resolved.is_relative_to(base):
        msg = f"{key}: {path} resolves outside {base}"
        raise RepoPathError(msg)
    return resolved
