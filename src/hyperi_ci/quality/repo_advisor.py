# Project:   HyperI CI
# File:      src/hyperi_ci/quality/repo_advisor.py
# Purpose:   Optional, non-blocking repo-hygiene advisory via `alint`
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Optional, non-blocking repo-hygiene advisory via ``alint``.

Wires the external ``alint`` linter (asamarts/alint) as an ADVISORY step.
alint is profile-gated - it detects the ecosystem (python/rust/node/go/...)
and runs matching rulesets, surfacing recommendations at info/warning level
(missing ``.gitignore`` / ``.editorconfig``, tracked build artefacts, absent
lockfile, ...). We surface those WITHOUT ever failing the build.

Zero per-repo config. hyperi-ci ships an opinionated default
(``config/alint/hyperi.alint.yml``, just alint's own bundled baseline for our
four languages) and passes it via ``alint check -c <default>``, so the advisory
works with no file the developer has to add. A repo that DOES want to tune it
runs ``alint init`` to drop its own ``.alint.yml``; that wins and we step aside
(let alint discover it). Turn the whole thing off with ``quality.alint:
disabled``.

alint is NOT a hyperi-ci dependency. Locally, missing -> the step skips via
:func:`hyperi_ci.tools.find_tool` (an info nudge under ``auto``, a louder warn
under ``enabled``) and never installs anything. In CI a missing alint is
fetched as the PINNED prebuilt binary (``tools.alint`` in
``config/versions.yaml``, through :mod:`hyperi_ci.native_tools`) so the
advisory runs on vanilla runners - nothing bakes alint into a runner image.
Either way it never fails the build.

The default is additionally PRIMARY-LANGUAGE-AWARE (issue #75): alint's
bundled ``has_<lang>`` facts match a manifest anywhere in the tree, but its
manifest/lockfile existence rules demand the file at the repo ROOT - right
for the repo's primary ecosystem, a false positive for a nested secondary
one (a TS monorepo with ``packages/*/go.mod`` got a red ``go-mod-exists``
error). When the primary language is known, a generated override layer
(``extends:`` the shipped default, then ``level: off``) disables the OTHER
ecosystems' root-only rules; per-file rules (Trojan-Source, hygiene) stay
active for every ecosystem. A Rust library with no bin target also loses
``rust-cargo-lock-exists``: Rust's own guidance leaves committing a library's
``Cargo.lock`` to the maintainer, so the warning is noise there.

Config (``.hyperi-ci.yaml``):

    quality.alint: auto      # run if alint is installed, else info-skip (default)
    quality.alint: enabled   # run, warn (still non-fatal) if alint is missing
    quality.alint: disabled  # never run
"""

import tempfile
from pathlib import Path

from hyperi_ci.common import is_ci, run_cmd, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.detect import LANGUAGE_MARKERS
from hyperi_ci.languages.rust.targets import rust_is_library
from hyperi_ci.native_tools import ci_binary
from hyperi_ci.tools import find_tool

# Shipped opinionated default (packaged under hyperi_ci/config/, so it travels
# in the wheel). config/ is a sibling of quality/ inside the package.
_DEFAULT_CONFIG = (
    Path(__file__).resolve().parents[1] / "config" / "alint" / "hyperi.alint.yml"
)


# hyperi-ci language -> the alint bundled-ruleset group it maps to.
_ALINT_GROUP: dict[str, str] = {
    "python": "python",
    "rust": "rust",
    "typescript": "node",
    "javascript": "node",
    "golang": "go",
}

# The root-only manifest/lockfile/toolchain existence rules in each bundled
# ruleset (alint-dsl rulesets/v1/<group>.yml). These are the rules whose
# `root_only: true` clashes with the tree-wide `has_<group>` fact on nested
# monorepo packages - the per-file content/hygiene rules are NOT listed and
# stay active for every ecosystem.
_ROOT_ONLY_RULES: dict[str, tuple[str, ...]] = {
    "python": ("python-manifest-exists", "python-has-lockfile"),
    "rust": (
        "rust-cargo-toml-exists",
        "rust-cargo-lock-exists",
        "rust-toolchain-pinned",
    ),
    "node": (
        "node-package-json-exists",
        "node-has-lockfile",
        "node-engine-or-nvmrc",
    ),
    "go": ("go-mod-exists", "go-sum-exists"),
}


# Rules that stay on for a Rust primary but go off when the crate or workspace
# has no bin target.
_RUST_LIBRARY_OFF_RULES: tuple[str, ...] = ("rust-cargo-lock-exists",)


def _override_layer(language: str | None, project_dir: Path) -> str | None:
    """Render the primary-language override config, or None to skip it.

    Returns YAML that ``extends:`` the shipped default and sets ``level:
    off`` on the root-only rules of every alint group EXCEPT the primary
    language's own. A Rust primary that is library-only also switches off
    :data:`_RUST_LIBRARY_OFF_RULES`. None when the language is unknown -
    with no primary to judge against, the shipped default runs unmodified.

    NOTE: this must be ONE config file. alint 0.13's repeatable ``-c`` only
    honours the first file (later layers are silently ignored), so a second
    ``-c`` override layer does not work; ``extends:`` + same-id rule merge
    does.

    Args:
        language: The resolved primary language.
        project_dir: Repo root, inspected for Rust bin targets.

    Returns:
        The override YAML, or None.

    """
    lang = (language or "").strip().lower()
    if lang not in LANGUAGE_MARKERS:
        return None
    primary_group = _ALINT_GROUP.get(lang)
    # Single-quote the path: YAML single quotes never escape backslashes, so
    # a Windows install path survives verbatim. The only char needing care is
    # a literal quote in the path (doubled per YAML).
    default = str(_DEFAULT_CONFIG).replace("'", "''")
    lines = [
        "version: 1",
        "",
        # This layer lives in a temp dir and extends the packaged default in
        # site-packages - both outside the linted repo. alint 0.14 confines
        # local `extends:` targets to the lint root unless the top-level
        # config opts out; 0.13 parses the same key (there it grants rules
        # out-of-root READS, harmless for the bundled repo-relative rules).
        "allow_out_of_root: true",
        "",
        "extends:",
        f"  - '{default}'",
        "",
        "rules:",
    ]
    off: list[str] = []
    for group, rules in _ROOT_ONLY_RULES.items():
        if group != primary_group:
            off.extend(rules)
    if primary_group == "rust" and rust_is_library(project_dir):
        off.extend(_RUST_LIBRARY_OFF_RULES)
    for rule_id in off:
        lines += [f"  - id: {rule_id}", "    level: off"]
    return "\n".join(lines) + "\n"


def run(
    config: CIConfig,
    project_dir: Path | None = None,
    *,
    language: str | None = None,
) -> int:
    """Run the alint advisory. ALWAYS returns 0 - it never gates a build.

    ``quality.alint`` selects the mode (auto / enabled / disabled). Findings
    stream straight to the log (``--format github`` in CI so they land as
    annotations, ``human`` locally). ``language`` is the resolved primary
    language (falls back to ``config.language``); when known, the shipped
    default is wrapped in the :func:`_override_layer` so root-only rules of
    OTHER ecosystems stop false-positive-ing on nested monorepo packages.
    """
    mode = str(config.get("quality.alint", "auto")).strip().lower()
    if mode in ("disabled", "off", "false", "none"):
        return 0

    root = project_dir or Path.cwd()
    # The temp dir holds the generated override layer, which must outlive the
    # alint run. tempfile is the sanctioned process-local scratch.
    with tempfile.TemporaryDirectory(prefix="hyperi-ci-alint-") as tmp:
        # In CI a missing alint is the pinned download, so the advisory runs on
        # vanilla runners (nothing bakes alint). Only when both miss does the
        # install notice fire.
        exe = ci_binary("alint")
        if not exe:
            find_tool("alint", recommended=(mode == "enabled"))
            return 0

        cmd = [exe, "check", "--format", "github" if is_ci() else "human"]
        # A repo's own .alint.yml wins - let alint auto-discover it. Otherwise
        # ship the HyperI default explicitly so the advisory works with no
        # per-repo file - language-scoped when the primary language is known.
        if not (root / ".alint.yml").exists():
            layer = _override_layer(language or getattr(config, "language", None), root)
            if layer is None:
                cmd += ["-c", str(_DEFAULT_CONFIG)]
            else:
                layer_path = Path(tmp) / "hyperi.alint.override.yml"
                layer_path.write_text(layer, encoding="utf-8", newline="\n")
                cmd += ["-c", str(layer_path)]

        try:
            result = run_cmd(cmd, check=False, cwd=root)
        except OSError as exc:
            # The binary resolved but could not be exec'd (removed between
            # which() and exec, broken symlink, no exec bit). Advisory -
            # never fail.
            warn(f"alint could not be run ({exc}) - advisory only, not failing.")
            return 0
    # ADVISORY: alint exits 1 on error-level findings, 0 otherwise (warnings /
    # info never fail it). It is a recommendation surface here, not a gate, so
    # we never propagate a non-zero. Exit 2 (config) / 3 (internal) means alint
    # itself misbehaved - note it, still don't fail the build.
    if result.returncode >= 2:
        warn(
            f"alint exited {result.returncode} (config/internal issue) - "
            "advisory only, not failing the build."
        )
    return 0
