# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/quality.py
# Purpose:   Rust quality checks (fmt, clippy, audit, deny)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Rust quality checks: fmt, clippy, audit, osv-scanner, deny, the feature matrix.

In a root-package workspace the package-scoped commands take ``--workspace``;
cargo fmt and cargo audit cover the whole workspace already.
"""

import os
import re
import shutil
import subprocess
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from hyperi_ci.common import (
    announce,
    error,
    info,
    run_cmd,
    strip_ansi,
    success,
    warn,
)
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import (
    get_test_ignore,
    resolve_tool_mode,
    run_gate_tool,
)
from hyperi_ci.languages.rust._manifest import (
    feature_resolver,
    is_root_package_workspace,
    split_feature_sets,
)
from hyperi_ci.languages.rust.targets import cargo_metadata
from hyperi_ci.quality import cargo_flags, osv_scanner
from hyperi_ci.quality.ignores import IgnoreEntry, for_tool, load_ignores
from hyperi_ci.tools import matches_pin, version_output
from hyperi_ci.versions import tool_version

# A target entry of its own joins the repo's and the runner's target rustflags,
# where RUSTFLAGS would discard them.
_DENY_WARNINGS_CONFIG = "target.'cfg(all())'.rustflags=[\"-Dwarnings\"]"

# cargo-hack writes its progress line as a log group under GitHub Actions.
_HACK_RUN = re.compile(
    r"^(?:info: |::group::)running `(?P<cmd>[^`]*)` on (?P<crate>\S+)"
)
_WARNED_UNIT = re.compile(r"^warning: `[^`]+` \(.+\) generated \d+ warnings?")
_FAILED_UNIT = re.compile(r"^error: could not compile `")

_FEATURE_WARNINGS_TITLE = "hyperi-ci feature set builds with warnings"
_RESOLVER_ONE_TITLE = "hyperi-ci feature matrix cannot see dev-dependency features"


@dataclass(frozen=True, slots=True)
class FeatureSetFinding:
    """A feature set whose build produced diagnostics.

    Attributes:
        label: The feature flags cargo ran with, and the crate when known.
        message: The first diagnostic, with its source location when cargo gave one.
    """

    label: str
    message: str


def _deny_toml_advisory_ignores(project_dir: Path | None = None) -> list[str]:
    """Return the advisory IDs ignored in ``deny.toml`` ``[advisories.ignore]``.

    These are fed to cargo-audit and osv-scanner too, so one entry silences
    all three tools (issue #42). Accepts both the bare-string and the
    ``{ id = ... }`` entry forms.
    """
    cwd = project_dir or Path.cwd()
    deny_toml = cwd / "deny.toml"
    if not deny_toml.exists():
        return []
    try:
        manifest = tomllib.loads(deny_toml.read_text(encoding="utf-8"))
    except Exception as exc:  # malformed deny.toml -- cargo deny will report it
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
        # The list can also hold crate names and licence IDs, which mean
        # nothing to cargo-audit or osv-scanner.
        if ident.startswith(("RUSTSEC-", "CVE-", "GHSA-")):
            ids.append(ident)
    return ids


def _merge_deny_advisory_ignores(
    entries: list[IgnoreEntry], tool: str, deny_ids: list[str]
) -> list[IgnoreEntry]:
    """Union ``deny.toml`` advisory IDs into ``tool``'s ignore entries, de-duped."""
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

    Callers pass ``--lib`` only when this holds, because cargo fails with
    ``no library targets found`` on a bin-only crate.
    """
    cwd = project_dir or Path.cwd()
    if not (cwd / "Cargo.toml").exists():
        return False

    metadata = cargo_metadata(cwd)
    if metadata is not None:
        for package in metadata.get("packages", []):
            for target in package.get("targets", []):
                if "lib" in target.get("kind", []) or "rlib" in target.get("kind", []):
                    return True
        return False

    # Without cargo metadata, workspace members are not explored.
    return (cwd / "src" / "lib.rs").exists()


def _package_lib_map(project_dir: Path | None = None) -> dict[str, bool]:
    """Map each workspace package name to whether it exposes a lib target.

    `cargo hack` runs per member, so one bin-only member given ``--lib`` fails
    the whole check. Empty when cargo metadata is unavailable.
    """
    cwd = project_dir or Path.cwd()
    if not (cwd / "Cargo.toml").exists():
        return {}

    metadata = cargo_metadata(cwd)
    if metadata is None:
        return {}

    lib_map: dict[str, bool] = {}
    for package in metadata.get("packages", []):
        name = package.get("name")
        if not name:
            continue
        lib_map[name] = any(
            "lib" in target.get("kind", []) or "rlib" in target.get("kind", [])
            for target in package.get("targets", [])
        )
    return lib_map


# cargo audit's message, then cargo deny's fetch and load messages.
_ADVISORY_DB_ERRORS = (
    "error loading advisory database",
    "failed to fetch advisory database",
    "failed to load advisory database",
)


def _advisory_db_unreachable(result: subprocess.CompletedProcess[str]) -> bool:
    """Whether cargo audit or cargo deny failed to load its advisory database."""
    output = f"{result.stdout or ''}{result.stderr or ''}".lower()
    return result.returncode != 0 and any(e in output for e in _ADVISORY_DB_ERRORS)


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

    mode = resolve_tool_mode("fmt", config, language="rust")
    if not run_gate_tool("cargo fmt", ["cargo", "fmt", "--check"], mode):
        had_failure = True

    # Two clippy passes: production code strict, tests and benches relaxed.
    mode = resolve_tool_mode("clippy", config, language="rust")
    features = (extra_env or {}).get("RUST_FEATURES", "all")
    feature_sets = split_feature_sets(features)
    test_ignore = get_test_ignore("rust", config)
    clippy_user_allows = [f"-A{e.id}" for e in for_tool(ignores, "clippy")]

    has_lib = _has_lib_target()
    workspace = is_root_package_workspace()
    workspace_args = ["--workspace"] if workspace else []
    if workspace:
        info(
            "  Root package is also a workspace: clippy, cargo deny, the feature "
            "matrix and rustdoc take --workspace, so every member is checked"
        )

    for feature_set in feature_sets:
        feature_args = []
        if feature_set == "all":
            feature_args.append("--all-features")
        elif feature_set != "default":
            feature_args.extend(["--features", feature_set])

        target_args = ["--lib", "--bins"] if has_lib else ["--bins"]
        prod_cmd = ["cargo", "clippy", *workspace_args, *target_args, *feature_args]
        prod_cmd.extend(
            ["--", "-D", "warnings", "-D", "clippy::dbg_macro", *clippy_user_allows]
        )
        if not run_gate_tool(f"clippy src ({feature_set})", prod_cmd, mode):
            had_failure = True

        test_cmd = ["cargo", "clippy", *workspace_args, "--tests", "--benches"]
        test_cmd.extend(feature_args)
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
        if not run_gate_tool(f"clippy tests ({feature_set})", test_cmd, mode):
            had_failure = True

    deny_advisory_ids = _deny_toml_advisory_ignores()
    if deny_advisory_ids:
        info(
            f"  advisories: sharing {len(deny_advisory_ids)} deny.toml ignore(s) "
            f"with cargo-audit + osv-scanner (issue #42)"
        )

    mode = resolve_tool_mode("audit", config, language="rust")
    audit_cmd = ["cargo", "audit"]
    audit_ignores = _merge_deny_advisory_ignores(
        for_tool(ignores, "cargo-audit"), "cargo-audit", deny_advisory_ids
    )
    for entry in audit_ignores:
        audit_cmd.extend(["--ignore", entry.id])
    if not run_gate_tool(
        "cargo audit",
        audit_cmd,
        mode,
        pinned="cargo-audit",
        retry_unreachable=_advisory_db_unreachable,
    ):
        had_failure = True

    # osv-scanner catches MAL-* advisories: the RustSec DB cargo audit reads
    # does not ingest the OSSF malicious-packages feed.
    mode = resolve_tool_mode("osv_scanner", config, language="rust")
    osv_ignores = _merge_deny_advisory_ignores(
        for_tool(ignores, osv_scanner.SLUG), osv_scanner.SLUG, deny_advisory_ids
    )
    if not osv_scanner.run(Path("Cargo.lock"), osv_ignores, mode):
        had_failure = True

    mode = resolve_tool_mode("deny", config, language="rust")
    if not Path("deny.toml").exists():
        # A bare "skipped" would read as though advisories went unchecked.
        info(
            "  cargo deny: skipped (no deny.toml). Advisories are still "
            "gated by cargo audit; a deny.toml would add licence, ban and "
            "source checks."
        )
    elif not run_gate_tool(
        "cargo deny",
        ["cargo", "deny", *workspace_args, "check"],
        mode,
        pinned="cargo-deny",
        retry_unreachable=_advisory_db_unreachable,
    ):
        had_failure = True

    if not _run_feature_matrix(
        config, workspace=workspace, clippy_allows=clippy_user_allows
    ):
        had_failure = True

    _run_rustdoc_hint(config, workspace=workspace)

    # Rustflags this repo declares but cannot ship (issue #178).
    if cargo_flags.run(config) != 0:
        had_failure = True

    return 1 if had_failure else 0


def _names_a_package_scope(args: list[str]) -> bool:
    """Return True when ``args`` already pick the packages cargo acts on.

    cargo and cargo-hack reject a second ``--workspace``, and ``-p`` narrows
    what ``--workspace`` would widen, so a repo's own scope wins.
    """
    scope_flags = {"--workspace", "--all", "-p", "--package"}
    return any(a in scope_flags or a.startswith("--package=") for a in args)


def _cargo_hack_version() -> str | None:
    """What ``cargo hack --version`` prints, or None when cargo-hack is absent."""
    if not shutil.which("cargo-hack"):
        return None
    return version_output(["cargo", "hack", "--version"])


def _ensure_cargo_hack() -> bool:
    """Make the pinned cargo-hack the one ``cargo hack`` runs.

    A different version is reinstalled at the pin, since warnings can differ
    by version and the local gate would then disagree with CI.

    Returns:
        False when the pinned cargo-hack could not be installed.

    """
    pinned = tool_version("cargo-hack")
    found = _cargo_hack_version()
    if found and matches_pin(pinned, found):
        return True

    seen = f"found '{found.splitlines()[0]}'" if found else "not found"
    install = ["cargo", "install", "--locked", "cargo-hack", "--version", pinned]
    info(f"  feature_matrix: cargo-hack {seen}, pinned {pinned} -- installing")
    try:
        result = run_cmd(install, check=False, capture=True)
    except OSError as exc:
        error(f"  feature_matrix: cannot install cargo-hack {pinned}: {exc}")
        return False
    if result.returncode != 0:
        error(f"  feature_matrix: failed to install cargo-hack {pinned}")
        if result.stderr:
            info(result.stderr)
        return False

    after = _cargo_hack_version()
    if not after or not matches_pin(pinned, after):
        # cargo install wrote the pin, but another cargo-hack still answers first.
        warn(
            f"  feature_matrix: installed cargo-hack {pinned}, but `cargo hack` "
            f"still reports '{(after or 'nothing').splitlines()[0]}' -- check PATH"
        )
    return True


def _run_feature_matrix(
    config: CIConfig,
    *,
    workspace: bool = False,
    clippy_allows: Sequence[str] = (),
) -> bool:
    """Run the cargo-hack feature matrix under clippy.

    Catches a module behind feature X using a crate only feature Y declares,
    plus lints that fire only on a single-feature build. Runs
    ``--no-default-features`` and then ``cargo hack --each-feature``; neither
    writes to the working tree.

    ``quality.rust.feature_matrix.warnings`` decides what a warning does:
    ``warn`` names each set, ``blocking`` fails, ``disabled`` does not look.
    Opting out of the matrix needs a ``reason``.

    Args:
        config: Merged CI configuration.
        workspace: Pass ``--workspace``, for a root-package workspace, unless
            each member is already scoped with ``-p`` or ``extra_args`` names
            a scope.
        clippy_allows: ``-A<lint>`` flags from the repo's clippy ignores, so a
            lint the repo allowed is not reported again per feature.

    Returns:
        True when the check passed or was disabled with a reason.

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
        info(f"  feature_matrix: disabled - {reason}")
        return True

    if not _ensure_cargo_hack():
        return False

    had_failure = False
    warnings_mode = resolve_tool_mode(
        "feature_matrix.warnings", config, language="rust", default="warn"
    )

    # A workspace mixing lib and bin-only members gets one -p scope per member,
    # each with its own --lib or --bins.
    extra = fm_config.get("extra_args", [])
    extra = [str(x) for x in extra] if isinstance(extra, list) else []

    lib_map = _package_lib_map()
    mixed_workspace = len(lib_map) > 1 and len(set(lib_map.values())) > 1
    if mixed_workspace:
        scopes = [
            (["-p", name], ["--lib"] if has_lib else ["--bins"])
            for name, has_lib in sorted(lib_map.items())
        ]
    else:
        widen = workspace and not _names_a_package_scope(extra)
        scopes = [
            (
                ["--workspace"] if widen else [],
                ["--lib"] if _has_lib_target() else ["--bins"],
            )
        ]

    lint_tool = (
        "check"
        if resolve_tool_mode("clippy", config, language="rust") == "disabled"
        else "clippy"
    )
    lint_args: list[str] = []
    if lint_tool == "clippy":
        # Below blocking, a lint the repo sets to deny only warns, so it names
        # the feature set instead of failing the matrix.
        cap = [] if warnings_mode == "blocking" else ["--cap-lints", "warn"]
        driver_args = [*cap, *clippy_allows]
        lint_args = ["--", *driver_args] if driver_args else []

    tuning: list[str] = []

    exclude = fm_config.get("exclude", [])
    if isinstance(exclude, list) and exclude:
        tuning.extend(["--exclude-features", ",".join(str(x) for x in exclude)])

    mutex = fm_config.get("mutually_exclusive", [])
    if isinstance(mutex, list):
        for pair in mutex:
            if isinstance(pair, list) and len(pair) >= 2:
                tuning.extend(
                    [
                        "--mutually-exclusive-features",
                        ",".join(str(x) for x in pair),
                    ]
                )

    tuning.extend(extra)

    # SHORTCUT: resolver 1 keeps dev-dependency features in the library build,
    # upgrade to a scratch-copy run with --no-dev-deps if a repo cannot move off it.
    if feature_resolver() == 1:
        announce(
            "feature_matrix: Cargo.toml selects feature resolver 1, which builds "
            "the library with its dev-dependencies' features, so a feature that "
            "only a dev-dependency enables can hide a feature-gating bug. Set "
            'resolver = "2" or later in the root Cargo.toml to check it.',
            _RESOLVER_ONE_TITLE,
        )

    if fm_config.get("also_check_no_default_features", True):
        for scope_args, target_args in scopes:
            cmd = [
                "cargo",
                lint_tool,
                "--no-default-features",
                *scope_args,
                *target_args,
                *lint_args,
            ]
            label = " on ".join(["--no-default-features", *scope_args[1:]])
            if not _run_matrix_pass(
                "feature_matrix (no-default-features)", cmd, label, warnings_mode
            ):
                had_failure = True

    # No --no-dev-deps: it rewrites every Cargo.toml while it runs, and
    # resolver 2+ already keeps dev-dependency features out of the build.
    for scope_args, target_args in scopes:
        cmd = [
            "cargo",
            "hack",
            "--each-feature",
            lint_tool,
            *scope_args,
            *target_args,
            *tuning,
            *lint_args,
        ]
        if not _run_matrix_pass(
            "feature_matrix (each-feature)",
            cmd,
            "a feature set cargo-hack did not name",
            warnings_mode,
        ):
            had_failure = True

    return not had_failure


def _deny_warnings(
    cmd: list[str], environ: Mapping[str, str]
) -> tuple[list[str], dict[str, str]]:
    """Return ``cmd`` and the extra env that make rustc deny every warning.

    Cargo takes flags from ONE source: ``CARGO_ENCODED_RUSTFLAGS``, else
    ``RUSTFLAGS``, else the joined ``target.*`` entries, else
    ``build.rustflags``. With no env source, the deny is added as its own
    ``target.'cfg(all())'`` entry, which joins existing target entries but
    displaces ``build.rustflags`` where there are none.

    Args:
        cmd: The cargo or cargo-hack command, containing ``clippy`` or
            ``check``.
        environ: The environment the command will inherit.

    Returns:
        The command to run, and env vars to set on top of ``environ``.

    """
    if "CARGO_ENCODED_RUSTFLAGS" in environ:
        flags = [f for f in environ["CARGO_ENCODED_RUSTFLAGS"].split("\x1f") if f]
        return cmd, {"CARGO_ENCODED_RUSTFLAGS": "\x1f".join([*flags, "-D", "warnings"])}
    if "RUSTFLAGS" in environ:
        return cmd, {"RUSTFLAGS": f"{environ['RUSTFLAGS'].strip()} -D warnings".strip()}
    subcommand = "clippy" if "clippy" in cmd else "check"
    at = cmd.index(subcommand) + 1
    return [*cmd[:at], "--config", _DENY_WARNINGS_CONFIG, *cmd[at:]], {}


def _feature_set_label(hack_cmd: str, crate: str) -> str:
    """Name the feature set in a ``cargo hack`` "running" line."""
    tokens = hack_cmd.split()
    if "--features" in tokens and tokens.index("--features") + 1 < len(tokens):
        flags = f"--features {tokens[tokens.index('--features') + 1]}"
    elif "--all-features" in tokens:
        flags = "--all-features"
    else:
        flags = "--no-default-features"
    return f"{flags} on {crate}"


def _first_diagnostic(lines: list[str], prefix: str) -> str:
    """Return the first ``prefix`` diagnostic, with the location cargo printed."""
    fallback = ""
    for index, line in enumerate(lines):
        if not line.startswith(prefix):
            continue
        if _WARNED_UNIT.match(line) or _FAILED_UNIT.match(line):
            continue
        text = line.removeprefix(prefix).strip()
        following = lines[index + 1].strip() if index + 1 < len(lines) else ""
        if following.startswith("--> "):
            return f"{text} ({following.removeprefix('--> ')})"
        fallback = fallback or text
    return fallback


def _feature_set_findings(
    output: str, level: str, first_label: str
) -> list[FeatureSetFinding]:
    """Split cargo output by feature set and report each set that ``level``-ed.

    A set counts only when cargo closes one of its units with a warned or
    failed summary line. Cargo replays cached diagnostics, so a warm target
    directory reports the same sets as a cold one.

    Args:
        output: cargo's stdout and stderr through one pipe, so cargo-hack's
            progress lines stay in order with the diagnostics.
        level: ``warning`` or ``error``.
        first_label: Name for output before any ``running`` line, which is all
            of it for a plain ``cargo check``.

    Returns:
        One finding per feature set, in the order cargo ran them.

    """
    summary = _WARNED_UNIT if level == "warning" else _FAILED_UNIT
    sections: list[tuple[str, list[str]]] = [(first_label, [])]
    for line in output.splitlines():
        if run := _HACK_RUN.match(line):
            label = _feature_set_label(run["cmd"], run["crate"])
            sections.append((label, []))
            continue
        sections[-1][1].append(line)

    findings: list[FeatureSetFinding] = []
    for label, lines in sections:
        if any(summary.match(line) for line in lines):
            message = _first_diagnostic(lines, f"{level}: ")
            findings.append(FeatureSetFinding(label=label, message=message))
    return findings


def _run_matrix_pass(
    tool_name: str, cmd: list[str], first_label: str, warnings_mode: str
) -> bool:
    """Run one feature-matrix pass. Returns True if the pipeline should continue.

    A compile error always fails. A warning fails under ``blocking``, is named
    per feature set under ``warn``, and is not looked for under ``disabled``.
    """
    if warnings_mode == "disabled" or not shutil.which(cmd[0]):
        return run_gate_tool(tool_name, cmd, "blocking")

    # The env var beats a [term] table; strip_ansi covers a --config in extra_args.
    env: dict[str, str] = {"CARGO_TERM_COLOR": "never"}
    if warnings_mode == "blocking":
        cmd, deny_env = _deny_warnings(cmd, os.environ)
        env.update(deny_env)
        if cmd[1] == "hack":
            # Without it cargo-hack stops at the first set, and each rerun names one.
            cmd = [*cmd[:2], "--keep-going", *cmd[2:]]

    # One pipe keeps cargo-hack's set names (stdout under GitHub Actions) in order
    # with the diagnostics (stderr).
    result = run_cmd(cmd, check=False, capture=True, merge_stderr=True, env=env)
    output = strip_ansi(result.stdout or "")

    if result.returncode != 0:
        error(f"  {tool_name}: failed")
        for finding in _feature_set_findings(output, "error", first_label):
            error(f"  {tool_name}: {finding.label}: {finding.message}")
        info(output)
        return False

    warned = _feature_set_findings(output, "warning", first_label)
    if not warned:
        success(f"  {tool_name}: passed")
        return True
    for finding in warned:
        announce(
            f"feature_matrix: {finding.label} builds with warnings: {finding.message}",
            _FEATURE_WARNINGS_TITLE,
        )
    info(
        f"  {tool_name}: {len(warned)} feature set(s) warn (non-blocking; "
        "quality.rust.feature_matrix.warnings: blocking fails on them)"
    )
    return True


def _run_rustdoc_hint(config: CIConfig, *, workspace: bool = False) -> None:
    """Run cargo doc and warn once with the doc-warning count; never fails.

    ``quality.rust.rustdoc_hint.enabled: false`` silences it.

    Args:
        config: Merged CI configuration.
        workspace: Pass ``--workspace``, for a root-package workspace.

    """
    rd_config = config.get("quality.rust.rustdoc_hint", {})
    if not isinstance(rd_config, dict):
        rd_config = {}

    if not rd_config.get("enabled", True):
        return

    if not shutil.which("cargo"):
        return  # cargo not on PATH -- quality stage already noted this

    # A bin-only crate has no public rustdoc surface.
    if not _has_lib_target():
        return

    result = run_cmd(
        [
            "cargo",
            "doc",
            *(["--workspace"] if workspace else []),
            "--no-deps",
            "--lib",
            "--all-features",
        ],
        check=False,
        capture=True,
        env={
            "RUSTDOCFLAGS": "-W rustdoc::broken_intra_doc_links "
            "-W rustdoc::private_intra_doc_links "
            "-W rustdoc::invalid_codeblock_attributes "
            "-W rustdoc::invalid_rust_codeblocks "
            "-W rustdoc::bare_urls",
        },
    )
    combined = (result.stdout or "") + (result.stderr or "")
    warning_count = combined.count("warning:")
    # Each documented crate adds one "warning: `x` (lib doc) generated N
    # warnings" summary line, which is not a finding of its own.
    warning_count = max(0, warning_count - combined.count("lib doc) generated"))

    if warning_count == 0:
        return

    warn(
        f"  rustdoc: {warning_count} doc warning(s) -- see "
        "https://doc.rust-lang.org/rustdoc/ + "
        "https://rust-lang.github.io/api-guidelines/documentation.html"
    )
