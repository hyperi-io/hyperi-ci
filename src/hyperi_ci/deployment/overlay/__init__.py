# Project:   HyperI CI
# File:      src/hyperi_ci/deployment/overlay/__init__.py
# Purpose:   Public surface of the deployment-artefact overlay framework
#
# License:   Proprietary - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Post-contract amendment framework for deployment artefacts.

Consumers declare overlays in ``.hyperi-ci.yaml`` under
``publish.<artefact>.overlays:``. hyperi-ci's stage handlers (container,
helm, argocd) call into this package to splice the overlays into the
contract-generated base before passing the result to the downstream
build/package/apply tool.

The module is artefact-agnostic at its core (``model``, ``render``,
``errors``); each artefact contributes its own anchor resolver under
``anchors/``.

See: ``docs/superpowers/specs/2026-05-15-deployment-overlay-framework-spec.md``.
"""

from __future__ import annotations

from hyperi_ci.deployment.overlay.errors import (
    AnchorNotFound,
    OverlayError,
    OverlayFileMissing,
    OverlayValidationError,
)
from hyperi_ci.deployment.overlay.model import (
    HelmOverlays,
    Overlay,
    OverlayConfig,
)
from hyperi_ci.deployment.overlay.render import apply_overlays

__all__ = [
    "AnchorNotFound",
    "HelmOverlays",
    "Overlay",
    "OverlayConfig",
    "OverlayError",
    "OverlayFileMissing",
    "OverlayValidationError",
    "apply_overlays",
]
