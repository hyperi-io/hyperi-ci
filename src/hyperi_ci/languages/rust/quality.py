# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/quality.py
# Purpose:   Rust quality checks (fmt, clippy, audit, deny)
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Rust quality checks handler.

Orchestrates: cargo fmt --check, cargo clippy, cargo audit, cargo deny.
Each tool's mode (blocking/warn/disabled) is configurable via
.hyperi-ci.yaml quality.rust section.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from hyperi_ci.common import error, info, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import get_test_ignore
from hyperi_ci.quality import osv_scanner
from hyperi_ci.quality.ignores import IgnoreEntry, for_tool, load_ignores

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover — Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]  # ty: ignore[unresolved-import]

_DEFAULT_RUST_TEST_IGNORE = [
    "clippy::unwrap_used",
    "clippy::expect_used",
    "clippy::panic",
    "clippy::indexing_slicing",
]


def _deny_toml_advisory_ignores(project_dir: Path | None = None) -> list[str]:
    """Return the RUSTSEC IDs ignored in ``deny.toml`` ``[advisories.ignore]``.

    ``cargo deny`` reads its ignore list from ``deny.toml`` while
    ``cargo audit`` / ``osv-scanner`` read the central ``quality.ignore``
    (issue #42). An advisory acceptable to one was NOT honoured by the
    others, so a repo could pass ``cargo deny`` on push yet fail
    ``osv-scanner`` on the same ID at release. We make ``deny.toml`` a
    shared source: its advisory ignores are fed into the other two tools
    as well, so one entry silences all three.

    Handles both entry forms cargo-deny accepts::

        [advisories]
        ignore = [
            "RUSTSEC-2024-0436",
            { id = "RUSTSEC-2021-0127", reason = "..." },
        ]

    Non-RUSTSEC/CVE entries (e.g. licence exceptions, crate bans) are not
    advisory IDs and are left alone. Returns an empty list when there is
    no ``deny.toml`` or no advisory ignores.
    """
    cwd = project_dir or Path.cwd()
    deny_toml = cwd / "deny.toml"
    if not deny_toml.exists():
        return []
    try:
        manifest = tomllib.loads(deny_toml.read_text())
    except Exception as exc:  # malformed deny.toml — cargo deny will report it
        warn(f"  cargo deny: could not parse deny.toml for shared ignores: {exc}")
        return []

    raw = (manifest.get("advisories") or {}).get("ignore") or []
    if not isinstance(raw, list):
        return []

    ids: list[str] = []
    for entry in raw:
        if isinstance(entry, str):
            ident = entry.strip()
        elif isinstance(entry, dict) and entry.get("id"):
            ident = str(entry["id"]).strip()
        else:
            continue
        # Advisory IDs only — cargo-deny's ignore list can also hold crate
        # names / licence IDs which mean nothing to cargo-audit / osv.
        if ident.startswith(("RUSTSEC-", "CVE-", "GHSA-")):
            ids.append(ident)
    return ids


def _merge_deny_advisory_ignores(
    entries: list[IgnoreEntry], tool: str, deny_ids: list[str]
) -> list[IgnoreEntry]:
    """Union ``deny.toml`` advisory IDs into ``tool``'s ignore entries.

    De-dupes against IDs already present for ``tool`` in ``quality.ignore``
    so the dfe-loader-style duplicate entry becomes unnecessary (issue #42).
    """
    existing = {e.id for e in entries}
    merged = list(entries)
    for ident in deny_ids:
        if ident not in existing:
            merged.append(
                IgnoreEntry(
                    tool=tool,
                    id=ident,
                    reason="shared from deny.toml [advisories.ignore] (issue #42)",
                )
            )
            existing.add(ident)
    return merged


def _has_lib_target(project_dir: Path | None = None) -> bool:
    """Return True if the project (or workspace) exposes any lib target.

    ``cargo clippy --lib`` and ``cargo check --lib`` fail with ``no
    library targets found`` on bin-only projects. We only pass ``--lib``
    when at least one ``lib`` target is present so binary-only projects
    (like ci-test-rust-app or any `cargo new --bin` crate) don't
    spuriously fail the quality stage.

    Authoritative answer comes from ``cargo metadata`` when available;
    a Cargo.toml + filesystem fallback covers the case where cargo is
    on PATH but metadata fails for some reason.
    """
    cwd = project_dir or Path.cwd()
    if not (cwd / "Cargo.toml").exists():
        return False

    try:
        result = subprocess.run(
            ["cargo", "metadata", "--no-deps", "--format-version=1"],
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        rc = getattr(result, "returncode", 1)
    except (FileNotFoundError, OSError):
        rc = 1
        result = None  # type: ignore[assignment]

    if rc == 0:
        import json as _json

        try:
            metadata = _json.loads(getattr(result, "stdout", "") or "")
        except _json.JSONDecodeError:
            metadata = {}
        for package in metadata.get("packages", []):
            for target in package.get("targets", []):
                if "lib" in target.get("kind", []) or "rlib" in target.get("kind", []):
                    return True
        return False

    # Fallback: cargo metadata failed (e.g. cargo missing). Treat
    # presence of src/lib.rs as a proxy. Workspace members aren't
    # explored — this is the conservative path.
    return (cwd / "src" / "lib.rs").exists()


def _split_feature_sets(features: str) -> list[str]:
    """Split pipe-separated feature sets into individual sets.

    Each set is run as a separate invocation to properly test mutually
    exclusive features (e.g. jemalloc vs mimalloc).
    """
    if features in ("all", "default"):
        return [features]
    return [f.strip() for f in features.split("|") if f.strip()]


def _get_tool_mode(tool: str, config: CIConfig) -> str:
    """Get quality tool mode: blocking, warn, or disabled."""
    return str(config.get(f"quality.rust.{tool}", "blocking"))


def _resolve_tool_cmd(cmd: list[str], use_uvx: bool = False) -> list[str]:
    """Resolve tool command, using uvx for standalone tools not on PATH."""
    if shutil.which(cmd[0]):
        return cmd
    if use_uvx and shutil.which("uvx"):
        return ["uvx", *cmd]
    return cmd


def _run_tool(
    tool_name: str,
    cmd: list[str],
    mode: str,
    use_uvx: bool = False,
) -> bool:
    """Run a quality tool. Returns True if pipeline should continue."""
    if mode == "disabled":
        info(f"  {tool_name}: disabled")
        return True

    resolved = _resolve_tool_cmd(cmd, use_uvx=use_uvx)
    if resolved == cmd and not shutil.which(cmd[0]):
        if mode == "blocking":
            error(f"  {tool_name}: not installed (required)")
            return False
        warn(f"  {tool_name}: not installed (skipping)")
        return True

    result = subprocess.run(resolved, capture_output=True, text=True)

    if result.returncode == 0:
        success(f"  {tool_name}: passed")
        return True

    # Transient failures (e.g. cargo audit "error loading advisory database")
    # should not block CI — treat as warning regardless of mode
    combined = (result.stdout or "") + (result.stderr or "")
    if "error loading advisory database" in combined.lower():
        warn(f"  {tool_name}: advisory database unavailable (skipping)")
        return True

    if mode == "warn":
        warn(f"  {tool_name}: issues found (non-blocking)")
        if result.stdout:
            print(result.stdout)
        return True

    error(f"  {tool_name}: failed")
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr)
    return False


def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Run Rust quality checks.

    Args:
        config: Merged CI configuration.
        extra_env: Additional env vars (RUST_FEATURES).

    Returns:
        Exit code (0 = success).

    """
    info("Running Rust quality checks...")
    ignores = load_ignores(config._raw)
    had_failure = False

    # cargo fmt --check
    mode = _get_tool_mode("fmt", config)
    if not _run_tool("cargo fmt", ["cargo", "fmt", "--check"], mode):
        had_failure = True

    # cargo clippy — two-pass: production (strict) + test (relaxed)
    mode = _get_tool_mode("clippy", config)
    features = (extra_env or {}).get("RUST_FEATURES", "all")
    feature_sets = _split_feature_sets(features)
    test_ignore = get_test_ignore("rust", config, _DEFAULT_RUST_TEST_IGNORE)
    clippy_user_allows = [f"-A{e.id}" for e in for_tool(ignores, "clippy")]

    has_lib = _has_lib_target()

    for feature_set in feature_sets:
        feature_args = []
        if feature_set == "all":
            feature_args.append("--all-features")
        elif feature_set != "default":
            feature_args.extend(["--features", feature_set])

        # Production pass — lib (when present) + bins, no test/bench targets.
        # Bin-only crates would fail clippy --lib with "no library targets
        # found", so we only include --lib when the project actually has one.
        target_args = ["--lib", "--bins"] if has_lib else ["--bins"]
        prod_cmd = ["cargo", "clippy", *target_args, *feature_args]
        prod_cmd.extend(
            ["--", "-D", "warnings", "-D", "clippy::dbg_macro", *clippy_user_allows]
        )
        if not _run_tool(f"clippy src ({feature_set})", prod_cmd, mode):
            had_failure = True

        # Test pass — test + bench targets, relaxed
        test_cmd = ["cargo", "clippy", "--tests", "--benches"] + feature_args
        allow_flags = [f"-A{rule}" for rule in test_ignore]
        test_cmd.extend(
            [
                "--",
                "-D",
                "warnings",
                "-D",
                "clippy::dbg_macro",
                *allow_flags,
                *clippy_user_allows,
            ]
        )
        if not _run_tool(f"clippy tests ({feature_set})", test_cmd, mode):
            had_failure = True

    # Advisory ignores declared in deny.toml are shared with cargo-audit
    # and osv-scanner so one entry silences all three tools (issue #42).
    deny_advisory_ids = _deny_toml_advisory_ignores()
    if deny_advisory_ids:
        info(
            f"  advisories: sharing {len(deny_advisory_ids)} deny.toml ignore(s) "
            f"with cargo-audit + osv-scanner (issue #42)"
        )

    # cargo audit -- one --ignore <RUSTSEC-id> per entry
    mode = _get_tool_mode("audit", config)
    audit_cmd = ["cargo", "audit"]
    audit_ignores = _merge_deny_advisory_ignores(
        for_tool(ignores, "cargo-audit"), "cargo-audit", deny_advisory_ids
    )
    for entry in audit_ignores:
        audit_cmd.extend(["--ignore", entry.id])
    if not _run_tool("cargo audit", audit_cmd, mode):
        had_failure = True

    # osv-scanner -- malicious-package (MAL-*) scan. cargo audit uses the
    # RustSec DB, which does NOT ingest the OSSF malicious-packages feed;
    # osv-scanner does. Defence-in-depth behind the 7-day Renovate cooldown.
    mode = _get_tool_mode("osv_scanner", config)
    osv_ignores = _merge_deny_advisory_ignores(
        for_tool(ignores, osv_scanner.SLUG), osv_scanner.SLUG, deny_advisory_ids
    )
    if not osv_scanner.run(
        Path("Cargo.lock"),
        osv_ignores,
        mode,
        _run_tool,
    ):
        had_failure = True

    # cargo deny (requires deny.toml — useless without project-specific config)
    mode = _get_tool_mode("deny", config)
    if not Path("deny.toml").exists():
        info("  cargo deny: skipped (no deny.toml found)")
    elif not _run_tool("cargo deny", ["cargo", "deny", "check"], mode):
        had_failure = True

    # Feature matrix check (cargo hack --each-feature)
    if not _run_feature_matrix(config):
        had_failure = True

    # Rustdoc compliance hint (non-blocking; default: enabled)
    _run_rustdoc_hint(config)

    # Semgrep SAST scanning
    mode = _get_tool_mode("semgrep", config)
    semgrep_cmd = ["semgrep", "scan", "--config", "auto", "--error", "--quiet"]
    for entry in for_tool(ignores, "semgrep"):
        semgrep_cmd.extend(["--exclude-rule", entry.id])
    if not _run_tool("semgrep", semgrep_cmd, mode, use_uvx=True):
        had_failure = True

    return 1 if had_failure else 0


def _run_feature_matrix(config: CIConfig) -> bool:
    """Run cargo-hack feature-matrix check.

    Catches feature-gating bugs where a module behind feature X uses a crate
    only declared by feature Y. Without this check, transitive deps from
    other features mask the bug until a downstream consumer enables only X.

    Default behaviour (enabled=true, no other config): runs
        cargo check --no-default-features --lib
        cargo hack --each-feature --no-dev-deps check --lib

    Opt-out requires an explicit reason; CI fails if reason is missing.
    """
    fm_config = config.get("quality.rust.feature_matrix", {})
    if not isinstance(fm_config, dict):
        fm_config = {}

    enabled = fm_config.get("enabled", True)
    reason = fm_config.get("reason", "")

    if not enabled:
        if not reason or not str(reason).strip():
            error(
                "  feature_matrix: opt-out requires a reason "
                "(set quality.rust.feature_matrix.reason)"
            )
            return False
        info(f"  feature_matrix: disabled — {reason}")
        return True

    # Install cargo-hack if missing (scalo-rs-style: idempotent, fail-soft)
    if not shutil.which("cargo-hack"):
        info("  feature_matrix: installing cargo-hack...")
        result = subprocess.run(
            ["cargo", "install", "--locked", "cargo-hack"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            error("  feature_matrix: failed to install cargo-hack")
            if result.stderr:
                print(result.stderr)
            return False

    had_failure = False
    has_lib = _has_lib_target()

    # Both passes use --lib when a lib target exists, --bins otherwise.
    # cargo would error "no library targets found" on bin-only crates
    # otherwise. The semantic intent is "check whatever this crate
    # actually exposes" so falling back to --bins keeps the same
    # feature-gate-safety guarantee.
    target_args = ["--lib"] if has_lib else ["--bins"]

    # Pass 1 — bare crate (no default features). Catches "breaks without defaults" bugs.
    if fm_config.get("also_check_no_default_features", True):
        cmd = ["cargo", "check", "--no-default-features", *target_args]
        if not _run_tool("feature_matrix (no-default-features)", cmd, "blocking"):
            had_failure = True

    # Pass 2 — each feature in isolation
    cmd = ["cargo", "hack", "--each-feature", "--no-dev-deps", "check", *target_args]

    exclude = fm_config.get("exclude", [])
    if isinstance(exclude, list) and exclude:
        cmd.extend(["--exclude-features", ",".join(str(x) for x in exclude)])

    mutex = fm_config.get("mutually_exclusive", [])
    if isinstance(mutex, list):
        for pair in mutex:
            if isinstance(pair, list) and len(pair) >= 2:
                cmd.extend(
                    [
                        "--mutually-exclusive-features",
                        ",".join(str(x) for x in pair),
                    ]
                )

    extra = fm_config.get("extra_args", [])
    if isinstance(extra, list):
        cmd.extend(str(x) for x in extra)

    if not _run_tool("feature_matrix (each-feature)", cmd, "blocking"):
        had_failure = True

    return not had_failure


def _run_rustdoc_hint(config: CIConfig) -> None:
    """Run cargo doc and emit a single concise warning if any issues found.

    Non-blocking by design: rustdoc hygiene is a ratchet, not a gate. Reports
    one summary line + standards links so AI agents and humans know where to
    look. Set quality.rust.rustdoc_hint.enabled=false to silence entirely.
    """
    rd_config = config.get("quality.rust.rustdoc_hint", {})
    if not isinstance(rd_config, dict):
        rd_config = {}

    if not rd_config.get("enabled", True):
        return

    if not shutil.which("cargo"):
        return  # cargo not on PATH — quality stage already noted this

    # rustdoc hint only applies to lib targets — bin-only crates have
    # no public rustdoc surface to lint against.
    if not _has_lib_target():
        return

    # Build with --no-deps + RUSTDOCFLAGS treating warnings as warnings (default)
    # We just want the count, not to fail.
    result = subprocess.run(
        ["cargo", "doc", "--no-deps", "--lib", "--all-features"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "RUSTDOCFLAGS": "-W rustdoc::broken_intra_doc_links "
            "-W rustdoc::private_intra_doc_links "
            "-W rustdoc::invalid_codeblock_attributes "
            "-W rustdoc::invalid_rust_codeblocks "
            "-W rustdoc::bare_urls",
        },
    )
    combined = (result.stdout or "") + (result.stderr or "")
    warning_count = combined.count("warning:")
    # Subtract the trailing summary line (e.g. "generated N warnings") to avoid
    # double-counting; that line itself contains "warning:" once.
    if "lib doc) generated" in combined:
        warning_count = max(0, warning_count - 1)

    if warning_count == 0:
        return

    warn(
        f"  rustdoc: {warning_count} doc warning(s) — see "
        "https://doc.rust-lang.org/rustdoc/ + "
        "https://rust-lang.github.io/api-guidelines/documentation.html"
    )
