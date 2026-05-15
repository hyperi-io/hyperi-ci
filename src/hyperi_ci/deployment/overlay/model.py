# Project:   HyperI CI
# File:      src/hyperi_ci/deployment/overlay/model.py
# Purpose:   Overlay declaration models + parser
#
# License:   Proprietary - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Overlay declaration models + parsers.

Two shapes:

* :class:`Overlay` — used by Dockerfile and ArgoCD overlays. Single
  ``anchor`` + (``content`` xor ``file``).
* :class:`HelmOverlays` — used by Helm overlays. Two-shape decision
  (adds vs patches) per the upstream framework spec, because Helm
  charts are directories and Kustomize patches are the idiomatic
  modify mechanism.

Both load from the equivalent yaml shape under ``publish.<artefact>``
and validate at load time. Validation errors surface as
:class:`OverlayValidationError` with structured location info so the
yaml line of the offending entry can be pin-pointed in error messages.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hyperi_ci.deployment.overlay.errors import (
    OverlayFileMissing,
    OverlayValidationError,
)


@dataclass(frozen=True, slots=True)
class Overlay:
    """A single overlay fragment to splice at a named anchor.

    Exactly one of ``content`` or ``file`` is set; the other is empty.
    Validation enforces this at load time.
    """

    anchor: str
    content: str = ""
    file: Path | None = None

    def resolve(self, *, base_dir: Path, artefact: str, index: int) -> str:
        """Read the fragment text — from ``content`` or ``file``."""
        if self.content:
            return self.content
        if self.file is None:
            raise OverlayValidationError(
                "overlay must set exactly one of `content` or `file`",
                artefact=artefact,
                overlay_index=index,
            )
        path = self.file if self.file.is_absolute() else base_dir / self.file
        if not path.exists():
            raise OverlayFileMissing(
                path=path, artefact=artefact, overlay_index=index
            )
        return path.read_text(encoding="utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class HelmAddOverlay:
    """Drop a new template file into the chart's ``templates/`` directory.

    ``path`` is the destination (relative to the chart root);
    ``file`` (or ``content``) is the source.
    """

    path: str
    content: str = ""
    file: Path | None = None

    def resolve(self, *, base_dir: Path, index: int) -> str:
        """Read the template content — from ``content`` or ``file``."""
        if self.content:
            return self.content
        if self.file is None:
            raise OverlayValidationError(
                "helm add overlay must set exactly one of `content` or `file`",
                artefact="helm",
                overlay_index=index,
            )
        path = self.file if self.file.is_absolute() else base_dir / self.file
        if not path.exists():
            raise OverlayFileMissing(
                path=path, artefact="helm", overlay_index=index
            )
        return path.read_text(encoding="utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class HelmPatchOverlay:
    """Strategic-merge or JSON-6902 patch against a rendered Helm template.

    ``target`` selects the resource (Kustomize-style: kind, name,
    optional namespace + labels). ``patch`` (or ``patch_file``) is the
    patch body.
    """

    target: dict[str, Any]
    patch: str = ""
    patch_file: Path | None = None

    def resolve_patch(self, *, base_dir: Path, index: int) -> str:
        """Read the patch text — from inline ``patch`` or ``patch_file``."""
        if self.patch:
            return self.patch
        if self.patch_file is None:
            raise OverlayValidationError(
                "helm patch overlay must set exactly one of `patch` or `patch_file`",
                artefact="helm",
                overlay_index=index,
            )
        path = (
            self.patch_file
            if self.patch_file.is_absolute()
            else base_dir / self.patch_file
        )
        if not path.exists():
            raise OverlayFileMissing(
                path=path, artefact="helm", overlay_index=index
            )
        return path.read_text(encoding="utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class HelmOverlays:
    """All Helm overlays for a release: zero-or-more adds, zero-or-more patches."""

    adds: tuple[HelmAddOverlay, ...] = field(default_factory=tuple)
    patches: tuple[HelmPatchOverlay, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class OverlayConfig:
    """Full ``publish.*.overlays`` block parsed from ``.hyperi-ci.yaml``."""

    container: tuple[Overlay, ...] = field(default_factory=tuple)
    helm: HelmOverlays = field(default_factory=HelmOverlays)
    argocd: tuple[Overlay, ...] = field(default_factory=tuple)


# ---- parsers --------------------------------------------------------------


def _parse_simple_overlay(raw: Any, *, artefact: str, index: int) -> Overlay:
    """Parse a single Dockerfile/ArgoCD overlay entry.

    Shape: ``{anchor: str, content: str | None, file: str | None}``.
    Exactly one of ``content``/``file`` must be set.
    """
    if not isinstance(raw, dict):
        raise OverlayValidationError(
            f"overlay must be a mapping, got {type(raw).__name__}",
            artefact=artefact,
            overlay_index=index,
        )
    anchor = raw.get("anchor")
    if not isinstance(anchor, str) or not anchor:
        raise OverlayValidationError(
            "overlay missing required string `anchor`",
            artefact=artefact,
            overlay_index=index,
        )
    content = raw.get("content", "") or ""
    file_raw = raw.get("file")
    file_path = Path(str(file_raw)) if file_raw else None

    if bool(content) == bool(file_path):
        raise OverlayValidationError(
            "overlay must set exactly one of `content` or `file` "
            f"(got content={bool(content)}, file={bool(file_path)})",
            artefact=artefact,
            overlay_index=index,
        )

    return Overlay(anchor=anchor, content=content, file=file_path)


def parse_simple_overlays(
    raw_list: Any, *, artefact: str
) -> tuple[Overlay, ...]:
    """Parse a list of Dockerfile/ArgoCD overlays."""
    if raw_list is None:
        return ()
    if not isinstance(raw_list, list):
        raise OverlayValidationError(
            f"overlays must be a list, got {type(raw_list).__name__}",
            artefact=artefact,
        )
    return tuple(
        _parse_simple_overlay(item, artefact=artefact, index=i)
        for i, item in enumerate(raw_list)
    )


def _parse_helm_add(raw: Any, *, index: int) -> HelmAddOverlay:
    if not isinstance(raw, dict):
        raise OverlayValidationError(
            f"helm add overlay must be a mapping, got {type(raw).__name__}",
            artefact="helm",
            overlay_index=index,
        )
    path = raw.get("path")
    if not isinstance(path, str) or not path:
        raise OverlayValidationError(
            "helm add overlay missing required string `path`",
            artefact="helm",
            overlay_index=index,
        )
    content = raw.get("content", "") or ""
    file_raw = raw.get("file")
    file_path = Path(str(file_raw)) if file_raw else None
    if bool(content) == bool(file_path):
        raise OverlayValidationError(
            "helm add overlay must set exactly one of `content` or `file`",
            artefact="helm",
            overlay_index=index,
        )
    return HelmAddOverlay(path=path, content=content, file=file_path)


def _parse_helm_patch(raw: Any, *, index: int) -> HelmPatchOverlay:
    if not isinstance(raw, dict):
        raise OverlayValidationError(
            f"helm patch overlay must be a mapping, got {type(raw).__name__}",
            artefact="helm",
            overlay_index=index,
        )
    target = raw.get("target")
    if not isinstance(target, dict) or not target:
        raise OverlayValidationError(
            "helm patch overlay missing required mapping `target`",
            artefact="helm",
            overlay_index=index,
        )
    patch = raw.get("patch", "") or ""
    patch_file_raw = raw.get("patch_file")
    patch_file = Path(str(patch_file_raw)) if patch_file_raw else None
    if bool(patch) == bool(patch_file):
        raise OverlayValidationError(
            "helm patch overlay must set exactly one of `patch` or `patch_file`",
            artefact="helm",
            overlay_index=index,
        )
    return HelmPatchOverlay(
        target=dict(target), patch=patch, patch_file=patch_file
    )


def parse_helm_overlays(raw: Any) -> HelmOverlays:
    """Parse the helm overlay block: ``{adds: [...], patches: [...]}``."""
    if raw is None:
        return HelmOverlays()
    if not isinstance(raw, dict):
        raise OverlayValidationError(
            f"helm overlays must be a mapping, got {type(raw).__name__}",
            artefact="helm",
        )
    adds_raw = raw.get("adds") or []
    patches_raw = raw.get("patches") or []
    if not isinstance(adds_raw, list):
        raise OverlayValidationError(
            "helm overlays.adds must be a list", artefact="helm"
        )
    if not isinstance(patches_raw, list):
        raise OverlayValidationError(
            "helm overlays.patches must be a list", artefact="helm"
        )
    adds = tuple(
        _parse_helm_add(item, index=i) for i, item in enumerate(adds_raw)
    )
    patches = tuple(
        _parse_helm_patch(item, index=i) for i, item in enumerate(patches_raw)
    )
    return HelmOverlays(adds=adds, patches=patches)


def parse_overlay_config(publish_block: dict[str, Any] | None) -> OverlayConfig:
    """Parse the full ``publish:`` block's overlays into one OverlayConfig.

    Accepts the merged ``publish:`` dict from ``.hyperi-ci.yaml``;
    returns an OverlayConfig with empty tuples for any artefact that
    has no overlays declared (so the caller never sees None).
    """
    if not publish_block:
        return OverlayConfig()
    container_raw = (publish_block.get("container") or {}).get("overlays")
    helm_raw = (publish_block.get("helm") or {}).get("overlays")
    argocd_raw = (publish_block.get("argocd") or {}).get("overlays")
    return OverlayConfig(
        container=parse_simple_overlays(container_raw, artefact="container"),
        helm=parse_helm_overlays(helm_raw),
        argocd=parse_simple_overlays(argocd_raw, artefact="argocd"),
    )


def overlay_grouped_by_anchor(
    overlays: Iterable[Overlay],
) -> dict[str, list[Overlay]]:
    """Group overlays by anchor preserving declaration order within each anchor."""
    groups: dict[str, list[Overlay]] = {}
    for o in overlays:
        groups.setdefault(o.anchor, []).append(o)
    return groups
