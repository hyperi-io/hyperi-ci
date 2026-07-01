# Project:   HyperI CI
# File:      src/hyperi_ci/deployment/topology/resolve.py
# Purpose:   Resolve chart version ranges against OCI registry contents
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""OCI chart version resolution.

The stitcher needs to turn each ``apps[].version: "^1.18"`` semver range
into a concrete version (Helm sub-charts don't honour ranges at install
time). This module:

1. Queries the OCI registry for available chart versions.
2. Picks the highest version that satisfies the declared range.
3. Returns the concrete version for the stitcher to pin in Chart.yaml.

:class:`ChartVersionResolver` takes a dict of pre-fetched available
versions for testability; :func:`resolve_versions` provides the
end-to-end resolution by querying OCI via the ORAS client.

Error type comes from scalo: ``scalo.deployment.topology.errors.VersionResolutionError``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from scalo.deployment.topology.errors import VersionResolutionError

# Indirected for test monkeypatching; real binding is oras.client.OrasClient
try:
    from oras.client import OrasClient as _OrasClient
except ImportError:  # pragma: no cover
    _OrasClient = None  # type: ignore[assignment,misc]  # ty: ignore[invalid-assignment]


@dataclass
class ChartVersionResolver:
    """Resolve chart version ranges against a known catalog.

    Attributes:
        registry: OCI registry URL (informational; not queried by this class).
        available: ``{chart-name: [versions]}`` catalog of known versions.

    """

    registry: str
    available: dict[str, list[str]] = field(default_factory=dict)

    def resolve(self, chart: str, version_range: str) -> str:
        """Resolve a semver range to a concrete version.

        Args:
            chart: Chart name (e.g. "dfe-loader").
            version_range: Semver range (e.g. "^1.18", "1.18.0", ">=1.0,<2.0").

        Returns:
            Highest version string that satisfies the range.

        Raises:
            VersionResolutionError: chart not in catalog OR no version matches.

        """
        versions = self.available.get(chart)
        if not versions:
            raise VersionResolutionError(
                "chart not found in registry",
                chart=chart,
                version_range=version_range,
            )

        # Filter to parseable, non-prerelease versions
        parsed: list[Version] = []
        for v in versions:
            try:
                pv = Version(v)
            except InvalidVersion:
                continue
            if pv.is_prerelease:
                continue
            parsed.append(pv)

        if not parsed:
            raise VersionResolutionError(
                "no stable versions found",
                chart=chart,
                version_range=version_range,
            )

        spec = _to_specifier(version_range)
        matches = [v for v in parsed if v in spec]
        if not matches:
            raise VersionResolutionError(
                f"no version matches range; available: {[str(v) for v in parsed]}",
                chart=chart,
                version_range=version_range,
            )

        return str(max(matches))


def _to_specifier(version_range: str) -> SpecifierSet:
    """Convert a topology version expression to PEP 440 SpecifierSet.

    Accepts ``^X.Y[.Z]`` (Helm/SemVer style) and PEP 440 styles
    (``>=X,<Y``, ``X.Y.Z``).
    """
    s = version_range.strip()
    if s.startswith("^"):
        rest = s[1:]
        try:
            base = Version(rest)
        except InvalidVersion as exc:
            raise VersionResolutionError(
                f"unparseable version range {version_range!r}",
                chart="",
                version_range=version_range,
            ) from exc
        next_major = Version(f"{base.major + 1}.0.0")
        return SpecifierSet(f">={base},<{next_major}")
    if s.startswith("~"):
        rest = s[1:]
        try:
            base = Version(rest)
        except InvalidVersion as exc:
            raise VersionResolutionError(
                f"unparseable version range {version_range!r}",
                chart="",
                version_range=version_range,
            ) from exc
        next_minor = Version(f"{base.major}.{base.minor + 1}.0")
        return SpecifierSet(f">={base},<{next_minor}")
    # Treat bare versions as exact; PEP 440 needs "==" prefix
    if all(part.isdigit() for part in s.split(".")):
        return SpecifierSet(f"=={s}")
    # Otherwise pass through (e.g. ">=1.0,<2.0")
    try:
        return SpecifierSet(s)
    except InvalidSpecifier as exc:
        raise VersionResolutionError(
            f"unparseable version range {version_range!r}",
            chart="",
            version_range=version_range,
        ) from exc


def resolve_versions(
    *,
    registry: str,
    charts: dict[str, str],
) -> dict[str, str]:
    """Resolve every chart's version range against the OCI registry.

    Args:
        registry: OCI registry URL.
        charts: ``{chart-name: version-range}``.

    Returns:
        ``{chart-name: concrete-version}``.

    Raises:
        VersionResolutionError: any chart fails to resolve.

    """
    available = _fetch_available(registry, list(charts.keys()))
    resolver = ChartVersionResolver(registry=registry, available=available)
    return {chart: resolver.resolve(chart, rng) for chart, rng in charts.items()}


def _fetch_available(registry: str, chart_names: list[str]) -> dict[str, list[str]]:
    """Query OCI for available versions of each chart via ORAS.

    Args:
        registry: ``oci://`` registry URL (e.g.
            ``oci://ghcr.io/hyperi-io/helm-charts``).
        chart_names: Names to look up under that registry.

    Returns:
        ``{chart-name: [versions]}``. Charts that 404 produce empty lists.

    """
    if _OrasClient is None:
        raise VersionResolutionError(
            "oras-py not installed; `uv add oras`",
            chart=chart_names[0] if chart_names else "",
            version_range="",
        )

    # Strip oci:// prefix; ORAS uses bare host/path
    base = registry.removeprefix("oci://")
    client = _OrasClient()  # auth via standard docker creds chain

    out: dict[str, list[str]] = {}
    for chart in chart_names:
        repo = f"{base}/{chart}"
        try:
            tags = client.get_tags(repo)
        except Exception:  # noqa: BLE001
            # Treat any failure (404, network error, auth) as no versions
            out[chart] = []
            continue
        out[chart] = list(tags)
    return out
