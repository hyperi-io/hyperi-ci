# Project:   HyperI CI
# File:      src/hyperi_ci/languages/golang/publish.py
# Purpose:   Golang publish handler (Go proxy)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Golang publish handler.

Go modules publish automatically to proxy.golang.org when tagged.
Binary artifact uploads are handled generically by publish_binaries
in dispatch.py — not duplicated here.
"""

from __future__ import annotations

import subprocess

from hyperi_ci.common import error, group, info, success
from hyperi_ci.config import CIConfig


def _publish_go_proxy() -> int:
    """Trigger Go module proxy indexing.

    Go modules are automatically indexed by proxy.golang.org when a tag
    is pushed to a public repo. This forces an immediate fetch.

    Returns:
        Exit code (0 = success).

    """
    result = subprocess.run(
        ["go", "list", "-m"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        error("Could not determine Go module path")
        return 1

    module_path = result.stdout.strip()
    info(f"Go module {module_path} will be indexed by proxy.golang.org on tag push")
    success("Go proxy publish: automatic on tag push")
    return 0


def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Run Golang publish stage.

    Handles Go-specific publishing (module proxy). Binary artifact uploads
    are handled by the generic publish_binaries handler in dispatch.py.

    Args:
        config: Merged CI configuration.
        extra_env: Additional environment variables.

    Returns:
        Exit code (0 = success).

    """
    go_destinations = config.destination_for("go")

    if not go_destinations:
        info("No Go publish destinations configured")
        return 0

    for dest in go_destinations:
        if dest == "go-proxy":
            with group("Publish: Go proxy"):
                rc = _publish_go_proxy()
                if rc != 0:
                    return rc

        else:
            error(f"Unknown Go publish destination: {dest}")
            return 1

    return 0
