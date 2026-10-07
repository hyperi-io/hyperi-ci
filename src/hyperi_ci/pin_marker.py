# Project:   HyperI CI
# File:      src/hyperi_ci/pin_marker.py
# Purpose:   The `# hyperi-ci:pin <key>` convention, in one place
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""The `# hyperi-ci:pin <key>` marker convention.

A version MIRRORED into source (a composite action's `default:`, a workflow
input's) is invisible to dependency managers, Renovate included. The marker
makes it visible, and works the same in Python and YAML::

    # hyperi-ci:pin tools.osv-scanner
    default: v2.4.0

    # hyperi-ci:pin runtimes.python
    default: "3.14"

Two callers share the pattern:

- ``scripts/update-versions.py`` ENFORCES a marked pin against the versions SSOT
  for this repo, and rewrites it on drift.
- ``hyperi_ci.deps`` DISCOVERS marked pins in any repo and reports what the
  marker declares.
"""

import re

# `{name}` is an escaped key for the enforcing caller, a capture group for the
# discovering one.
MARKER = r"#\s*hyperi-ci:pin\s+{name}\s*\n"

# A sha256 pins the BYTES. Tried before the version alternative below, which
# would otherwise consume a digest that begins with a digit.
_DIGEST = r"[0-9a-f]{64}"

# Version token on the line after the marker. The required `=` or `:` stops a
# digit inside an identifier (`_SHA256 = ...`) reading as the version.
_SEMVER = r"v?\d[\w.+-]*"

_VERSION = rf"(?P<ver>{_SEMVER})"
_TOKEN = rf"(?P<ver>{_DIGEST}|{_SEMVER})"
_VALUE = rf'[^\n]*?[=:]\s*"?{_TOKEN}'


def pin_pattern(name: str) -> re.Pattern[str]:
    """Match ONE named pin, splitting the prefix from the version token.

    Group 1 is everything up to the version, so ``re.sub`` can swap the version
    and keep the prefix (``--apply``). Group 2 (``ver``) is the
    version alone, so a report can point at the pin's line, not the marker's.
    """
    marker = MARKER.format(name=re.escape(name))
    return re.compile(rf'({marker}[^\n]*?[=:]\s*"?){_VERSION}')


def digest_pin_pattern(name: str) -> re.Pattern[str]:
    """Match ONE named DIGEST pin, splitting the prefix from the hash.

    Separate from :func:`pin_pattern` so a digest is never rewritten with a
    version or the reverse, which would install unverified.
    """
    marker = MARKER.format(name=re.escape(name))
    return re.compile(rf'({marker}[^\n]*?[=:]\s*"?)(?P<ver>{_DIGEST})')


# A dotted identifier (`tools.cargo-audit`), bounded so a marker quoted inside a
# string literal does not read as a real pin.
_KEY = r"(?P<dep>[\w.\-/]+)"


def discovery_pattern() -> re.Pattern[str]:
    """Match ANY marked pin, capturing the key as ``dep`` and the version.

    The reference definition of the regex that the ``pin-marker`` surface in
    ``config/dep-surfaces.yaml`` declares as data, which a test compares.
    """
    return re.compile(MARKER.format(name=_KEY) + _VALUE)
