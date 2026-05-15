# Project:   HyperI CI
# File:      src/hyperi_ci/deployment/overlay/anchors/__init__.py
# Purpose:   Per-artefact anchor resolvers
#
# License:   Proprietary - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Anchor-resolution implementations for each deployment artefact.

Each resolver knows how to map an artefact-specific anchor name (e.g.
``before-user`` for Dockerfile, ``spec.source.append`` for ArgoCD) to
a concrete splice operation against the contract-generated base.
"""
