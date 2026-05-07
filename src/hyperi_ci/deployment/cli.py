# Project:   HyperI CI
# File:      src/hyperi_ci/deployment/cli.py
# Purpose:   `hyperi-ci emit-artefacts` subcommand handler
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""``hyperi-ci emit-artefacts`` — Tier 3 templater entry point.

Reads ``ci/deployment-contract.json``, validates it, and writes the
generated deployment artefacts (Dockerfile, Dockerfile.runtime,
container-manifest.json, argocd-application.yaml, chart/) under the
output directory.

This is the producer for Tier 3 apps in the three-tier deployment-contract
model. Tier 1 (rustlib) and Tier 2 (pylib) apps run their own binary's
``generate-artefacts`` subcommand; only repos with no producer framework
fall through to Tier 3 here.

For all three tiers, output is byte-identical for the same JSON input —
that's enforced by the parity test suite (see plan Phase 6).

Generators are not yet implemented (Phase 2 of the implementation plan,
blocked on rustlib 2.8.0 shipping the JSON schema export and the parity
fixture suite). This module currently:

  - parses CLI args and resolves the contract path
  - loads + validates the JSON via the Pydantic ``DeploymentContract``
  - enforces the schema_version gate
  - prints a summary of what *would* be written, then errors with a
    NotImplementedError pointing at the spec

Once Phase 2 lands, this same flow writes the actual artefacts.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from hyperi_ci.common import error, info
from hyperi_ci.deployment.contract import DeploymentContract

# Files the templater will write for every contract. Exposed here so the
# CLI's --help text and the docs/emit-artefacts.md reference can stay in
# sync with what's actually emitted.
ARTEFACT_FILES: tuple[str, ...] = (
    "Dockerfile",
    "Dockerfile.runtime",
    "container-manifest.json",
    "argocd-application.yaml",
    "chart/",
    "deployment-contract.schema.json",
)

# Exit codes the wrapping Typer command translates into typer.Exit().
# Match the spec's "Exits non-zero if" list so CI logs are actionable
# without re-reading the spec.
EXIT_OK = 0
EXIT_CONTRACT_MISSING = 2
EXIT_CONTRACT_INVALID = 3
EXIT_SCHEMA_TOO_NEW = 4
EXIT_NOT_IMPLEMENTED = 5
EXIT_IO_ERROR = 6


def emit_artefacts(
    output_dir: Path,
    contract_path: Path | None = None,
) -> int:
    """Run the emit-artefacts flow for a Tier 3 repo.

    Args:
        output_dir: Where artefacts get written. Created if missing.
            Existing files are overwritten without prompt — this is the
            CI-stage and ``ci/`` regen workflow, not interactive editing.
        contract_path: Path to the ``deployment-contract.json``. Defaults
            to ``<output_dir>/../ci/deployment-contract.json`` when
            ``output_dir`` looks like ``ci/``, else ``ci/deployment-contract.json``
            relative to the cwd.

    Returns:
        Exit code (one of the ``EXIT_*`` constants in this module).

    """
    contract_path = _resolve_contract_path(output_dir, contract_path)

    if not contract_path.is_file():
        error(f"contract not found: {contract_path}")
        info("expected location: ci/deployment-contract.json")
        info(
            "Tier 3 apps commit this file in their repo. Tier 1/2 apps "
            "use their own producer (`<app> generate-artefacts`)."
        )
        return EXIT_CONTRACT_MISSING

    contract = _load_and_validate(contract_path)
    if contract is None:
        return EXIT_CONTRACT_INVALID

    info(f"Loaded contract for {contract.app_name}")
    info(f"  schema_version: {contract.schema_version}")
    info(f"  base_image:     {contract.base_image}")
    info(f"  image_registry: {contract.image_registry}")
    info(f"  image_profile:  {contract.image_profile.value}")

    # Phase 2 (templating) is blocked on rustlib 2.8.0 + parity fixtures.
    # Until then, advertise the would-be output and error out with a
    # clear pointer so callers know exactly what's missing.
    error(
        "emit-artefacts: artefact templater is not yet implemented "
        "(Phase 2 of the deployment-contract plan)."
    )
    info(f"Would write the following under {output_dir}/:")
    for entry in ARTEFACT_FILES:
        info(f"  {entry}")
    info(
        "Plan: docs/superpowers/plans/"
        "2026-04-30-deployment-contract-three-tier.md (Phase 2)"
    )
    info(
        "Spec: docs/superpowers/specs/"
        "2026-04-30-deployment-contract-three-tier-design.md"
    )
    return EXIT_NOT_IMPLEMENTED


def _resolve_contract_path(
    output_dir: Path,
    explicit: Path | None,
) -> Path:
    """Resolve where the contract JSON lives.

    Three signals, in priority:
      1. Explicit ``--from`` path passed by the caller.
      2. If ``output_dir`` is named ``ci`` and it has a sibling
         ``deployment-contract.json``, use that — supports the
         "regenerate ci/ in place" idiom where output_dir == source dir.
      3. Else default to ``<cwd>/ci/deployment-contract.json``.
    """
    if explicit is not None:
        return explicit

    candidate = output_dir / "deployment-contract.json"
    if output_dir.name == "ci" and candidate.is_file():
        return candidate

    return Path.cwd() / "ci" / "deployment-contract.json"


def _load_and_validate(path: Path) -> DeploymentContract | None:
    """Read JSON, parse to ``DeploymentContract``, return None on failure.

    Errors print at ``[ERROR]`` level and the caller should propagate
    the appropriate non-zero exit code.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        error(f"contract JSON parse error: {exc}")
        return None
    except OSError as exc:
        error(f"contract read error: {exc}")
        return None

    if not isinstance(raw, dict):
        error(f"contract root must be a JSON object, got {type(raw).__name__}")
        return None

    try:
        return DeploymentContract.model_validate(raw)
    except ValidationError as exc:
        # Pydantic's pretty-error already includes the field path and
        # reason for each failure. Print as-is for actionability.
        error("contract validation failed:")
        for line in str(exc).splitlines():
            error(f"  {line}")
        return None
