# Project:   HyperI CI
# File:      src/hyperi_ci/quality/repo_advisor.py
# Purpose:   Optional, non-blocking repo-hygiene advisory via `alint`
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Repo-hygiene advisory via ``alint`` (asamarts/alint); never fails a build.

With no repo ``.alint.yml``, the shipped ``config/alint/hyperi.alint.yml``
(alint's bundled baseline for our four languages) is passed with ``-c``. A
repo's own ``.alint.yml`` is left for alint to discover. Locally a missing
alint skips without installing; on CI the ``versions.yaml`` pin is downloaded.

alint's root-only manifest rules fire for any ecosystem found anywhere in the
tree (issue #75). When the primary language is known, a generated layer turns
off the other ecosystems' root-only rules, plus ``rust-cargo-lock-exists`` for
a Rust library with no bin target.

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

_DEFAULT_CONFIG = (
    Path(__file__).resolve().parents[1] / "config" / "alint" / "hyperi.alint.yml"
)


_ALINT_GROUP: dict[str, str] = {
    "python": "python",
    "rust": "rust",
    "typescript": "node",
    "javascript": "node",
    "golang": "go",
}

# The `root_only: true` existence rules in each bundled ruleset
# (alint-dsl rulesets/v1/<group>.yml); per-file rules stay on for every group.
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


# Off for a Rust primary with no bin target.
_RUST_LIBRARY_OFF_RULES: tuple[str, ...] = ("rust-cargo-lock-exists",)


def _override_layer(language: str | None, project_dir: Path) -> str | None:
    """Render the primary-language override config, or None to skip it.

    The YAML ``extends:`` the shipped default and turns off the root-only rules
    of every group except the primary's, plus :data:`_RUST_LIBRARY_OFF_RULES`
    for a Rust library. It must be one file: alint 0.13 honours only the first
    ``-c``.

    Args:
        language: The resolved primary language.
        project_dir: Repo root, inspected for Rust bin targets.

    Returns:
        The override YAML, or None when the language is unknown.

    """
    lang = (language or "").strip().lower()
    if lang not in LANGUAGE_MARKERS:
        return None
    primary_group = _ALINT_GROUP.get(lang)
    # YAML single quotes keep Windows backslashes verbatim; a quote is doubled.
    default = str(_DEFAULT_CONFIG).replace("'", "''")
    lines = [
        "version: 1",
        "",
        # The layer and the default it extends are both outside the repo, which
        # alint 0.14 refuses without this.
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
    """Run the alint advisory; always returns 0.

    Output is ``--format github`` in CI and ``human`` locally. ``language`` is
    the primary language, else ``config.language``, for :func:`_override_layer`.
    """
    mode = str(config.get("quality.alint", "auto")).strip().lower()
    if mode in ("disabled", "off", "false", "none"):
        return 0

    root = project_dir or Path.cwd()
    # Holds the override layer for the length of the alint run.
    with tempfile.TemporaryDirectory(prefix="hyperi-ci-alint-") as tmp:
        exe = ci_binary("alint")
        if not exe:
            find_tool("alint", recommended=(mode == "enabled"))
            return 0

        cmd = [exe, "check", "--format", "github" if is_ci() else "human"]
        # A repo's own .alint.yml is left for alint to discover.
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
            warn(f"alint could not be run ({exc}) - advisory only, not failing.")
            return 0
    # alint exits 1 on error-level findings, and 2 or 3 when alint itself fails.
    if result.returncode >= 2:
        warn(
            f"alint exited {result.returncode} (config/internal issue) - "
            "advisory only, not failing the build."
        )
    return 0
