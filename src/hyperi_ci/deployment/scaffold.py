# Project:   HyperI CI
# File:      src/hyperi_ci/deployment/scaffold.py
# Purpose:   Scaffold a starter ci/deployment-contract.json for Tier 3 apps
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Scaffold a starter `deployment-contract.json` for Tier 3 onboarding.

`hyperi-ci init-contract --app-name foo` writes a contract with sensible
defaults under ``ci/deployment-contract.json``. The output validates
against the Pydantic ``DeploymentContract`` so the very first run of
``emit-artefacts`` works without manual editing.

Used by:
  - First-time Tier 3 onboarding (bash / TS / Go apps that don't have
    a producer framework).
  - Quick smoke testing of the contract pipeline.

Tier 1 (rustlib) and Tier 2 (pylib) apps DO NOT use this — they
construct their contract from the app's config cascade, not from a
template. Calling ``init-contract`` in a rustlib repo would produce a
contract that drifts from the source of truth.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from hyperi_ci.common import error, info, success
from hyperi_ci.deployment.contract import (
    DeploymentContract,
    HealthContract,
)

# Exit codes — match the spec's pattern: 0 success, non-zero on every
# failure mode.
EXIT_OK = 0
EXIT_INVALID_NAME = 2
EXIT_ALREADY_EXISTS = 3
EXIT_IO_ERROR = 4

# App-name validation. Matches the org repo-naming convention from
# universal.md: lowercase, hyphen-separated, no underscores, no
# camelCase. Length cap chosen to fit comfortably in a K8s label
# (max 63 chars including any prefix the Helm chart adds).
_APP_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,49}[a-z0-9]$")


def init_contract(
    output_dir: Path,
    app_name: str,
    *,
    force: bool = False,
) -> int:
    """Scaffold a starter ``deployment-contract.json``.

    Writes ``<output_dir>/deployment-contract.json`` derived from the
    Pydantic ``DeploymentContract`` defaults. Field defaults map to
    rustlib's defaults so a Tier 3 starter and a Tier 1 default emit
    byte-identical artefacts (same base_image, image_registry,
    health paths, vendor/license labels, etc.).

    Args:
        output_dir: Directory to write into. Usually ``ci/``. Created
            if missing.
        app_name: Application name. Must match the org convention
            (lowercase, hyphenated). Becomes ``app_name`` in the
            contract and is also used to derive ``binary_name``,
            ``env_prefix``, and ``metric_prefix`` from sensible
            defaults.
        force: Overwrite an existing file. Default False — onboarding
            should not silently clobber an existing contract.

    Returns:
        Exit code (one of the ``EXIT_*`` constants).

    """
    if not _APP_NAME_RE.fullmatch(app_name):
        error(f"invalid app_name: {app_name!r}")
        info(
            "must be lowercase, hyphen-separated, "
            "start with a letter, end alphanumeric, 3–50 chars."
        )
        info("examples: dfe-loader, my-app, ci-test-rust-app")
        return EXIT_INVALID_NAME

    target = output_dir / "deployment-contract.json"
    if target.exists() and not force:
        error(f"contract already exists: {target}")
        info("re-run with --force to overwrite, or edit the file directly.")
        return EXIT_ALREADY_EXISTS

    contract = _starter_contract(app_name)
    payload = contract.model_dump_json(indent=2) + "\n"

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
    except OSError as exc:
        error(f"failed to write {target}: {exc}")
        return EXIT_IO_ERROR

    success(f"Wrote {target}")
    info(
        "Edit the file to match your app's actual config "
        "(env vars, metrics port, secrets, KEDA scaling)."
    )
    info(
        "Then run `hyperi-ci emit-artefacts ci/` "
        "to generate Dockerfile, chart/, etc. (Phase 2 — coming once "
        "rustlib 2.8.0 ships the parity fixtures)."
    )
    return EXIT_OK


def _starter_contract(app_name: str) -> DeploymentContract:
    """Build a default contract for ``app_name``.

    Defaults derive every field from ``app_name`` so the scaffolded
    contract works without further editing:

      - ``binary_name`` defaults to app_name (rustlib's fallback).
      - ``env_prefix`` is the SCREAMING_SNAKE form (``my-app`` →
        ``MY_APP``). DFE convention.
      - ``metric_prefix`` is the snake form (``my-app`` → ``my_app``).
        Becomes the Prometheus namespace.
      - ``config_mount_path`` follows ``/etc/<app>/<app>.yaml`` —
        same convention as dfe-loader / dfe-receiver.
      - ``description`` is empty — the operator should fill this in.
        Not auto-generating a placeholder so it shows up cleanly in
        ``ci/`` diffs as a TODO for the human.
    """
    snake = app_name.replace("-", "_")
    return DeploymentContract(
        app_name=app_name,
        binary_name=app_name,
        description="",
        metrics_port=9090,
        health=HealthContract(),
        env_prefix=snake.upper(),
        metric_prefix=snake,
        config_mount_path=f"/etc/{app_name}/{app_name}.yaml",
    )


def _read_payload(path: Path) -> dict | None:
    """Read a written contract file (test helper that mirrors the CLI parse path).

    Tests can also use ``json.loads(path.read_text())`` directly; this
    helper exists so tests share the same parse path the CLI uses.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
