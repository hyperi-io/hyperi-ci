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

The runner moves an annotation's ``title=`` and ``file=`` out of the log text,
so in CI each line's message is prefixed with its rule id and path.

Config (``.hyperi-ci.yaml``):

    quality.alint: auto      # run if alint is installed, else info-skip (default)
    quality.alint: enabled   # run, warn (still non-fatal) if alint is missing
    quality.alint: disabled  # never run
"""

import re
import tempfile
from pathlib import Path

import yaml

from hyperi_ci.common import echo_chunk, escape_command_data, is_ci, run_cmd, warn
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

_ANNOTATION = re.compile(r"^::(notice|warning|error)( [^:]*)?::(.*)$")

# Property values escape these on top of what escape_command_data covers.
_PROPERTY_ESCAPES = (("%2C", ","), ("%3A", ":"), ("%0A", "\n"), ("%0D", "\r"))


def _unescape_property(value: str) -> str:
    """Decode a workflow command property value, ``%25`` last."""
    for code, char in _PROPERTY_ESCAPES:
        value = value.replace(code, char)
    return value.replace("%25", "%")


def _with_location(line: str) -> str:
    """Prefix an annotation's message with its rule id and ``file=`` path.

    Any other line is returned unchanged.
    """
    match = _ANNOTATION.match(line)
    if not match:
        return line
    level, props, data = match.groups()
    fields = dict(
        prop.split("=", 1) for prop in (props or "").strip().split(",") if "=" in prop
    )
    prefix = ""
    if rule := fields.get("title"):
        prefix += f"[{_unescape_property(rule)}] "
    if path := fields.get("file"):
        prefix += f"{_unescape_property(path)}: "
    if not prefix:
        return line
    return f"::{level}{props or ''}::{escape_command_data(prefix)}{data}"


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
    off: list[str] = []
    for group, rules in _ROOT_ONLY_RULES.items():
        if group != primary_group:
            off.extend(rules)
    if primary_group == "rust" and rust_is_library(project_dir):
        off.extend(_RUST_LIBRARY_OFF_RULES)
    return yaml.safe_dump(
        {
            "version": 1,
            # The layer and the default it extends are both outside the repo,
            # which alint 0.14 refuses without this.
            "allow_out_of_root": True,
            "extends": [str(_DEFAULT_CONFIG)],
            "rules": [{"id": rule_id, "level": "off"} for rule_id in off],
        },
        sort_keys=False,
    )


def run(
    config: CIConfig,
    project_dir: Path | None = None,
    *,
    language: str | None = None,
) -> int:
    """Run the alint advisory; always returns 0.

    Output is ``--format github`` in CI, passed through :func:`_with_location`,
    and ``human`` locally. ``language`` is
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

        github = is_ci()
        cmd = [exe, "check", "--format", "github" if github else "human"]
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
            result = run_cmd(
                cmd, check=False, cwd=root, capture=github, merge_stderr=github
            )
        except OSError as exc:
            warn(f"alint could not be run ({exc}) - advisory only, not failing.")
            return 0
    if github and result.stdout:
        echo_chunk(
            "".join(f"{_with_location(line)}\n" for line in result.stdout.splitlines())
        )
    # alint exits 1 on error-level findings, and 2 or 3 when alint itself fails.
    if result.returncode >= 2:
        warn(
            f"alint exited {result.returncode} (config/internal issue) - "
            "advisory only, not failing the build."
        )
    return 0
