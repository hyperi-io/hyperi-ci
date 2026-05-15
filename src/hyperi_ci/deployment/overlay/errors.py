# Project:   HyperI CI
# File:      src/hyperi_ci/deployment/overlay/errors.py
# Purpose:   Error taxonomy for overlay processing
#
# License:   Proprietary - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Overlay-framework error types.

All overlay errors derive from :class:`OverlayError` so callers can
catch broadly. Each subclass carries the structured context needed to
produce an actionable message — anchor name, candidate landmarks,
file paths, line numbers — without forcing the caller to re-derive
them from a stringified message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


class OverlayError(Exception):
    """Base class for all overlay-processing errors."""


@dataclass
class OverlayValidationError(OverlayError):
    """An overlay declaration in ``.hyperi-ci.yaml`` is structurally invalid.

    Raised at config-load time, before any contract generator is
    invoked. Surfaces shape errors fast so consumers see them in
    ``hyperi-ci check`` rather than mid-build.
    """

    message: str
    artefact: str = ""
    overlay_index: int = -1

    def __post_init__(self) -> None:  # noqa: D105
        super().__init__(self.__str__())

    def __str__(self) -> str:  # noqa: D105
        loc = ""
        if self.artefact and self.overlay_index >= 0:
            loc = f" (publish.{self.artefact}.overlays[{self.overlay_index}])"
        elif self.artefact:
            loc = f" (publish.{self.artefact}.overlays)"
        return f"{self.message}{loc}"


@dataclass
class OverlayFileMissing(OverlayError):  # noqa: N818 — name reads naturally; suffix would be redundant
    """An overlay's ``file:`` reference doesn't exist on disk."""

    path: Path
    artefact: str = ""
    overlay_index: int = -1

    def __post_init__(self) -> None:  # noqa: D105
        super().__init__(self.__str__())

    def __str__(self) -> str:  # noqa: D105
        loc = (
            f" (publish.{self.artefact}.overlays[{self.overlay_index}])"
            if self.artefact and self.overlay_index >= 0
            else ""
        )
        return f"overlay fragment file not found: {self.path}{loc}"


@dataclass
class AnchorNotFound(OverlayError):  # noqa: N818 — name reads naturally; suffix would be redundant
    """The named anchor doesn't exist in the base artefact."""

    anchor: str
    artefact: str
    candidates: list[str] = field(default_factory=list)
    base_excerpt: str = ""

    def __post_init__(self) -> None:  # noqa: D105
        super().__init__(self.__str__())

    def __str__(self) -> str:  # noqa: D105
        msg = f"overlay anchor {self.anchor!r} not found in {self.artefact} base"
        if self.candidates:
            msg += f" (known anchors for this artefact: {sorted(self.candidates)!r})"
        return msg
