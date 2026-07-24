# Project:   HyperI CI
# File:      src/hyperi_ci/deployment/stage.py
# Purpose:   `generate` stage handler — three-tier producer dispatch
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""``generate`` CI stage — produces fresh deployment artefacts.

Sits between Build and Container in the pipeline. Auto-detects the
producer tier and dispatches:

  Tier 1 (RUST)   → subprocess `<app> generate-artefacts --output-dir <out>`
                    (binary built by the Build stage; scalo 2.7+
                    provides the subcommand)
  Tier 2 (PYTHON) → subprocess `<app> generate-artefacts --output-dir <out>`
                    (entry point installed via uv; scalo 2.x provides
                    the subcommand)
  Tier 3 (OTHER)  → in-process call to ``hyperi_ci.deployment.cli.emit_artefacts``
                    (Tier 3 templater)
  None            → log + skip with success (no contract = nothing to
                    generate)

The Container stage then reads from ``ci-tmp/Dockerfile.runtime`` and
``ci-tmp/container-manifest.json`` rather than the repo's committed
``ci/`` so a stale commit can't poison a build.

``deployment.producer`` in the config cascade overrides the tier
auto-detection — ``false`` skips the stage outright (the escape hatch
for a scalo library consumer that ships its own Dockerfile), ``true``
forces dispatch on the marker dep alone.

Until the scalo crate (2.7+) and package (2.x) ship their generators, Tier RUST and
Tier PYTHON paths return a clear "producer not yet shipped" error.
Tier 3 works end-to-end (its templater is similarly Phase-2-blocked,
but the dispatch and exit-code contract is already wired so adopters
can test the local flow against the stub).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from hyperi_ci.common import error, info, normalise_tristate, success
from hyperi_ci.config import CIConfig, load_config
from hyperi_ci.deployment.cli import emit_artefacts
from hyperi_ci.deployment.detect import Tier, resolve_tier
from hyperi_ci.deployment.manifest import python_entry_point, rust_binary_name

DEFAULT_OUTPUT_DIR = Path("ci-tmp")
DEFAULT_DRIFT_DIR = Path(".tmp/drift")

# Cascade key gating the whole stage. Tri-state, same shape as
# `publish.container.enabled` — see :func:`run`.
PRODUCER_KEY = "deployment.producer"

# Exit codes layered on top of `emit_artefacts`'s set. EXIT_PRODUCER_MISSING
# means the tier was detected but the producer isn't present yet (scalo
# binary not built, scalo entry point not on PATH, etc.) — distinct from
# EXIT_CONTRACT_MISSING (= 2 from emit_artefacts) which means the JSON
# contract file isn't there.
EXIT_OK = 0
EXIT_PRODUCER_MISSING = 7
EXIT_PRODUCER_FAILED = 8
EXIT_TIER_NOT_YET_IMPLEMENTED = 9


def run(
    output_dir: Path | None = None,
    *,
    project_dir: Path | None = None,
    contract_path: Path | None = None,
    config: CIConfig | None = None,
) -> int:
    """Run the generate stage.

    The ``deployment.producer`` cascade key gates the whole stage. Most
    repos never set it — with no marker dep and no committed contract
    they resolve to Tier NONE and skip, same as they always have.

    * ``auto`` (default) — dispatch on the detected tier. A repo that
      carries a Tier 1/2 marker dep but produces no binary / console
      script is a library consumer, not a producer, and skips.
    * ``false`` — skip outright. The escape hatch for a library
      consumer whose shape DOES look like a producer (it has a CLI of
      its own) but which ships deployment artefacts by hand.
    * ``true`` — force. The marker dep alone selects the tier, for a
      genuine producer auto-detection can't see. Hard-fails when no
      tier resolves at all, rather than silently doing nothing.

    Args:
        output_dir: Where artefacts are written. Defaults to
            :data:`DEFAULT_OUTPUT_DIR` (``ci-tmp/``). Created if missing.
        project_dir: Project root. Defaults to cwd. Used for tier
            auto-detection only.
        contract_path: For Tier 3, override the contract source.
            Ignored for Tier 1/2 (those producers find their own
            contract from the app's source).
        config: Merged CI configuration. Loaded from the cascade
            rooted at ``project_dir`` when not supplied, so a direct
            ``hyperi-ci run generate`` honours ``.hyperi-ci.yaml`` too.

    Returns:
        Exit code (0 on success / skip; non-zero on failure).

    """
    project_dir = project_dir or Path.cwd()
    output_dir = output_dir or DEFAULT_OUTPUT_DIR

    tier, rc = _resolve_producer(project_dir, config)
    if tier is None:
        return rc

    return _dispatch_tier(tier, output_dir, project_dir, contract_path)


def _dispatch_tier(
    tier: Tier,
    output_dir: Path,
    project_dir: Path,
    contract_path: Path | None,
) -> int:
    """Run the producer for an already-resolved tier."""
    output_dir.mkdir(parents=True, exist_ok=True)

    if tier == Tier.OTHER:
        return _run_tier3(output_dir, contract_path)

    if tier == Tier.RUST:
        return _run_tier1(output_dir, project_dir)

    if tier == Tier.PYTHON:
        return _run_tier2(output_dir, project_dir)

    # Defensive — Tier enum is exhaustive but a compatibility break
    # in resolve_tier (e.g. a future tier added) shouldn't crash here.
    error(f"Generate: unrecognised tier '{tier.value}'")
    return EXIT_TIER_NOT_YET_IMPLEMENTED


def _resolve_producer(
    project_dir: Path,
    config: CIConfig | None,
) -> tuple[Tier | None, int]:
    """Apply the ``deployment.producer`` gate and resolve the tier.

    Returns ``(tier, exit_code)``. A ``None`` tier means "don't
    dispatch" and the exit code is the caller's return value —
    ``EXIT_OK`` for a legitimate skip, ``EXIT_PRODUCER_MISSING`` when
    the operator forced a producer that doesn't resolve.

    Shared by :func:`run` and :func:`check_drift` so the drift check
    can't mistake "this repo generates nothing" for "the committed
    artefacts drifted".
    """
    if config is None:
        # reload=True because load_config caches globally and ignores
        # project_dir on a warm cache — without it, `run(project_dir=X)`
        # silently answers from whatever project loaded first. The CI
        # path passes config explicitly and never lands here.
        config = load_config(project_dir=project_dir, reload=True)
    producer = normalise_tristate(config.get(PRODUCER_KEY, "auto"), key=PRODUCER_KEY)

    if producer == "false":
        info(f"Generate: {PRODUCER_KEY}: false — skipping")
        return None, EXIT_OK

    decision = resolve_tier(project_dir, require_producer=producer != "true")
    info(f"Generate: detected tier '{decision.tier.value}' — {decision.reason}")

    if decision.tier != Tier.NONE:
        return decision.tier, EXIT_OK

    if producer == "true":
        error(
            f"Generate: {PRODUCER_KEY}: true but no producer tier resolved "
            f"— {decision.reason}"
        )
        info(
            "A forced producer still needs a scalo dep in Cargo.toml / "
            "pyproject.toml, or a committed ci/deployment-contract.json."
        )
        return None, EXIT_PRODUCER_MISSING

    info(f"Generate: skipping — {decision.reason}")
    if decision.demoted:
        info(
            f"Set `{PRODUCER_KEY}: true` if this repo really does emit "
            "deployment artefacts."
        )
    return None, EXIT_OK


def check_drift(
    *,
    project_dir: Path | None = None,
    committed_dir: Path | None = None,
    drift_dir: Path | None = None,
    contract_path: Path | None = None,
    config: CIConfig | None = None,
) -> int:
    """Run the producer to a temp dir and compare against the committed ``ci/``.

    Used by the Quality stage. Fails (non-zero) when the regenerated
    output differs from what the repo committed — that's a signal the
    operator edited the contract without re-running ``generate-artefacts``,
    or the producer's output drifted from the contract.

    Args:
        project_dir: Project root. Defaults to cwd.
        committed_dir: Directory containing the committed artefacts.
            Defaults to ``<project_dir>/ci``.
        drift_dir: Where to regenerate to. Defaults to
            :data:`DEFAULT_DRIFT_DIR`.
        contract_path: Override for Tier 3 contract source.
        config: Merged CI configuration, passed through to :func:`run`
            so ``deployment.producer: false`` disables the drift check
            along with the stage it checks.

    Returns:
        ``EXIT_OK`` when the regenerated output matches the committed
        directory byte-for-byte; non-zero on producer failure or drift.

    """
    project_dir = project_dir or Path.cwd()
    committed = committed_dir or (project_dir / "ci")
    drift = drift_dir or DEFAULT_DRIFT_DIR

    # Resolve the gate FIRST. A repo that generates nothing has no
    # drift to check — regenerating into an empty dir and diffing it
    # against a committed ci/ would report every file as missing.
    # Dispatching the resolved tier directly (rather than calling run())
    # also keeps the gate from being resolved and logged twice.
    tier, rc = _resolve_producer(project_dir, config)
    if tier is None:
        return rc

    # Ensure a clean drift dir — any leftovers from previous runs would
    # confuse the diff.
    if drift.exists():
        shutil.rmtree(drift)

    rc = _dispatch_tier(tier, drift, project_dir, contract_path)
    if rc != EXIT_OK:
        return rc

    if not committed.is_dir():
        # No committed ci/ to compare against — the operator hasn't run
        # generate-artefacts yet, so the drift check has nothing to do.
        # This is distinct from "drift detected" (a real problem) — log
        # at info, return success.
        info(
            f"Drift check: no committed {committed} directory — "
            "nothing to compare against. Run "
            "`hyperi-ci emit-artefacts ci/` and commit the result."
        )
        return EXIT_OK

    if _dirs_byte_identical(drift, committed):
        success("Drift check: committed artefacts match contract")
        return EXIT_OK

    error(f"Drift check: artefacts under {committed}/ drift from the contract.")
    info(
        "Re-run the producer (`<app> generate-artefacts --output-dir ci/` "
        "for Tier 1/2, `hyperi-ci emit-artefacts ci/` for Tier 3) and "
        "commit the result."
    )
    return EXIT_PRODUCER_FAILED


def _run_tier1(output_dir: Path, project_dir: Path) -> int:
    """Tier 1 (Rust + scalo): subprocess into the app binary.

    The binary is expected at one of:
      - ``dist/<bin>-linux-amd64`` (post-Build artifact in CI)
      - ``target/release/<bin>`` (cargo build --release output)
      - ``target/debug/<bin>`` (cargo build output)

    Falls back through that order. Errors with EXIT_PRODUCER_MISSING if
    none exist — the caller is expected to run Build (CI) or
    ``cargo build`` (local) first.

    Until scalo 2.7+ ships and an app actually adopts the
    `cli-service,deployment` features, even a built binary will fail
    with "unknown subcommand 'generate-artefacts'". That's an
    EXIT_PRODUCER_FAILED case, distinguished by exit code from the
    binary-missing case.
    """
    binary = _resolve_rust_binary(project_dir)
    if binary is None:
        error("Generate (Tier 1): no Rust binary found.")
        info(
            "Looked for dist/<bin>-linux-amd64, target/release/<bin>, "
            "and target/debug/<bin>. Run the Build stage (CI) or "
            "`cargo build --release` (local) first."
        )
        return EXIT_PRODUCER_MISSING

    info(f"Generate (Tier 1): running {binary} generate-artefacts")
    cmd = [str(binary), "generate-artefacts", "--output-dir", str(output_dir)]
    return _run_producer_subprocess(cmd, "Rust")


def _run_tier2(output_dir: Path, project_dir: Path) -> int:
    """Tier 2 (Python + scalo): subprocess into the app entry point.

    Looks up the entry point name from ``pyproject.toml``'s
    ``[project.scripts]`` table — the binary that scalo's
    ``Application.deployment_contract()`` emits artefacts from. If
    multiple scripts are declared, the first one wins.

    The entry point is installed into the project's uv-managed virtualenv
    (via ``uv sync``), not the global ``PATH`` — so a bare ``PATH`` lookup
    fails under ``uvx hyperi-ci run generate`` in CI, where the venv is
    never activated. When ``uv`` is available we invoke through
    ``uv run``, which resolves the script in the project environment
    regardless of activation and respects ``UV_PROJECT_ENVIRONMENT``.
    ``--frozen`` keeps the lockfile untouched (matching the workflow's
    ``uv sync --frozen``, so generate can't mutate the lock). Falls back
    to a ``PATH`` lookup when ``uv`` is absent but the script is installed
    globally.

    Until the scalo Python package ships its mirror of the scalo Rust crate's deployment
    module (parallel work; not yet started), even an installed entry
    point will fail with no ``generate-artefacts`` subcommand. That
    presents as EXIT_PRODUCER_FAILED.
    """
    script_name = python_entry_point(project_dir)
    if script_name is None:
        # Only reachable under `deployment.producer: true` — auto
        # detection demotes a scriptless repo to Tier NONE before it
        # gets here (issue #76).
        error(
            "Generate (Tier 2): no [project.scripts] entry point found in "
            f"{project_dir}/pyproject.toml — scalo's generate-artefacts "
            "subcommand needs an installed CLI entry."
        )
        info(
            f"If this repo only USES scalo as a library, drop the "
            f"`{PRODUCER_KEY}: true` override and it will skip cleanly."
        )
        return EXIT_PRODUCER_MISSING

    uv = shutil.which("uv")
    if uv is not None:
        info(f"Generate (Tier 2): running uv run {script_name} generate-artefacts")
        cmd = [
            uv,
            "run",
            "--frozen",
            "--project",
            str(project_dir),
            script_name,
            "generate-artefacts",
            "--output-dir",
            str(output_dir),
        ]
        return _run_producer_subprocess(cmd, "Python")

    binary = shutil.which(script_name)
    if binary is None:
        error(
            f"Generate (Tier 2): {script_name!r} not resolvable — "
            "uv not on PATH and no globally installed script."
        )
        info(
            f"Install uv (recommended), or run `uv sync --project {project_dir}` "
            f"(or `pip install -e {project_dir}`) so {script_name!r} resolves."
        )
        return EXIT_PRODUCER_MISSING

    info(f"Generate (Tier 2): running {binary} generate-artefacts")
    cmd = [binary, "generate-artefacts", "--output-dir", str(output_dir)]
    return _run_producer_subprocess(cmd, "Python")


def _run_tier3(
    output_dir: Path,
    contract_path: Path | None,
) -> int:
    """Tier 3 (other): in-process call into the hyperi-ci templater.

    Maps :func:`emit_artefacts`'s exit codes onto our extended set:
      - EXIT_NOT_IMPLEMENTED (5) is propagated verbatim — Phase 2 not
        yet shipped; the message at the call site is already actionable.
      - All other exit codes are similarly propagated; the spec's
        "exits non-zero if" table covers the meanings.
    """
    info("Generate (Tier 3): templating from contract")
    return emit_artefacts(output_dir, contract_path)


def _run_producer_subprocess(cmd: list[str], tier_label: str) -> int:
    """Invoke a producer binary and translate its exit code."""
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        error(f"Generate ({tier_label}): producer not executable — {exc}")
        return EXIT_PRODUCER_MISSING

    if result.stdout:
        for line in result.stdout.splitlines():
            info(f"  {line}")
    if result.stderr:
        for line in result.stderr.splitlines():
            info(f"  {line}")

    if result.returncode != 0:
        error(f"Generate ({tier_label}): producer exited with code {result.returncode}")
        # Common: the binary doesn't have generate-artefacts yet
        # (scalo crate < 2.7, package < 2.x). Hint at that for actionability.
        if "generate-artefacts" in (result.stderr or ""):
            info(
                "If this is a 'no such subcommand' error, the app "
                "binary is built against an older library that doesn't "
                "implement generate-artefacts yet. Update the app to "
                "scalo crate 2.7+ / package 2.x."
            )
        return EXIT_PRODUCER_FAILED

    success(f"Generate ({tier_label}): producer succeeded")
    return EXIT_OK


def _resolve_rust_binary(project_dir: Path) -> Path | None:
    """Find a built Rust binary to invoke for Tier 1.

    Reads the binary name from Cargo.toml ``[[bin]]`` tables, falls
    back to the package name.

    Lookup order:
      1. ``dist/<bin>-linux-<host-arch>`` — preferred when the Build
         stage just produced an arch-specific artefact for this runner.
      2. ``dist/<bin>-linux-*`` glob — fallback when the host arch
         doesn't match a dist/ binary (e.g. cross-compiled on amd64
         host, dist/ contains arm64 only). Used so a cross-compile
         producer can still emit a manifest from any arch.
      3. ``target/release/<bin>`` then ``target/debug/<bin>`` — local
         dev builds.
    """
    bin_name = rust_binary_name(project_dir)
    if bin_name is None:
        return None

    host_arch = _host_linux_arch()
    dist_dir = project_dir / "dist"

    if host_arch:
        host_specific = dist_dir / f"{bin_name}-linux-{host_arch}"
        if host_specific.is_file():
            return host_specific

    if dist_dir.is_dir():
        # Glob fallback — pick any linux-* binary. The generate-artefacts
        # subcommand emits the same manifest regardless of which binary
        # arch is invoked, so any matching binary works.
        for candidate in sorted(dist_dir.glob(f"{bin_name}-linux-*")):
            if candidate.is_file():
                return candidate

    for candidate in (
        project_dir / "target" / "release" / bin_name,
        project_dir / "target" / "debug" / bin_name,
    ):
        if candidate.is_file():
            return candidate
    return None


def _host_linux_arch() -> str | None:
    """Map the host machine to the Linux dist/ arch suffix.

    Returns ``"amd64"`` on x86_64, ``"arm64"`` on aarch64, ``None``
    otherwise (so the caller falls back to the glob).
    """
    import platform

    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "amd64"
    if machine in {"aarch64", "arm64"}:
        return "arm64"
    return None


def _dirs_byte_identical(left: Path, right: Path) -> bool:
    """Compare two directories file-for-file, byte-for-byte.

    Both directories must contain the same set of files (same relative
    paths) and each file's bytes must match. Used by the drift check
    which needs strict equality, not "logically equivalent" YAML/JSON.

    This is the cheap path. A future optimisation could short-circuit
    on file size mismatch before reading bytes; not worth the
    complexity at the artefact volumes we're emitting (tens of files,
    a few KB each).
    """
    left_files = _relative_files(left)
    right_files = _relative_files(right)
    if left_files != right_files:
        return False
    for relpath in left_files:
        a = (left / relpath).read_bytes()
        b = (right / relpath).read_bytes()
        if a != b:
            return False
    return True


def _relative_files(root: Path) -> set[Path]:
    """Return the set of file paths under ``root`` relative to ``root``."""
    if not root.is_dir():
        return set()
    return {p.relative_to(root) for p in root.rglob("*") if p.is_file()}
