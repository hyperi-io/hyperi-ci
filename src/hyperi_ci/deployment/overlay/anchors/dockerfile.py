# Project:   HyperI CI
# File:      src/hyperi_ci/deployment/overlay/anchors/dockerfile.py
# Purpose:   Keyword-relative anchor resolver for Dockerfile overlays
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Dockerfile anchor resolver.

Anchor names map to positions relative to landmark instructions in the
base Dockerfile. The contract-generated Dockerfile (scalo's
``generate_dockerfile()`` or hyperi-ci's own generators) emits a
predictable shape, which makes keyword matching unambiguous and avoids
needing scalo to emit explicit marker comments.

Anchors resolve against LOGICAL instructions, not physical lines: an
instruction runs to the end of its trailing-backslash continuations and
any heredoc bodies it opens, so an overlay never lands inside one.

If a future consumer needs a finer-grained anchor that doesn't map to
a Dockerfile keyword landmark, revisit by either (a) adding a new
keyword anchor here that the consumer's contract-generator already
emits, or (b) introducing scalo-side marker comments -- but only
when at least one consumer actually pulls for it (Rule of Three).

Anchor catalog (order = position-in-file):

    - ``after-base-image``    : after the first ``FROM`` instruction
    - ``after-base-deps``     : after the LAST ``RUN`` in the final
                                build stage that invokes a package
                                manager (apt-get / apt / dnf / yum /
                                microdnf / apk / pacman / zypper)
                                anywhere in its command
    - ``after-app-binary``    : after a ``COPY <name> ...`` instruction
                                where ``<name>`` matches the binary name
                                supplied as resolver context
    - ``before-user``         : before the ``USER`` instruction
    - ``before-healthcheck``  : before the ``HEALTHCHECK`` instruction
    - ``before-entrypoint``   : before the ``ENTRYPOINT`` or ``CMD``
                                instruction
    - ``end-of-image``        : alias of ``before-entrypoint``
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

from hyperi_ci.deployment.overlay.errors import AnchorNotFound
from hyperi_ci.deployment.overlay.model import Overlay

# Anchor names that don't need positional context, mapped to
# (where, instruction keywords). `where` is "before" | "after".
_SIMPLE_ANCHORS: dict[str, tuple[str, frozenset[str]]] = {
    "after-base-image": ("after", frozenset({"FROM"})),
    "before-user": ("before", frozenset({"USER"})),
    "before-healthcheck": ("before", frozenset({"HEALTHCHECK"})),
    "before-entrypoint": ("before", frozenset({"ENTRYPOINT", "CMD"})),
    "end-of-image": ("before", frozenset({"ENTRYPOINT", "CMD"})),
}

# A package manager in command position, so `/var/lib/apt/lists` does not count.
_PKG_MANAGER_RE = re.compile(
    r"(?:^|[\s;&|(`])(?:apt-get|apt|dnf|yum|microdnf|apk|pacman|zypper)\s"
)

# Recognised binary-COPY shape for `after-app-binary`. Matches:
#   COPY <name> /usr/local/bin/<name>
#   COPY --chown=... <name> ...
_BINARY_COPY_TEMPLATE = r"^\s*COPY\s+(?:--[\w=]+\s+)*{name}(\s|$)"

# `<<EOF`, `<<-EOF`, `<<"EOF"`; the lookarounds reject a `<<<` here-string.
_HEREDOC_RE = re.compile(r"(?<!<)<<(?!<)(-?)([\"']?)([A-Za-z_]\w*)\2")

# Only these instructions take heredocs (Dockerfile syntax 1.4+).
_HEREDOC_KEYWORDS = frozenset({"RUN", "COPY", "ADD"})


@dataclass(frozen=True, slots=True)
class _Instruction:
    """One logical Dockerfile instruction.

    Attributes:
        keyword: The instruction keyword, upper-cased.
        start: Index of its first physical line.
        end: Index of its last physical line, continuations and heredocs included.
        text: Its physical lines joined, heredoc bodies included.
    """

    keyword: str
    start: int
    end: int
    text: str


def _is_blank_or_comment(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _continuation_end(lines: list[str], start: int) -> int:
    """Return the last physical line of the instruction beginning at ``start``.

    Blank and comment lines inside a continuation are dropped by the
    Dockerfile parser rather than ending the instruction, so they are
    skipped here too.
    """
    end = start
    while lines[end].rstrip().endswith("\\"):
        nxt = end + 1
        while nxt < len(lines) and _is_blank_or_comment(lines[nxt]):
            nxt += 1
        if nxt >= len(lines):
            break
        end = nxt
    return end


def _heredoc_end(lines: list[str], head: str, end: int) -> int:
    """Return the last line of the heredoc bodies ``head`` opens after ``end``."""
    for match in _HEREDOC_RE.finditer(head):
        strip_tabs = match.group(1) == "-"
        delimiter = match.group(3)
        idx = end + 1
        while idx < len(lines):
            body_line = lines[idx].rstrip("\r\n")
            if strip_tabs:
                body_line = body_line.lstrip("\t")
            if body_line == delimiter:
                break
            idx += 1
        end = min(idx, len(lines) - 1)
    return end


def _parse_instructions(lines: list[str]) -> list[_Instruction]:
    """Group physical ``lines`` into logical instructions, in file order."""
    instructions: list[_Instruction] = []
    idx = 0
    while idx < len(lines):
        if _is_blank_or_comment(lines[idx]):
            idx += 1
            continue
        start = idx
        keyword = lines[start].split(maxsplit=1)[0].upper()
        end = _continuation_end(lines, start)
        if keyword in _HEREDOC_KEYWORDS:
            head = "".join(lines[start : end + 1])
            end = _heredoc_end(lines, head, end)
        text = "".join(lines[start : end + 1])
        instructions.append(_Instruction(keyword, start, end, text))
        idx = end + 1
    return instructions


@dataclass(frozen=True, slots=True)
class DockerfileAnchorResolver:
    """Splice overlays into a base Dockerfile at keyword-relative anchors.

    ``binary_name`` is required for the ``after-app-binary`` anchor;
    other anchors ignore it. Default ``""`` means "after-app-binary
    won't resolve" -- that's acceptable when no overlay uses it.
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
        order. An ``after`` anchor lands after the whole logical
        instruction and a ``before`` anchor above its first line. Returns
        the spliced text. Raises :class:`AnchorNotFound` if any overlay's
        anchor doesn't resolve in the base.
        """
        # Group by anchor while preserving declaration order so multiple
        # overlays at the same anchor land contiguously and in input order.
        grouped: dict[str, list[Overlay]] = {}
        for o in overlays:
            grouped.setdefault(o.anchor, []).append(o)

        if not grouped:
            return base

        lines = base.splitlines(keepends=True)
        instructions = _parse_instructions(lines)

        # (insert-before-line-index, position, block); an index of
        # len(lines) appends at the end of the file.
        insertions: list[tuple[int, str, str]] = []
        for anchor, group in grouped.items():
            instruction, position = self._resolve(anchor, instructions)
            text_block = "\n".join(o.content.rstrip("\n") for o in group)
            # Each spliced block is its own logical paragraph -- add a
            # trailing newline so the next line keeps its indent.
            block = text_block + ("\n" if not text_block.endswith("\n") else "")
            target = instruction.end + 1 if position == "after" else instruction.start
            insertions.append((target, position, block))

        # At a shared insertion point, the previous instruction's "after"
        # blocks come before the next instruction's "before" blocks.
        insertions.sort(key=lambda t: (t[0], 0 if t[1] == "after" else 1))

        appends_at_eof = insertions[-1][0] == len(lines)
        if appends_at_eof and lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        out: list[str] = []
        pending = iter(insertions)
        nxt = next(pending, None)
        for idx in range(len(lines) + 1):
            while nxt is not None and nxt[0] == idx:
                out.append(nxt[2])
                nxt = next(pending, None)
            if idx < len(lines):
                out.append(lines[idx])
        return "".join(out)

    # ---- internal -------------------------------------------------------

    def _not_found(self, anchor: str) -> AnchorNotFound:
        return AnchorNotFound(
            anchor=anchor,
            artefact="Dockerfile",
            candidates=self.known_anchors,
        )

    def _resolve(
        self, anchor: str, instructions: list[_Instruction]
    ) -> tuple[_Instruction, str]:
        """Return ``(instruction, position)`` for ``anchor``."""
        if anchor in _SIMPLE_ANCHORS:
            position, keywords = _SIMPLE_ANCHORS[anchor]
            for instruction in instructions:
                if instruction.keyword in keywords:
                    return instruction, position
            raise self._not_found(anchor)

        if anchor == "after-base-deps":
            # Packages installed in an earlier build stage never reach the image.
            final_from = max(
                (i for i, ins in enumerate(instructions) if ins.keyword == "FROM"),
                default=0,
            )
            match: _Instruction | None = None
            for instruction in instructions[final_from:]:
                if instruction.keyword == "RUN" and _PKG_MANAGER_RE.search(
                    instruction.text
                ):
                    match = instruction
            if match is None:
                raise self._not_found(anchor)
            return match, "after"

        if anchor == "after-app-binary":
            if not self.binary_name:
                raise self._not_found(anchor)
            pattern = re.compile(
                _BINARY_COPY_TEMPLATE.format(name=re.escape(self.binary_name))
            )
            for instruction in instructions:
                if instruction.keyword == "COPY" and pattern.search(instruction.text):
                    return instruction, "after"
            raise self._not_found(anchor)

        raise self._not_found(anchor)
