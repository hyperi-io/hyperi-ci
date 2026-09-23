# Project:   HyperI CI
# File:      src/hyperi_ci/quality/lint_compose.py
# Purpose:   Orchestrate the docker-compose linting dimension (Path C)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Orchestrate docker-compose linting for a compose packaging repo (Path C).

This is what the ``hyperi-ci lint-compose <dir>`` verb runs. It is the entry
point for a repo whose deliverable IS the compose stack: it has no language
pipeline for ``stage_quality`` to hang hadolint off, and it ships no Helm chart,
no ``apiVersion``+``kind`` manifest and no terraform for ``lint-manifests`` to
discover - so both existing container paths reach past it.

Pipeline:

1. discover every compose file the ``docker-compose`` dependency surface claims
   that holds a top-level ``services`` mapping;
2. **compose-config** - ``docker compose config`` resolution GATE over each
   standalone stack, with placeholders injected for the keys the file declares
   mandatory;
3. **compose-pins** - image-pin GATE over every file, fragments included, run
   statically so it holds with no docker installed.

Both gate. They are complementary rather than redundant: compose aborts on the
FIRST unset mandatory key, so a resolution pass says nothing about the pins
behind it, and a pin pass says nothing about whether the file merges.
"""

from __future__ import annotations

from pathlib import Path

from hyperi_ci.common import get_exclude_dirs, group, info
from hyperi_ci.config import CIConfig
from hyperi_ci.quality import compose_config, compose_pins
from hyperi_ci.quality.targets import discover_compose_files


def run(
    root: Path | str, config: CIConfig, *, sarif_path: str | Path | None = None
) -> int:
    """Lint every docker-compose file under ``root``.

    Returns non-zero when either GATE fails: a file that does not resolve, or an
    image that resolves to ``latest``. Both tools always RUN, so a resolution
    failure still surfaces the pin findings alongside it.
    """
    root = Path(root)
    files = discover_compose_files(root, exclude_dirs=get_exclude_dirs(config._raw))
    if not files:
        info(f"lint-compose: no docker-compose files under {root} - skipping")
        return 0

    info(f"lint-compose: {len(files)} compose file(s) under {root}")

    with group("docker compose config resolution (gate)"):
        config_rc = compose_config.run(files, config, sarif_path=sarif_path)

    with group("compose image pins (gate)"):
        pins_rc = compose_pins.run(files, config, sarif_path=sarif_path)

    return config_rc or pins_rc
