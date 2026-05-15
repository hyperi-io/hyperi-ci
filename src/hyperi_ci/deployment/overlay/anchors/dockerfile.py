# Project:   HyperI CI
# File:      src/hyperi_ci/deployment/overlay/anchors/dockerfile.py
# Purpose:   Keyword-relative anchor resolver for Dockerfile overlays
#
# License:   Proprietary - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Dockerfile anchor resolver.

Anchor names map to keyword-relative line positions in the base
Dockerfile. The contract-generated Dockerfile (rustlib's
``generate_dockerfile()`` or pylib's equivalent) emits a predictable
shape with each top-level directive appearing once, which makes
keyword-on-line matching unambiguous and avoids needing rustlib to
emit explicit marker comments.

If a future consumer needs a finer-grained anchor that doesn't map to
a Dockerfile keyword landmark, revisit by either (a) adding a new
keyword anchor here that the consumer's contract-generator already
emits, or (b) introducing rustlib-side marker comments — but only
when at least one consumer actually pulls for it (Rule of Three).

Anchor catalog (order = position-in-file):

    - ``after-base-image``    : after the first ``FROM`` line
    - ``after-base-deps``     : after the LAST ``RUN apt-get`` /
                                ``RUN dnf`` / ``RUN apk`` line
    - ``after-app-binary``    : after a ``COPY <name> ...`` line where
                                ``<name>`` matches the binary name
                                supplied as resolver context
    - ``before-user``         : before the ``USER`` line  ← vector's anchor
    - ``before-healthcheck``  : before the ``HEALTHCHECK`` line
    - ``before-entrypoint``   : before the ``ENTRYPOINT`` or ``CMD`` line
    - ``end-of-image``        : alias of ``before-entrypoint``
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from hyperi_ci.deployment.overlay.errors import AnchorNotFound
from hyperi_ci.deployment.overlay.model import Overlay

# Anchor names that don't need positional context, mapped to (where, regex).
# `where` is one of "before" | "after"; regex matches the landmark line.
_SIMPLE_ANCHORS: dict[str, tuple[str, re.Pattern[str]]] = {
    "after-base-image": ("after", re.compile(r"^\s*FROM\b")),
    "before-user": ("before", re.compile(r"^\s*USER\b")),
    "before-healthcheck": ("before", re.compile(r"^\s*HEALTHCHECK\b")),
    "before-entrypoint": (
        "before",
        re.compile(r"^\s*(ENTRYPOINT|CMD)\b"),
    ),
    "end-of-image": ("before", re.compile(r"^\s*(ENTRYPOINT|CMD)\b")),
}

# Distro-agnostic package-manager regex for `after-base-deps`.
_PKG_MANAGER_RE = re.compile(
    r"^\s*RUN\s+(apt-get|apt|dnf|yum|microdnf|apk|pacman|zypper)\b"
)

# Recognised binary-COPY shape for `after-app-binary`. Matches:
#   COPY <name> /usr/local/bin/<name>
#   COPY --chown=... <name> ...
_BINARY_COPY_TEMPLATE = r"^\s*COPY\s+(?:--[\w=]+\s+)*{name}(\s|$)"


@dataclass(frozen=True, slots=True)
class DockerfileAnchorResolver:
    """Splice overlays into a base Dockerfile at keyword-relative anchors.

    ``binary_name`` is required for the ``after-app-binary`` anchor;
    other anchors ignore it. Default ``""`` means "after-app-binary
    won't resolve" — that's acceptable when no overlay uses it.
    """

    binary_name: str = ""

    @property
    def known_anchors(self) -> list[str]:
        """List of all anchor names this resolver recognises (sorted)."""
        base = list(_SIMPLE_ANCHORS.keys()) + ["after-base-deps"]
        if self.binary_name:
            base.append("after-app-binary")
        return sorted(base)

    def splice(self, base: str, overlays: Iterable[Overlay]) -> str:
        """Splice ``overlays`` into ``base`` at their declared anchors.

        Multiple overlays at the same anchor are spliced in declaration
        order. Returns the spliced text. Raises :class:`AnchorNotFound`
        if any overlay's anchor doesn't resolve in the base.
        """
        # Group by anchor while preserving declaration order so multiple
        # overlays at the same anchor land contiguously and in input order.
        grouped: dict[str, list[Overlay]] = {}
        for o in overlays:
            grouped.setdefault(o.anchor, []).append(o)

        if not grouped:
            return base

        lines = base.splitlines(keepends=True)
        # Insertions: list of (line_index, position, text). Process from
        # bottom to top so earlier-line indices stay valid.
        insertions: list[tuple[int, str, str]] = []

        for anchor, group in grouped.items():
            line_index, position = self._resolve(anchor, lines)
            text_block = "\n".join(o.content.rstrip("\n") for o in group)
            # Each spliced block is its own logical paragraph — add a
            # trailing newline so the next line keeps its indent.
            block = text_block + ("\n" if not text_block.endswith("\n") else "")
            insertions.append((line_index, position, block))

        # Apply insertions bottom-up, before-then-after at the same line
        # so before-anchor lands above after-anchor at the same index.
        insertions.sort(key=lambda t: (t[0], 0 if t[1] == "after" else 1), reverse=True)
        for line_index, position, block in insertions:
            target = line_index + 1 if position == "after" else line_index
            lines.insert(target, block)

        return "".join(lines)

    # ---- internal -------------------------------------------------------

    def _resolve(self, anchor: str, lines: list[str]) -> tuple[int, str]:
        """Return ``(line_index, position)`` for ``anchor`` in ``lines``."""
        if anchor in _SIMPLE_ANCHORS:
            position, pattern = _SIMPLE_ANCHORS[anchor]
            for idx, line in enumerate(lines):
                if pattern.search(line):
                    return idx, position
            raise AnchorNotFound(
                anchor=anchor,
                artefact="Dockerfile",
                candidates=self.known_anchors,
            )

        if anchor == "after-base-deps":
            last_idx = -1
            for idx, line in enumerate(lines):
                if _PKG_MANAGER_RE.search(line):
                    last_idx = idx
            if last_idx >= 0:
                return last_idx, "after"
            raise AnchorNotFound(
                anchor=anchor,
                artefact="Dockerfile",
                candidates=self.known_anchors,
            )

        if anchor == "after-app-binary":
            if not self.binary_name:
                raise AnchorNotFound(
                    anchor=anchor,
                    artefact="Dockerfile",
                    candidates=self.known_anchors,
                )
            pattern = re.compile(
                _BINARY_COPY_TEMPLATE.format(name=re.escape(self.binary_name))
            )
            for idx, line in enumerate(lines):
                if pattern.search(line):
                    return idx, "after"
            raise AnchorNotFound(
                anchor=anchor,
                artefact="Dockerfile",
                candidates=self.known_anchors,
            )

        raise AnchorNotFound(
            anchor=anchor,
            artefact="Dockerfile",
            candidates=self.known_anchors,
        )
