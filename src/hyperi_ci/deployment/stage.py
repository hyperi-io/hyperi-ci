# Project:   HyperI CI
# File:      src/hyperi_ci/deployment/stage.py
# Purpose:   `generate` stage handler — three-tier producer dispatch
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""``generate`` CI stage — produces fresh deployment artefacts.

Sits between Build and Container in the pipeline. Auto-detects the
producer tier and dispatches:

  Tier 1 (RUST)   → subprocess `<app> generate-artefacts --output-dir <out>`
                    (binary built by the Build stage; rustlib 2.7+
                    provides the subcommand)
  Tier 2 (PYTHON) → subprocess `<app> generate-artefacts --output-dir <out>`
                    (entry point installed via uv; pylib 2.x provides
                    the subcommand)
  Tier 3 (OTHER)  → in-process call to ``hyperi_ci.deployment.cli.emit_artefacts``
                    (Tier 3 templater)
  None            → log + skip with success (no contract = nothing to
                    generate)

The Container stage then reads from ``ci-tmp/Dockerfile.runtime`` and
``ci-tmp/container-manifest.json`` rather than the repo's committed
``ci/`` so a stale commit can't poison a build.

Until rustlib 2.7+ and pylib 2.x ship their generators, Tier RUST and
Tier PYTHON paths return a clear "producer not yet shipped" error.
Tier 3 works end-to-end (its templater is similarly Phase-2-blocked,
but the dispatch and exit-code contract is already wired so adopters
can test the local flow against the stub).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from hyperi_ci.common import error, info, success
from hyperi_ci.deployment.cli import emit_artefacts
from hyperi_ci.deployment.detect import Tier, detect_tier

DEFAULT_OUTPUT_DIR = Path("ci-tmp")
DEFAULT_DRIFT_DIR = Path(".tmp/drift")

# Exit codes layered on top of `emit_artefacts`'s set. EXIT_PRODUCER_MISSING
# means the tier was detected but the producer isn't present yet (rustlib
# binary not built, pylib entry point not on PATH, etc.) — distinct from
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
) -> int:
    """Run the generate stage.

    Args:
        output_dir: Where artefacts are written. Defaults to
            :data:`DEFAULT_OUTPUT_DIR` (``ci-tmp/``). Created if missing.
        project_dir: Project root. Defaults to cwd. Used for tier
            auto-detection only.
        contract_path: For Tier 3, override the contract source.
            Ignored for Tier 1/2 (those producers find their own
            contract from the app's source).

    Returns:
        Exit code (0 on success / skip; non-zero on failure).

    """
    project_dir = project_dir or Path.cwd()
    output_dir = output_dir or DEFAULT_OUTPUT_DIR

    tier = detect_tier(project_dir)
    info(f"Generate: detected tier '{tier.value}'")

    if tier == Tier.NONE:
        info("Generate: no deployment contract present — skipping")
        return EXIT_OK

    output_dir.mkdir(parents=True, exist_ok=True)

    if tier == Tier.OTHER:
        return _run_tier3(output_dir, contract_path)

    if tier == Tier.RUST:
        return _run_tier1(output_dir, project_dir)

    if tier == Tier.PYTHON:
        return _run_tier2(output_dir, project_dir)

    # Defensive — Tier enum is exhaustive but a compatibility break
    # in detect_tier (e.g. a future tier added) shouldn't crash here.
    error(f"Generate: unrecognised tier '{tier.value}'")
    return EXIT_TIER_NOT_YET_IMPLEMENTED


def check_drift(
    *,
    project_dir: Path | None = None,
    committed_dir: Path | None = None,
    drift_dir: Path | None = None,
    contract_path: Path | None = None,
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

    Returns:
        ``EXIT_OK`` when the regenerated output matches the committed
        directory byte-for-byte; non-zero on producer failure or drift.

    """
    project_dir = project_dir or Path.cwd()
    committed = committed_dir or (project_dir / "ci")
    drift = drift_dir or DEFAULT_DRIFT_DIR

    # Ensure a clean drift dir — any leftovers from previous runs would
    # confuse the diff.
    if drift.exists():
        shutil.rmtree(drift)

    rc = run(
        output_dir=drift,
        project_dir=project_dir,
        contract_path=contract_path,
    )
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
    """Tier 1 (Rust + rustlib): subprocess into the app binary.

    The binary is expected at one of:
      - ``dist/<bin>-linux-amd64`` (post-Build artifact in CI)
      - ``target/release/<bin>`` (cargo build --release output)
      - ``target/debug/<bin>`` (cargo build output)

    Falls back through that order. Errors with EXIT_PRODUCER_MISSING if
    none exist — the caller is expected to run Build (CI) or
    ``cargo build`` (local) first.

    Until hyperi-rustlib 2.7+ ships and an app actually adopts the
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
    """Tier 2 (Python + pylib): subprocess into the app entry point.

    Looks up the entry point name from ``pyproject.toml``'s
    ``[project.scripts]`` table — the binary that pylib's
    ``Application.deployment_contract()`` emits artefacts from. If
    multiple scripts are declared, the first one wins.

    Until hyperi-pylib ships its mirror of the rustlib deployment
    module (parallel work; not yet started), even an installed entry
    point will fail with no ``generate-artefacts`` subcommand. That
    presents as EXIT_PRODUCER_FAILED.
    """
    script_name = _resolve_python_entry_point(project_dir)
    if script_name is None:
        error(
            "Generate (Tier 2): no [project.scripts] entry point found in "
            f"{project_dir}/pyproject.toml — pylib's generate-artefacts "
            "subcommand needs an installed CLI entry."
        )
        return EXIT_PRODUCER_MISSING

    binary = shutil.which(script_name)
    if binary is None:
        error(f"Generate (Tier 2): {script_name!r} not on PATH.")
        info(
            f"Run `uv sync --project {project_dir}` "
            f"(or `pip install -e {project_dir}`) first."
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
        # (rustlib < 2.7, pylib < 2.x). Hint at that for actionability.
        if "generate-artefacts" in (result.stderr or ""):
            info(
                "If this is a 'no such subcommand' error, the app "
                "binary is built against an older library that doesn't "
                "implement generate-artefacts yet. Update the app to "
                "rustlib 2.7+ / pylib 2.x."
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
    bin_name = _rust_binary_name(project_dir)
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


def _rust_binary_name(project_dir: Path) -> str | None:
    """Best-effort Rust binary-name extraction from Cargo.toml.

    Selection order:

    1. The `[package].name` itself if a `[[bin]]` of the same name
       exists. This is the cargo convention for the "main" binary —
       a project named ``dfe-receiver`` with multiple `[[bin]]` blocks
       (e.g. ``pgo-driver`` for instrumentation, ``dfe-receiver`` for
       the app) wants the package-name-matching one to be the
       generate-artefacts producer.
    2. The `[package].name` even without an explicit `[[bin]]` block
       — covers the implicit-bin case.
    3. The FIRST `[[bin]]` block, in declaration order. Last-resort
       fallback for projects that diverge from cargo conventions.
    4. Workspace fallback — if the root Cargo.toml is `[workspace]`-only
       (no `[package]`), resolve each `members = [...]` entry and apply
       the same selection order. We pick the FIRST member that yields
       a binary; tying members named like the workspace directory wins.

    Substring-based parse — same approach as
    :func:`hyperi_ci.deployment.detect._depends_on`. Tier dispatch only
    needs a name to invoke, not a full Cargo manifest parse.
    """
    cargo_toml = project_dir / "Cargo.toml"
    if not cargo_toml.is_file():
        return None
    try:
        text = cargo_toml.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    package_name = _extract_package_name(text)
    bin_names = _extract_bin_names(text)

    # 1. package.name + matching [[bin]] (cargo convention)
    if package_name and package_name in bin_names:
        return package_name
    # 2. package.name (implicit bin)
    if package_name:
        return package_name
    # 3. first [[bin]] (last-resort)
    if bin_names:
        return bin_names[0]

    # 4. Workspace-only root: probe members in declaration order, prefer
    #    a member whose path-leaf matches the workspace directory name
    #    (e.g. workspace `dfe-archiver` with member `crates/archiver`).
    members = _extract_workspace_members(text)
    workspace_name = project_dir.name
    ranked: list[tuple[int, str]] = []
    for relpath in members:
        member_dir = project_dir / relpath
        leaf = member_dir.name  # e.g. "archiver" for "crates/archiver"
        # Prefer leaves that match the workspace directory loosely
        # ("dfe-archiver" → "archiver"). Fall back to declaration order.
        rank = 0 if leaf in workspace_name or workspace_name in leaf else 1
        candidate = _rust_binary_name(member_dir)
        if candidate:
            ranked.append((rank, candidate))
    if ranked:
        ranked.sort(key=lambda r: r[0])
        return ranked[0][1]
    return None


def _extract_workspace_members(text: str) -> list[str]:
    """Extract `members = [...]` entries from a `[workspace]` table.

    Substring-based parse: handles single-line and multi-line array
    forms. Returns relative paths with no quoting.
    """
    in_workspace = False
    in_members = False
    collected: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped == "[workspace]":
            in_workspace = True
            continue
        if in_workspace and stripped.startswith("[") and stripped != "[workspace]":
            in_workspace = False
            in_members = False
            continue
        if not in_workspace:
            continue
        if stripped.startswith("members"):
            # Could be `members = ["a", "b"]` or `members = [` (multi-line)
            if "[" in stripped and "]" in stripped:
                # Single-line form
                inner = stripped.split("[", 1)[1].rsplit("]", 1)[0]
                collected.extend(_split_members(inner))
                in_members = False
            elif "[" in stripped:
                in_members = True
            continue
        if in_members:
            if "]" in stripped:
                inner = stripped.split("]", 1)[0]
                collected.extend(_split_members(inner))
                in_members = False
            else:
                collected.extend(_split_members(stripped))
    return collected


def _split_members(text: str) -> list[str]:
    """Parse comma-separated quoted member paths from a `members` slice."""
    parts: list[str] = []
    for token in text.split(","):
        cleaned = token.strip().strip(",").strip('"').strip("'").strip()
        if cleaned:
            parts.append(cleaned)
    return parts


def _extract_package_name(text: str) -> str | None:
    """Extract `[package].name` from Cargo.toml text."""
    in_package = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "[package]":
            in_package = True
            continue
        if in_package and stripped.startswith("name"):
            value = stripped.split("=", 1)[1].strip().strip('"').strip("'")
            return value or None
        if stripped.startswith("[") and stripped != "[package]":
            in_package = False
    return None


def _extract_bin_names(text: str) -> list[str]:
    """Extract every `[[bin]].name` from Cargo.toml text, in order."""
    names: list[str] = []
    in_bin = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "[[bin]]":
            in_bin = True
            continue
        if in_bin and stripped.startswith("name"):
            value = stripped.split("=", 1)[1].strip().strip('"').strip("'")
            if value:
                names.append(value)
            in_bin = False
            continue
        if stripped.startswith("[") and stripped != "[[bin]]":
            in_bin = False
    return names


def _resolve_python_entry_point(project_dir: Path) -> str | None:
    """Read the first ``[project.scripts]`` entry from pyproject.toml.

    Only used to find the binary name; ``shutil.which`` resolves it
    against PATH.
    """
    pyproject = project_dir / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        text = pyproject.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    in_scripts = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "[project.scripts]":
            in_scripts = True
            continue
        if in_scripts:
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("["):
                return None
            if "=" in stripped:
                name = stripped.split("=", 1)[0].strip()
                # Strip quotes (TOML allows quoted keys for names with dashes).
                return name.strip('"').strip("'")
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
