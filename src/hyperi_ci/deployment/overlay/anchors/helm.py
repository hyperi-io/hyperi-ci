# Project:   HyperI CI
# File:      src/hyperi_ci/deployment/overlay/anchors/helm.py
# Purpose:   Helm overlay resolver - adds (template-overlay) + patches (post-renderer)
#
# License:   Proprietary - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Helm overlay resolver.

Helm charts are directories of templates. Two distinct overlay
mechanisms apply:

  * **Adds** — drop additional template files into ``templates/`` of
    the contract-generated chart before ``helm package``. Use for
    adding net-new resources (sidecar configs, PVCs, extra secrets).
  * **Patches** — strategic-merge or JSON 6902 patches applied to the
    rendered chart output (post-renderer pattern). Use for modifying
    existing resources (add a volume to the Deployment, override a
    container env var).

Both operate at distinct points in the helm pipeline:

  1. Generate base chart  (consumer's ``emit-chart`` subcommand)
  2. apply_adds()         ← writes new templates into chart/templates/
  3. helm template        (renders chart with default values)
  4. apply_patches()      ← rewrites rendered YAML
  5. helm package         (final tarball)
  6. helm push            (oci://ghcr.io/hyperi-io/helm-charts)

Step 4 is the post-renderer; it produces a single multi-doc YAML
stream that downstream `helm template` consumers (kustomize, etc.)
also produce. Patches use the standard Kubernetes strategic-merge
algorithm via JSON 6902 fallback for list operations.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from hyperi_ci.deployment.overlay.errors import (
    AnchorNotFound,
    OverlayValidationError,
)
from hyperi_ci.deployment.overlay.model import (
    HelmAddOverlay,
    HelmPatchOverlay,
)


@dataclass(frozen=True, slots=True)
class HelmAnchorResolver:
    """Apply Helm adds + patches per the framework spec section 3.2."""

    def apply_adds(
        self,
        *,
        chart_dir: Path,
        adds: Iterable[HelmAddOverlay],
        base_dir: Path,
    ) -> list[Path]:
        """Drop add-overlay fragments into the chart's ``templates/`` dir.

        Returns the list of written paths (for logging / drift checks).

        Raises :class:`OverlayValidationError` if an add's destination
        path already exists in the chart (overlays add resources, never
        silently overwrite generated ones — use ``patches:`` for that).
        """
        written: list[Path] = []
        for index, add in enumerate(adds):
            dest = chart_dir / add.path
            if dest.exists():
                raise OverlayValidationError(
                    f"helm add overlay would overwrite existing chart file "
                    f"{add.path!r} - use `patches:` to modify rendered output, "
                    f"or rename the destination to avoid the collision",
                    artefact="helm",
                    overlay_index=index,
                )
            dest.parent.mkdir(parents=True, exist_ok=True)
            content = add.resolve(base_dir=base_dir, index=index)
            dest.write_text(content, encoding="utf-8", newline="\n")
            written.append(dest)
        return written

    def apply_patches(
        self,
        *,
        rendered_yaml: str,
        patches: Iterable[HelmPatchOverlay],
        base_dir: Path,
    ) -> str:
        """Apply patches to the rendered multi-doc YAML stream.

        Each patch's ``target`` (kind + name + optional namespace) selects
        ONE document in the stream; the patch is then merged in
        strategic-merge style (maps deep-merge, lists replace).

        Raises :class:`AnchorNotFound` if a patch's target doesn't match
        any rendered document. Raises :class:`OverlayValidationError` if
        more than one document matches (target should be unambiguous).
        """
        # Parse multi-doc YAML; preserve order so the rebuilt stream is
        # diff-friendly against the input.
        docs: list[dict[str, Any] | None] = list(yaml.safe_load_all(rendered_yaml))

        for index, patch in enumerate(patches):
            patch_text = patch.resolve_patch(base_dir=base_dir, index=index)
            patch_doc = yaml.safe_load(patch_text)
            if not isinstance(patch_doc, dict):
                raise OverlayValidationError(
                    "helm patch body must be a YAML mapping",
                    artefact="helm",
                    overlay_index=index,
                )

            matches = [
                (i, doc)
                for i, doc in enumerate(docs)
                if doc and _matches_target(doc, patch.target)
            ]
            if not matches:
                kinds = sorted({d.get("kind", "?") for d in docs if d})
                raise AnchorNotFound(
                    anchor=str(patch.target),
                    artefact="Helm",
                    candidates=kinds,
                )
            if len(matches) > 1:
                raise OverlayValidationError(
                    f"helm patch target {patch.target!r} matched "
                    f"{len(matches)} rendered documents - target must be "
                    f"unambiguous (add `namespace:` or `labels:` to narrow)",
                    artefact="helm",
                    overlay_index=index,
                )
            i, original = matches[0]
            docs[i] = _strategic_merge(original, patch_doc)

        # Re-emit as multi-doc stream. Drop None placeholders (from
        # empty documents in the input).
        return "---\n".join(
            yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
            for doc in docs
            if doc is not None
        )


def _matches_target(doc: dict[str, Any], target: dict[str, Any]) -> bool:
    """Check whether ``doc`` matches a Kustomize-style target selector.

    Supported keys in ``target``:
      * kind - exact match required
      * name - exact match required
      * namespace - exact match required IF specified in target
      * labels - all entries must match metadata.labels
    """
    kind = target.get("kind")
    name = target.get("name")
    if kind and doc.get("kind") != kind:
        return False
    metadata = doc.get("metadata") or {}
    if name and metadata.get("name") != name:
        return False
    namespace = target.get("namespace")
    if namespace and metadata.get("namespace") != namespace:
        return False
    label_filter = target.get("labels") or {}
    if label_filter:
        labels = metadata.get("labels") or {}
        for k, v in label_filter.items():
            if labels.get(k) != v:
                return False
    return True


def _strategic_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``patch`` into ``base``.

    Rules:
      * Dicts merge key-by-key (recursive).
      * Lists in patch REPLACE the list in base (strategic-merge default;
        for list-append behaviour use a JSON 6902 patch via patch_file
        with operations - future-extension if needed).
      * Scalars in patch overwrite base.
      * ``$patch: delete`` value at any key removes that key from base.
    """
    result: dict[str, Any] = dict(base)
    for key, val in patch.items():
        if val == {"$patch": "delete"}:
            result.pop(key, None)
            continue
        if isinstance(val, dict) and isinstance(result.get(key), dict):
            result[key] = _strategic_merge(result[key], val)
        else:
            result[key] = val
    return result
