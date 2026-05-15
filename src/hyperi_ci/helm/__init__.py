# Project:   HyperI CI
# File:      src/hyperi_ci/helm/__init__.py
# Purpose:   Helm packaging + GHCR OCI publish
#
# License:   Proprietary - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Helm chart packaging + publish to GHCR OCI Helm.

Stage flow (per overlay-framework spec section 10.4):

  1. Invoke consumer's ``emit-chart <tmp>`` subcommand
  2. Apply ``publish.helm.overlays.adds`` (drop new templates into chart)
  3. ``helm lint <chart>``
  4. ``helm template <chart>`` to render full manifest stream
  5. Apply ``publish.helm.overlays.patches`` to rendered output
  6. ``helm package <chart>`` to produce the .tgz
  7. ``helm push <chart>.tgz oci://ghcr.io/hyperi-io/helm-charts``

Auth uses the existing GHCR_TOKEN / GITHUB_TOKEN; no separate
credentials needed.
"""
