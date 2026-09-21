# Project:   HyperI CI
# File:      src/hyperi_ci/classification.py
# Purpose:   Repo-classification vocabulary, marker readers and resolution
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Repo classification: the declared category a repo belongs to.

Four canonical categories -- ``internal``, ``product``, ``fork`` and
``general-oss`` -- decide visibility, licence, publish policy and
branding. The category is DECLARED, never inferred from the repo name.

Resolution order (first hit wins):

1. ``classification`` in the merged config (``.hyperi-ci.yaml``, or the
   ``HYPERCI_CLASSIFICATION`` override that lands on the same key).
2. The ``.hyperi-classification`` one-token dotfile at the repo root.
3. The GitHub org custom property, which needs org API access and is
   therefore opt-in rather than part of the offline config load.

Nothing declared on any rung resolves to no declaration at all, and the
effective reading falls back to ``internal`` -- the most restrictive
category. A repo that never says what it is is never read as public OSS.
"""

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

# The only values the GitHub org custom property accepts, and the only
# values written into a marker.
CANONICAL: tuple[str, ...] = ("internal", "product", "fork", "general-oss")

# Accepted input spellings that normalise to a canonical value. The
# category numbers come from the matrix in the repo-classification
# standard; `hyperi` and `oss` are the pre-rename spellings.
ALIASES: dict[str, str] = {
    "hyperi": "internal",
    "oss": "general-oss",
    "general_oss": "general-oss",
    "generaloss": "general-oss",
    "1": "internal",
    "2": "product",
    "3": "fork",
    "4": "general-oss",
}

# What an undeclared repo is read as: private, never published.
MOST_RESTRICTIVE: str = "internal"

# Marker used by repos with no `.hyperi-ci.yaml` (forks, some OSS).
DOTFILE_NAME: str = ".hyperi-classification"

SOURCE_CONFIG: str = ".hyperi-ci.yaml"
SOURCE_DOTFILE: str = DOTFILE_NAME
SOURCE_ORG: str = "github-org-property"
SOURCE_UNDECLARED: str = "undeclared"


@dataclass(frozen=True, slots=True)
class Resolution:
    """The outcome of resolving a repo's classification.

    Attributes:
        value: The canonical declared category, or "" when undeclared.
        source: Which rung answered -- one of the SOURCE_* constants.
        effective: The category to act on; ``MOST_RESTRICTIVE`` when
            nothing is declared.

    """

    value: str
    source: str
    effective: str


def normalise(value: object) -> str:
    """Normalise a declared category to its canonical spelling.

    Args:
        value: The declared token. A YAML scalar may arrive as an int
            (the category numbers) rather than a string.

    Returns:
        One of CANONICAL.

    Raises:
        ValueError: The token is not a canonical value or a known alias.

    """
    token = str(value).strip().lower().replace(" ", "-")
    if token in CANONICAL:
        return token
    aliased = ALIASES.get(token.replace("-", "_"))
    if aliased is not None:
        return aliased
    raise ValueError(
        f"Unknown classification '{value}' - expected one of "
        f"{', '.join(CANONICAL)} (aliases: {', '.join(sorted(ALIASES))})."
    )


def read_dotfile(project_dir: Path) -> str | None:
    """Read the `.hyperi-classification` dotfile, if present.

    Blank lines and `#` comments are skipped so a fork can explain the
    marker in the file it has to carry.

    Args:
        project_dir: Repo root to look in.

    Returns:
        The canonical category, or None when the file is absent or holds
        no token.

    Raises:
        ValueError: The file holds a token that is not a known category.

    """
    marker = project_dir / DOTFILE_NAME
    if not marker.is_file():
        return None
    for line in marker.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return normalise(stripped)
    return None


def parse_org_properties(payload: str) -> str | None:
    """Pull the classification out of a `gh api .../properties/values` body.

    Args:
        payload: The raw JSON array returned by the properties endpoint.

    Returns:
        The canonical category, or None when the org sets no
        `classification` property on the repo.

    Raises:
        ValueError: The payload is not the expected JSON array, or the
            property holds a value outside the canonical set.

    """
    try:
        entries = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Unreadable org-property payload: {exc}") from exc
    if not isinstance(entries, list):
        raise ValueError("Org-property payload is not a JSON array.")
    for entry in entries:
        if isinstance(entry, dict) and entry.get("property_name") == "classification":
            value = entry.get("value")
            if value in (None, ""):
                return None
            return normalise(value)
    return None


def from_org_property(repo: str) -> str | None:
    """Read the classification the GitHub org declares for a repo.

    Needs org API access, so a clone outside the org has nothing to read
    and gets None rather than an error.

    Args:
        repo: Fully qualified `owner/name`.

    Returns:
        The canonical category, or None when unavailable or unset.

    """
    from hyperi_ci.common import run_cmd

    try:
        result = run_cmd(
            ["gh", "api", f"repos/{repo}/properties/values"],
            check=True,
            capture=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return None
    try:
        return parse_org_properties(result.stdout or "")
    except ValueError:
        return None


def resolve(raw_config: dict, project_dir: Path) -> Resolution:
    """Resolve a repo's classification from its in-repo markers.

    Covers rungs 1 and 2 only; the org custom property is a network read
    and is left to the caller that opts into it.

    Args:
        raw_config: The merged config dict.
        project_dir: Repo root to look for the dotfile in.

    Returns:
        The Resolution. An undeclared repo yields value "" and effective
        ``internal``.

    Raises:
        ValueError: A marker holds a value outside the canonical set.

    """
    declared = raw_config.get("classification")
    if declared not in (None, ""):
        return _declared(normalise(declared), SOURCE_CONFIG)

    from_file = read_dotfile(project_dir)
    if from_file is not None:
        return _declared(from_file, SOURCE_DOTFILE)

    return Resolution("", SOURCE_UNDECLARED, MOST_RESTRICTIVE)


def _declared(value: str, source: str) -> Resolution:
    """Build a Resolution for a category that was actually declared."""
    return Resolution(value, source, value)
