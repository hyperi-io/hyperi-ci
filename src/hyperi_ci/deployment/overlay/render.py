# Project:   HyperI CI
# File:      src/hyperi_ci/deployment/overlay/render.py
# Purpose:   apply_overlays orchestrator - calls a per-artefact resolver
#
# License:   Proprietary - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Orchestrator for applying overlays to a base artefact.

This module is intentionally thin. It:

  1. Resolves each overlay's content (inline ``content`` or from
     ``file:`` reference, looking up files relative to ``base_dir``).
  2. Hands the resolved overlays to the artefact-specific resolver
     (caller supplies it).
  3. Returns the spliced output.

Splice mechanics (anchor catalog, insertion semantics) live in the
resolver — see ``anchors/dockerfile.py``, ``anchors/helm.py``,
``anchors/argocd.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from hyperi_ci.deployment.overlay.model import Overlay


class _Resolver(Protocol):
    """Anything that knows how to splice overlays into a base string."""

    def splice(self, base: str, overlays: list[Overlay]) -> str:
        ...


@dataclass(frozen=True, slots=True)
class _ResolvedOverlay:
    """An overlay with its file content already loaded."""

    anchor: str
    content: str


def apply_overlays(
    *,
    base: str,
    overlays: list[Overlay] | tuple[Overlay, ...],
    resolver: _Resolver,
    base_dir: Path,
    artefact: str,
) -> str:
    """Splice ``overlays`` into ``base`` using ``resolver``.

    Args:
        base: The contract-generated artefact text (e.g. Dockerfile).
        overlays: Overlay declarations from ``.hyperi-ci.yaml``.
        resolver: Per-artefact splice mechanic.
        base_dir: Directory used to resolve relative ``file:`` references.
        artefact: Artefact label for error messages
            (``"container"`` / ``"helm"`` / ``"argocd"``).

    Returns:
        The spliced artefact text.

    """
    if not overlays:
        return base

    resolved: list[Overlay] = []
    for index, o in enumerate(overlays):
        text = o.resolve(base_dir=base_dir, artefact=artefact, index=index)
        resolved.append(Overlay(anchor=o.anchor, content=text))

    return resolver.splice(base, resolved)
