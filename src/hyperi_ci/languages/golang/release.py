# Project:   HyperI CI
# File:      src/hyperi_ci/languages/golang/release.py
# Purpose:   Golang release handler (Go proxy)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Golang publish handler.

Go modules publish automatically to proxy.golang.org when tagged.
Binary artifact uploads are handled generically by publish_binaries
in dispatch.py -- not duplicated here.
"""

from pathlib import Path

from hyperi_ci.common import error, group, info, success
from hyperi_ci.config import CIConfig


def _module_path(root: Path) -> str | None:
    """Return the ``module`` path declared in ``go.mod``, or None.

    Read from the file: ``go list -m`` would fetch and run whatever toolchain
    go.mod names, with every publish credential in its environment.
    """
    go_mod = root / "go.mod"
    if not go_mod.is_file():
        return None
    for line in go_mod.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = line.split("//", 1)[0].split()
        if len(fields) == 2 and fields[0] == "module":
            return fields[1].strip('"`')
    return None


def _publish_go_proxy() -> int:
    """Report the module proxy.golang.org indexes when the tag is pushed.

    Returns:
        Exit code (0 = success).

    """
    module_path = _module_path(Path.cwd())
    if not module_path:
        error("Could not determine Go module path from go.mod")
        return 1

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
