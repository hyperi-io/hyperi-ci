# Project:   HyperI CI
# File:      src/hyperi_ci/deployment/__init__.py
# Purpose:   Deployment contract — Tier 3 producer (templater)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Deployment contract producer for hyperi-ci (Tier 3 of three-tier model).

Three-tier producer model unified by a single JSON Schema as the
language-agnostic source of truth:

  Tier 1 — hyperi-rustlib  (Rust apps emit their own contract + artefacts)
  Tier 2 — hyperi-pylib    (Python apps emit their own contract + artefacts)
  Tier 3 — hyperi-ci       (everything else — templates from committed
                            ci/deployment-contract.json)

This package provides Tier 3: the templater. Apps without a producer
framework commit `ci/deployment-contract.json`; their CI runs
`hyperi-ci emit-artefacts ci/` to regenerate Dockerfile,
Dockerfile.runtime, container-manifest.json, argocd-application.yaml,
and the Helm chart from that contract.

For all three tiers, output must be byte-identical for the same JSON
input. Cross-tier parity is verified via shared fixture suites.

See: docs/superpowers/specs/2026-04-30-deployment-contract-three-tier-design.md
"""

from hyperi_ci.deployment.contract import (
    AptRepoContract,
    DeploymentContract,
    HealthContract,
    ImageProfile,
    KedaContract,
    NativeDepsContract,
    OciLabels,
    PortContract,
    SecretEnvContract,
    SecretGroupContract,
)

__all__ = [
    "AptRepoContract",
    "DeploymentContract",
    "HealthContract",
    "ImageProfile",
    "KedaContract",
    "NativeDepsContract",
    "OciLabels",
    "PortContract",
    "SecretEnvContract",
    "SecretGroupContract",
]
