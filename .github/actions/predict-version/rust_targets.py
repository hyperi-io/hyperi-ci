#!/usr/bin/env python3
# Project:   HyperI CI
# File:      .github/actions/predict-version/rust_targets.py
# Purpose:   Write the project's build.rust.targets to the step outputs
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Write ``rust-targets`` to ``$GITHUB_OUTPUT``, space-separated.

The Rust Plan builds its matrix from this, so no runner image needs ``yq``.
The reading lives in ``src/hyperi_ci/build_targets.py``, loaded out of the
action's own checkout the same by-path way ``resolve_tier.py`` loads its
implementation, because hyperi-ci is not installed in the caller's job.

Empty means every target. A list that cannot be read is also written empty,
which is what the matrix did before, but it raises a ``::warning::`` naming
the file and the reason rather than building every target in silence.
"""

# KEEP on a 3.14 floor, where this import is otherwise wrong (issue #184).
# GitHub runs this composite's scripts before any install, on whatever python3
# the runner has, which may predate our floor.
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

_PACKAGE = Path(__file__).resolve().parents[3] / "src" / "hyperi_ci"


def _write_output(targets: list[str]) -> None:
    line = f"rust-targets={' '.join(targets)}\n"
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
    else:
        sys.stdout.write(line)


def _load_package() -> None:
    # hyperi_ci/__init__ reads installed package metadata, which is absent here.
    if "hyperi_ci" not in sys.modules:
        package = types.ModuleType("hyperi_ci")
        package.__path__ = [str(_PACKAGE)]
        sys.modules["hyperi_ci"] = package


def run() -> int:
    """Read the targets and write the output. Never fails the step."""
    workspace = Path(os.environ.get("GITHUB_WORKSPACE") or ".")
    if not (workspace / "Cargo.toml").is_file():
        # Only rust-ci.yml reads this, so another language gets no warning.
        _write_output([])
        return 0
    try:
        _load_package()
        from hyperi_ci.build_targets import read_rust_targets

        targets, problem = read_rust_targets(workspace)
    except Exception as exc:
        _write_output([])
        print(
            f"::warning title=rust targets::could not read build.rust.targets ({exc}) -- every target builds"
        )
        return 0

    _write_output(targets)
    if problem:
        print(f"::warning title=rust targets::{problem} -- every target builds")
    elif targets:
        print(f"::notice title=rust targets::{' '.join(targets)}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
