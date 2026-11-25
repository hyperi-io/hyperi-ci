# Project:   HyperI CI
# File:      src/hyperi_ci/config.py
# Purpose:   Typed configuration schema and loader
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Configuration schema, loading, and validation for HyperI CI.

Cascade priority (highest wins):
  CLI flags -> ENV vars (HYPERCI_*) -> .hyperi-ci.yaml -> defaults.yaml -> hardcoded
"""

import copy
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from hyperi_ci import classification
from hyperi_ci.project_config import find_config
from hyperi_ci.vocabulary import (
    CONFIG_NAMESPACE,
    LEGACY_CONFIG_NAMESPACE,
    canonical_key,
    carry_over_removed_gates,
    drop_removed_notices,
    find_removed_keys,
    fold_legacy_config,
    key_candidates,
    merge_deep,
    report_carry_overs,
    report_deprecated_config,
    report_removed_keys,
)

_CONFIG_DIR = Path(__file__).resolve().parent / "config"

# Lifecycle stages for `project.status` in `.hyperi-ci.yaml`, optional and
# information-only (see defaults.yaml).
VALID_PROJECT_STATUSES: tuple[str, ...] = (
    "experimental",
    "alpha",
    "beta",
    "ga",
    "legacy",
    "deprecated",
)


class ConfigError(ValueError):
    """A project config value the loader refuses rather than read past."""


@dataclass
class OrgConfig:
    """Organisation-specific configuration loaded from config/org.yaml."""

    github_org: str = "hyperi-io"
    ghcr_registry: str = "ghcr.io"
    ghcr_org: str = "hyperi-io"
    r2_bucket: str = "bin-repo"
    r2_account_id: str = "98d20454e2af7a9397ad9366a1641659"
    r2_public_url: str = "https://downloads.hyperi.io"
    mirror_path: str = "mirror"
    mirror_repo: str = "hyperi-ci"
    mirror_workflow: str = "mirror-images.yml"
    mirror_renovate_config: str = (
        "https://raw.githubusercontent.com/hyperi-io/renovate-config/HEAD/default.json"
    )
    openbao_addr: str = "https://bao.devex.hyperi.io:8200"
    openbao_jwt_mount: str = "github-actions"
    openbao_audience: str = "https://bao.devex.hyperi.io"

    @property
    def r2_endpoint(self) -> str:
        """S3 API endpoint of the R2 account."""
        return f"https://{self.r2_account_id}.r2.cloudflarestorage.com"

    @property
    def mirror_prefix(self) -> str:
        """Registry path the Docker Hub mirror publishes under."""
        return f"{self.ghcr_registry}/{self.ghcr_org}/{self.mirror_path}"


@dataclass
class CIConfig:
    """Full CI configuration after merging all sources."""

    language: str = "none"

    # Declared repo category, "" when undeclared. Decide on
    # `classification_effective`: an undeclared repo is the most restrictive
    # category, never general-oss.
    classification: str = ""
    classification_source: str = "undeclared"
    classification_effective: str = "internal"

    # Legacy `publish.*` keys in the project's own config, for `hyperi-ci check`.
    deprecated_keys: list[str] = field(default_factory=list)

    _raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def get(self, key: str, default: Any = None) -> Any:
        """Get config value by dot-notation key.

        A ``publish.*`` key resolves to its ``release.*`` equivalent, the
        namespaces having been merged.
        """
        missing = object()
        for candidate in key_candidates(key):
            value: Any = self._raw
            for part in candidate.split("."):
                if isinstance(value, dict) and part in value:
                    value = value[part]
                else:
                    value = missing
                    break
            if value is not missing:
                return value
        return default

    def setting(self, key: str) -> Any:
        """Get a key defaults.yaml declares, its shipped value when unset.

        Use this, never ``get(key, <literal>)``, for any key defaults.yaml
        declares: a literal fallback is a second copy of the default, free to
        disagree with the first. The shipped value answers only where the merged
        config lacks the key -- a hand-built config, or an environment variable
        that replaced a whole section -- because :func:`load_config` always
        starts from defaults.yaml and refuses a project section that is not a
        mapping.

        Args:
            key: Dot-notation key; ``publish.*`` resolves as in :meth:`get`.

        Returns:
            The merged value, else the value defaults.yaml ships.

        Raises:
            KeyError: defaults.yaml does not declare ``key``.

        """
        shipped = shipped_default(key)
        missing = object()
        value = self.get(key, missing)
        return shipped if value is missing else value

    def publish_destinations(self) -> list[dict[str, str]]:
        """Return the destination map to publish to (OSS only)."""
        dest = self.setting("release.destinations")
        dest = dict(dest) if isinstance(dest, dict) else {}
        # Merged, not a fallback, because the defaults always populate
        # `destinations`. Only a project sets `destinations_oss` (the old
        # spelling), so its entry wins.
        legacy = self.get("release.destinations_oss", {})
        if isinstance(legacy, dict):
            dest.update(legacy)
        return [dest] if dest else []

    def destination_for(self, artifact_type: str) -> list[str]:
        """Get publish destination(s) for a specific artifact type.

        A falsy destination (``false`` / ``null`` / empty) is an opt-out, e.g.
        ``release.destinations.python: false`` for a private Python service that
        ships only its GHCR container. The older ``publish.destinations_oss``
        spelling still works.

        Args:
            artifact_type: One of python, npm, cargo, container, binaries, go.

        Returns:
            List of destination identifiers (e.g. ['pypi'], ['ghcr']).

        """
        return [
            dest[artifact_type]
            for dest in self.publish_destinations()
            if artifact_type in dest and dest[artifact_type]
        ]


def warn(message: str) -> None:
    """Log a warning. Lazy, so the pytest plugin can import config without scalo."""
    from hyperi_ci.common import warn as log_warn

    log_warn(message)


def _read_yaml(path: Path) -> Any:
    """Parse a YAML file. None where it is missing or empty."""
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _not_a_mapping(path: str, value: Any, section: dict[str, Any]) -> str:
    """Name a non-mapping value set where defaults.yaml ships a mapping, and its fix."""
    written = f"`{path}: {json.dumps(value, default=str)}` is not a mapping"
    if value is False and "enabled" in section:
        return f"{written} -- to turn it off write `{path}: {{enabled: false}}`"
    keys = ", ".join(sorted(str(key) for key in section))
    return f"{written} -- write a mapping of its keys ({keys}), or delete it"


def _check_sections(
    project: dict[Any, Any],
    shipped: dict[str, Any],
    prefix: str,
    problems: list[str],
) -> dict[Any, Any]:
    """Return ``project`` with its empty sections dropped, noting non-mapping ones.

    A section is a key defaults.yaml ships as a mapping. Left empty it reads as
    absent, so the shipped mapping applies. Any other non-mapping value there
    would leave every key below it on its shipped default whatever the project
    meant, so it goes into ``problems`` for the caller to refuse.

    Args:
        project: One level of the project's own config.
        shipped: The same level of defaults.yaml.
        prefix: Dotted path of this level, ``""`` at the top.
        problems: Collects one message per non-mapping section.

    Returns:
        A copy of ``project`` without the empty sections.

    """
    checked: dict[Any, Any] = {}
    for key, value in project.items():
        path = f"{prefix}{key}"
        # A legacy top-level `publish:` is checked against `release:`.
        name = canonical_key(str(key)) if not prefix else key
        section = shipped.get(name)
        if not isinstance(section, dict):
            checked[key] = value
        elif value is None:
            warn(f"`{path}:` is empty, so the shipped defaults apply")
        elif not isinstance(value, dict):
            problems.append(_not_a_mapping(path, value, section))
        else:
            checked[key] = _check_sections(value, section, f"{path}.", problems)
    return checked


def _parse_env_value(value: str) -> Any:
    """Parse environment variable string to appropriate Python type."""
    if value.lower() in ("true", "yes", "1"):
        return True
    if value.lower() in ("false", "no", "0"):
        return False
    if value.isdigit():
        return int(value)
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _set_nested(config: dict, path: list[str], value: Any) -> None:
    """Set a nested configuration value by path segments."""
    if len(path) == 1:
        config[path[0]] = value
    else:
        if path[0] not in config:
            config[path[0]] = {}
        _set_nested(config[path[0]], path[1:], value)


_config_cache: CIConfig | None = None
_org_cache: OrgConfig | None = None


def load_org_config(*, reload: bool = False) -> OrgConfig:
    """Load organisation config from config/org.yaml."""
    global _org_cache
    if _org_cache is not None and not reload:
        return _org_cache

    raw: dict[str, Any] = _read_yaml(_CONFIG_DIR / "org.yaml") or {}
    github = raw.get("github", {})
    ghcr = raw.get("ghcr", {})
    r2 = raw.get("r2", {})
    mirror = raw.get("mirror", {})
    openbao = raw.get("openbao", {})
    fallback = OrgConfig()

    _org_cache = OrgConfig(
        github_org=os.environ.get("GITHUB_ORG", github.get("org", "hyperi-io")),
        ghcr_registry=ghcr.get("registry", "ghcr.io"),
        ghcr_org=ghcr.get("org", "hyperi-io"),
        r2_bucket=r2.get("bucket", fallback.r2_bucket),
        r2_account_id=r2.get("account_id", fallback.r2_account_id),
        r2_public_url=r2.get("public_url", fallback.r2_public_url),
        mirror_path=mirror.get("path", fallback.mirror_path),
        mirror_repo=mirror.get("repo", fallback.mirror_repo),
        mirror_workflow=mirror.get("workflow", fallback.mirror_workflow),
        mirror_renovate_config=mirror.get(
            "renovate_config", fallback.mirror_renovate_config
        ),
        openbao_addr=openbao.get("addr", fallback.openbao_addr),
        openbao_jwt_mount=openbao.get("jwt_mount", fallback.openbao_jwt_mount),
        openbao_audience=openbao.get("audience", fallback.openbao_audience),
    )
    return _org_cache


_packaged_defaults_cache: dict[str, Any] | None = None


def packaged_default(key: str, default: Any = None) -> Any:
    """Value a dotted key carries in the SHIPPED defaults, before any override.

    The merged config cannot tell a project's setting from ours, so a report on
    an override reads this layer on its own.
    """
    global _packaged_defaults_cache
    if _packaged_defaults_cache is None:
        _packaged_defaults_cache = _read_yaml(_CONFIG_DIR / "defaults.yaml") or {}

    node: Any = _packaged_defaults_cache
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def shipped_default(key: str) -> Any:
    """Value defaults.yaml ships for ``key``, failing loudly where it ships none.

    A copy, so a caller that mutates the result leaves the shipped layer intact.

    Raises:
        KeyError: defaults.yaml does not declare ``key``.

    """
    missing = object()
    for candidate in key_candidates(key):
        value = packaged_default(candidate, missing)
        if value is not missing:
            return copy.deepcopy(value)
    raise KeyError(f"{key} is not declared in src/hyperi_ci/config/defaults.yaml")


def load_config(
    *,
    reload: bool = False,
    project_dir: Path | None = None,
    report_removed: bool = True,
) -> CIConfig:
    """Load and merge CI configuration from all sources.

    Cascade (highest last):
      1. config/defaults.yaml (package defaults)
      2. .hyperi-ci.yaml (project override)
      3. HYPERCI_* environment variables

    Args:
        reload: Force re-read from files.
        project_dir: Project root to search for .hyperi-ci.yaml. Defaults to cwd.
        report_removed: Warn about renamed and removed keys. Off where stdout
            must stay parseable, since a GitHub annotation is written to stdout.

    Returns:
        Merged CIConfig instance.

    Raises:
        ConfigError: The project file is not a mapping, or sets a section
            defaults.yaml ships as a mapping to a non-mapping value. An empty
            section warns and takes the shipped defaults instead.

    """
    global _config_cache
    if _config_cache is not None and not reload:
        return _config_cache

    config: dict[str, Any] = _read_yaml(_CONFIG_DIR / "defaults.yaml") or {}
    project_dir = project_dir or Path.cwd()

    deprecated_keys: list[str] = []
    removed_keys: list[str] = []
    carried_keys: list[str] = []
    config_file = find_config(project_dir)
    if config_file is not None:
        name = config_file.name
        loaded = _read_yaml(config_file)
        if loaded and not isinstance(loaded, dict):
            raise ConfigError(f"{name}: the file must be a mapping of settings")
        if loaded:
            problems: list[str] = []
            checked = _check_sections(loaded, config, "", problems)
            if problems:
                raise ConfigError("\n".join(f"{name}: {p}" for p in problems))
            removed_keys = find_removed_keys(loaded)
            # Before the merge: the shipped successor default would
            # otherwise read as a project setting.
            checked, carried_keys = carry_over_removed_gates(checked)
            # Folded before the merge, or a shipped `release.x` default
            # would outrank a project's `publish.x`.
            folded, deprecated_keys = fold_legacy_config(checked)
            deprecated_keys = drop_removed_notices(deprecated_keys, loaded)
            config = merge_deep(config, folded)

    for key, value in os.environ.items():
        if key.startswith("HYPERCI_"):
            path = key[8:].lower().split("_")
            # Env beats the file, so a legacy path is rewritten, not folded
            # (folding lets the canonical side win).
            if path and path[0] == LEGACY_CONFIG_NAMESPACE:
                path = [CONFIG_NAMESPACE, *path[1:]]
            _set_nested(config, path, _parse_env_value(value))

    if report_removed:
        report_deprecated_config(deprecated_keys)
        report_removed_keys(removed_keys)
        report_carry_overs(carried_keys)

    # Warn, not fail: project.status is information-only.
    project = config.get("project", {})
    if isinstance(project, dict):
        status = str(project.get("status") or "").strip().lower()
        if status and status not in VALID_PROJECT_STATUSES:
            warn(
                f"Unknown project.status '{status}' -- expected one of "
                f"{', '.join(VALID_PROJECT_STATUSES)} (or unset). "
                f"Treating as unset for logging purposes."
            )

    resolved = _resolve_classification(config, project_dir)

    # Written back so `hyperi-ci config` (and its --json form) shows the
    # resolved category and the marker that answered. Popped first so the three
    # keys print together.
    config.pop("classification", None)
    config["classification"] = resolved.value
    config["classification_source"] = resolved.source
    config["classification_effective"] = resolved.effective

    _config_cache = CIConfig(
        # "none" is the hardcoded last layer, for a package missing defaults.yaml.
        language=config.get("language", "none"),
        classification=resolved.value,
        classification_source=resolved.source,
        classification_effective=resolved.effective,
        deprecated_keys=deprecated_keys,
        _raw=config,
    )
    return _config_cache


def _resolve_classification(
    config: dict[str, Any],
    project_dir: Path,
) -> classification.Resolution:
    """Resolve the declared repo category, warning on an unusable marker.

    An invalid or unreadable marker is reported and treated as undeclared, so a
    typo downgrades to the most restrictive category.

    Args:
        config: The merged config dict.
        project_dir: Repo root to look for the dotfile in.

    Returns:
        The resolved classification.

    """
    try:
        return classification.resolve(config, project_dir)
    except (ValueError, OSError) as exc:
        warn(
            f"{exc} Treating this repo as '{classification.MOST_RESTRICTIVE}' "
            f"until the marker is fixed."
        )
        return classification.Resolution(
            "",
            classification.SOURCE_UNDECLARED,
            classification.MOST_RESTRICTIVE,
        )
