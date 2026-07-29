# Project:   HyperI CI
# File:      src/hyperi_ci/pin_marker.py
# Purpose:   The `# hyperi-ci:pin <key>` convention, in one place
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""The `# hyperi-ci:pin <key>` marker convention.

A version that has to be MIRRORED into source -- a Python constant, a
composite action's `default:` -- is invisible to every dependency manager,
Renovate included. The marker is our answer::

    # hyperi-ci:pin tools.gitleaks
    _GITLEAKS_VERSION = "v8.30.1"

    # hyperi-ci:pin tools.osv-scanner
    default: v2.4.0

One explicit marker beats a per-tool regex guessing at each file's shape: it
reads the same in Python and YAML, survives the line being reworded, and lets
several tools share one file without a `default:` pattern rewriting all of them
to the same version.

Two callers, one definition:

- ``scripts/update-versions.py`` ENFORCES a marked pin against
  ``config/versions.yaml`` for this repo, and rewrites it on drift.
- ``hyperi_ci.deps`` DISCOVERS marked pins in any repo, with no SSOT to compare
  against -- it only reports what the marker declares.

They read the same lines, so the pattern lives here rather than being written
twice and drifting.
"""

from __future__ import annotations

import re

# The marker line itself. `{name}` is substituted with an escaped key for the
# enforcing caller, or a capture group for the discovering one.
MARKER = r"#\s*hyperi-ci:pin\s+{name}\s*\n"

# Version token on the line FOLLOWING the marker. The `=` or `:` (with an
# optional opening quote) is required, so a digit inside an identifier --
# `_SHA256 = ...` -- cannot be mistaken for the version.
_VERSION = r"(?P<ver>v?\d[\w.+-]*)"
_VALUE = rf'[^\n]*?[=:]\s*"?{_VERSION}'


def pin_pattern(name: str) -> re.Pattern[str]:
    """Match ONE named pin, splitting the prefix from the version token.

    Group 1 is everything up to the version, so ``re.sub`` can swap the version
    while keeping the prefix -- that is what ``--apply`` / ``--fix`` rely on.
    Group 2 (also named ``ver``) is the version on its own, so a report can
    point its line number at the pin rather than at the marker above it.
    """
    marker = MARKER.format(name=re.escape(name))
    return re.compile(rf'({marker}[^\n]*?[=:]\s*"?){_VERSION}')


# A marker KEY is a dotted identifier (`tools.cargo-audit`), never arbitrary
# non-space. Bounding it keeps a marker quoted inside a string literal -- a
# test fixture describing the convention -- from reading as a real pin.
_KEY = r"(?P<dep>[\w.\-/]+)"


def discovery_pattern() -> re.Pattern[str]:
    """Match ANY marked pin, capturing the key as ``dep`` and the version.

    The discovery half: no SSOT, no name known up front. Used by the
    ``pin-marker`` surface in ``config/dep-surfaces.yaml``, which declares this
    same regex as data -- this function is the reference definition and what
    the consistency test compares against.
    """
    return re.compile(MARKER.format(name=_KEY) + _VALUE)
