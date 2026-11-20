#!/usr/bin/env python3
# Project:   HyperI CI
# File:      .github/actions/predict-version/rust_targets.py
# Purpose:   Write the project's build.rust.targets to the step outputs
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Write the Rust target outputs to ``$GITHUB_OUTPUT``.

``rust-targets`` is the declared list, space-separated, for the Linux matrix.
``os-build-matrix`` and ``os-test-include`` are JSON for the Windows and macOS
legs. The Rust Plan reads these, so no runner image needs ``yq``. The reading
lives in ``src/hyperi_ci/build_targets.py``, loaded out of the action's own
checkout the same by-path way ``resolve_tier.py`` loads its implementation,
because hyperi-ci is not installed in the caller's job.

Empty means every target. A list that cannot be read is also written empty,
which is what the matrix did before, but it raises a ``::warning::`` naming
the file and the reason rather than building every target in silence.
"""

# KEEP: runs on the runner's python3, which may predate the 3.14 floor (#184).
from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

_PACKAGE = Path(__file__).resolve().parents[3] / "src" / "hyperi_ci"


def _write_outputs(targets: list[str], legs: list[dict[str, str]]) -> None:
    runners = list(dict.fromkeys(leg["runner"] for leg in legs))
    lines = (
        f"rust-targets={' '.join(targets)}\n"
        f"os-build-matrix={json.dumps({'include': legs})}\n"
        f"os-test-include={json.dumps([{'os': runner} for runner in runners])}\n"
        f"has-os-legs={'true' if legs else 'false'}\n"
    )
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(lines)
    else:
        sys.stdout.write(lines)


def _load_package() -> None:
    # hyperi_ci/__init__ reads installed package metadata, which is absent here.
    if "hyperi_ci" not in sys.modules:
        package = types.ModuleType("hyperi_ci")
        package.__path__ = [str(_PACKAGE)]
        sys.modules["hyperi_ci"] = package


def run() -> int:
    """Read the targets and write the outputs. Never fails the step."""
    workspace = Path(os.environ.get("GITHUB_WORKSPACE") or ".") / os.environ.get(
        "PROJECT_SUBDIR", ""
    )
    if not (workspace / "Cargo.toml").is_file():
        # Only rust-ci.yml reads this, so another language gets no warning.
        _write_outputs([], [])
        return 0
    try:
        _load_package()
        from hyperi_ci.build_targets import (
            TARGET_OS_ARCH,
            os_legs,
            read_rust_targets,
            unbuilt_targets,
        )

        targets, problem = read_rust_targets(workspace)
        legs = os_legs(targets)
        unbuilt = unbuilt_targets(targets)
        built = " ".join(TARGET_OS_ARCH)
    except Exception as exc:
        _write_outputs([], [])
        print(
            f"::warning title=rust targets::could not read build.rust.targets ({exc}) -- every target builds"
        )
        return 0

    _write_outputs(targets, legs)
    if problem:
        print(f"::warning title=rust targets::{problem} -- every target builds")
    elif targets:
        print(f"::notice title=rust targets::{' '.join(targets)}")
    if unbuilt:
        print(
            f"::warning title=rust targets::no CI leg builds {' '.join(unbuilt)} -- "
            f"CI builds {built}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(run())
