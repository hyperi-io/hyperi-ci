# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/build_phase.py
# Purpose:   Split a PGO + BOLT release build into one CI job per phase
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Release build phases, one CI job each.

``HYPERCI_BUILD_PHASE`` picks the phase: ``pgo-profile`` (instrumented build and
workload), ``pgo-optimize`` (the profile-use compile) or ``bolt``. Unset runs
the whole pipeline in one job. Each phase stashes what the next one needs under
``.hyperi-ci/phase/<target>/``, which rust-ci.yml carries between jobs as an
artifact. Upload drops the executable bit, so restore sets it again.
"""

import json
import os
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

PHASE_ENV = "HYPERCI_BUILD_PHASE"
PROFILE = "pgo-profile"
OPTIMIZE = "pgo-optimize"
BOLT = "bolt"
PHASES = (PROFILE, OPTIMIZE, BOLT)

STASH_ROOT = Path(".hyperi-ci") / "phase"
_STATE = "state.json"
_PROFILES = "pgo-profiles"
_BINARIES = "bin"


class PhaseError(ValueError):
    """``HYPERCI_BUILD_PHASE`` names no phase, or a phase finds no stash."""


@dataclass(slots=True)
class PhaseState:
    """What a finished phase tells the next one.

    Attributes:
        done: The build finished, so later phases have nothing to do.
        mold: The instrumented build needed mold, so later links use it too.
        bolt: BOLT was planned when the profile was taken.
        pgo_applied: The profile-use compile ran.

    """

    done: bool = False
    mold: bool = False
    bolt: bool = False
    pgo_applied: bool = False


def build_phase(environ: Mapping[str, str] | None = None) -> str:
    """Return the phase this build runs, ``""`` for the whole pipeline.

    Raises:
        PhaseError: The variable names no phase.

    """
    env = os.environ if environ is None else environ
    phase = env.get(PHASE_ENV, "").strip()
    if phase and phase not in PHASES:
        raise PhaseError(
            f"{PHASE_ENV}={phase!r} is not a build phase. Use one of "
            f"{', '.join(PHASES)}, or leave it unset for the whole build."
        )
    return phase


def target_dir(cwd: Path) -> Path:
    """Return cargo's target directory, honouring ``CARGO_TARGET_DIR``."""
    path = Path(os.environ.get("CARGO_TARGET_DIR") or "target")
    return path if path.is_absolute() else cwd / path


def stash_dir(cwd: Path, target: str) -> Path:
    """Return the directory one target's phases hand over through."""
    return cwd / STASH_ROOT / target


def stash(
    cwd: Path, target: str, state: PhaseState, binaries: Sequence[str] = ()
) -> None:
    """Write the state, the PGO profiles and ``binaries`` for the next phase."""
    root = stash_dir(cwd, target)
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    profiles = target_dir(cwd) / _PROFILES
    if profiles.is_dir():
        shutil.copytree(profiles, root / _PROFILES)
    release = target_dir(cwd) / target / "release"
    for name in binaries:
        (root / _BINARIES).mkdir(exist_ok=True)
        shutil.copy2(release / name, root / _BINARIES / name)
    (root / _STATE).write_text(
        json.dumps(asdict(state), indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def read_state(cwd: Path, target: str) -> PhaseState:
    """Return the previous phase's state without restoring anything.

    Raises:
        PhaseError: No stash, or a state file this version cannot read.

    """
    state_file = stash_dir(cwd, target) / _STATE
    try:
        raw = json.loads(state_file.read_text(encoding="utf-8"))
        return PhaseState(**raw)
    except (OSError, ValueError, TypeError) as exc:
        raise PhaseError(
            f"no usable {state_file} from the previous build phase: {exc}. The "
            "job before this one must upload it and this job download it."
        ) from exc


def restore(cwd: Path, target: str) -> PhaseState:
    """Put the previous phase's profiles and binaries back where cargo leaves them.

    Raises:
        PhaseError: No stash, or a state file this version cannot read.

    """
    state = read_state(cwd, target)
    root = stash_dir(cwd, target)
    profiles = root / _PROFILES
    if profiles.is_dir():
        shutil.copytree(profiles, target_dir(cwd) / _PROFILES, dirs_exist_ok=True)
    binaries = root / _BINARIES
    if binaries.is_dir():
        release = target_dir(cwd) / target / "release"
        release.mkdir(parents=True, exist_ok=True)
        for binary in binaries.iterdir():
            placed = release / binary.name
            shutil.copy2(binary, placed)
            placed.chmod(0o755)
    return state
