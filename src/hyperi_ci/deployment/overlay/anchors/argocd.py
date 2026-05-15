# Project:   HyperI CI
# File:      src/hyperi_ci/deployment/overlay/anchors/argocd.py
# Purpose:   YAML-path-relative anchor resolver for ArgoCD Application overlays
#
# License:   Proprietary - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""ArgoCD Application overlay resolver.

The contract emits a single ArgoCD Application YAML; overlays are
applied by walking to a YAML path and merging fragment content.

Anchor catalog:

  * ``spec.source.before``        - prepend keys to spec.source map
  * ``spec.source.append``        - append keys to spec.source map (overwrite on collision)
  * ``spec.destination.append``   - append keys to spec.destination map
  * ``spec.syncPolicy.append``    - append keys to spec.syncPolicy map (creates if missing)
  * ``metadata.annotations.append`` - append annotations (creates map if missing)
  * ``metadata.labels.append``    - append labels (creates map if missing)
  * ``root.append``               - append top-level keys to the Application document

Where ``before`` / ``append`` semantics differ:
  * ``append`` does the equivalent of dict.update() at the path
  * ``before`` is reserved for ordered structures (lists); on a dict
    target it's effectively the same as append (Python dicts preserve
    insertion order, and YAML round-tripping respects that).

Each overlay's content must be a YAML map (deserialises to a dict);
the content's keys are merged into the resolved path target.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import yaml

from hyperi_ci.deployment.overlay.errors import AnchorNotFound
from hyperi_ci.deployment.overlay.model import Overlay

_KNOWN_ANCHORS = (
    "spec.source.before",
    "spec.source.append",
    "spec.destination.append",
    "spec.syncPolicy.append",
    "metadata.annotations.append",
    "metadata.labels.append",
    "root.append",
)

# Anchors where the target dict should be created if missing
# (e.g. annotations on a fresh Application that has none).
_AUTO_CREATE = frozenset(
    {
        "metadata.annotations.append",
        "metadata.labels.append",
        "spec.syncPolicy.append",
    }
)


@dataclass(frozen=True, slots=True)
class ArgoCDAnchorResolver:
    """Splice overlays into a base ArgoCD Application YAML at named anchors."""

    @property
    def known_anchors(self) -> list[str]:
        """List of all anchor names this resolver recognises (sorted)."""
        return sorted(_KNOWN_ANCHORS)

    def splice(self, base: str, overlays: Iterable[Overlay]) -> str:
        """Apply each overlay to ``base`` (a single ArgoCD Application YAML).

        Returns the modified YAML string (re-serialised).
        """
        doc = yaml.safe_load(base) or {}
        if not isinstance(doc, dict):
            raise AnchorNotFound(
                anchor="root",
                artefact="ArgoCD",
                candidates=self.known_anchors,
                base_excerpt=base[:200],
            )

        for index, overlay in enumerate(overlays):
            if overlay.anchor not in _KNOWN_ANCHORS:
                raise AnchorNotFound(
                    anchor=overlay.anchor,
                    artefact="ArgoCD",
                    candidates=self.known_anchors,
                )
            fragment = yaml.safe_load(overlay.content) or {}
            if not isinstance(fragment, dict):
                raise AnchorNotFound(
                    anchor=overlay.anchor,
                    artefact="ArgoCD",
                    candidates=self.known_anchors,
                    base_excerpt=overlay.content[:200],
                )
            self._apply_fragment(doc, overlay.anchor, fragment)

        return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)

    # ---- internal -------------------------------------------------------

    def _apply_fragment(
        self,
        doc: dict[str, Any],
        anchor: str,
        fragment: dict[str, Any],
    ) -> None:
        """Walk ``doc`` to the anchor's path; merge ``fragment`` keys."""
        path_str, _, position = anchor.rpartition(".")
        if path_str == "root":
            target = doc
        else:
            parts = path_str.split(".")
            current: Any = doc
            for part in parts:
                if not isinstance(current, dict):
                    raise AnchorNotFound(
                        anchor=anchor,
                        artefact="ArgoCD",
                        candidates=self.known_anchors,
                    )
                if part not in current:
                    if anchor in _AUTO_CREATE:
                        current[part] = {}
                    else:
                        raise AnchorNotFound(
                            anchor=anchor,
                            artefact="ArgoCD",
                            candidates=self.known_anchors,
                        )
                current = current[part]
            if not isinstance(current, dict):
                raise AnchorNotFound(
                    anchor=anchor,
                    artefact="ArgoCD",
                    candidates=self.known_anchors,
                )
            target = current

        # `before` and `append` differ semantically on lists; on dicts
        # both produce dict.update(). Python dicts preserve insertion
        # order so the visual diff for `before` would be different
        # from `append` only if the key set already had ordering
        # significance - keep the merge consistent for now.
        if position == "before":
            # Build a new dict with fragment keys first, then existing ones.
            merged = dict(fragment)
            for k, v in target.items():
                if k not in merged:
                    merged[k] = v
            target.clear()
            target.update(merged)
        else:  # append (overwrite on collision)
            target.update(fragment)
