# Project:   HyperI CI
# File:      src/hyperi_ci/release/charts.py
# Purpose:   Package committed Helm charts and push them to an OCI registry
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Publish a project's committed Helm charts to an OCI registry.

Each chart is copied to a scratch directory with its ``file://`` dependencies at
the same relative paths, so ``helm dependency build`` and ``helm package`` never
write the checkout. The chart version is the release version, and its
``appVersion`` is left as committed unless the chart has none. A glob skips
library charts, and a library chart named by its exact directory is published.

A version already in the registry is reported, never re-pushed: ``helm package``
is not byte-reproducible, so a re-push would move the tag to a new digest.
"""

import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml

from hyperi_ci.common import (
    ReleaseVersionError,
    error,
    info,
    resolve_release_version,
    run_cmd,
    success,
    warn,
)
from hyperi_ci.config import CIConfig
from hyperi_ci.native_tools import install_tool
from hyperi_ci.repo_path import RepoPathError, confine

DIGEST_RE = re.compile(r"^Digest:\s*(sha256:[0-9a-f]{64})\s*$", re.MULTILINE)

NOTES_FILE = "hyperi-ci-chart-digests.md"

# Local registries serve no TLS, and helm will not guess that.
_PLAIN_HTTP = frozenset({"localhost", "127.0.0.1"})


class ChartError(ValueError):
    """A configured chart path or a chart's own metadata cannot be used."""


@dataclass(frozen=True, slots=True)
class Chart:
    """One chart to publish."""

    path: Path
    name: str
    app_version: str | None
    has_deps: bool
    file_deps: tuple[Path, ...]


@dataclass(slots=True)
class Published:
    """What a chart publish produced, as ``--output json`` reports it."""

    chart: str
    version: str
    digest: str | None
    ref: str | None
    signed: bool = False


def _read_chart(path: Path) -> dict:
    manifest = path / "Chart.yaml"
    try:
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ChartError(f"cannot read {manifest}: {exc}") from exc
    if not isinstance(data, dict) or not data.get("name"):
        raise ChartError(f"{manifest} has no chart name")
    return data


def _file_deps(path: Path, data: dict, root: Path) -> list[Path]:
    """Return every ``file://`` dependency dir, transitively, held inside ``root``."""
    found: list[Path] = []
    for dep in data.get("dependencies") or []:
        repo = str(dep.get("repository", "")) if isinstance(dep, dict) else ""
        if not repo.startswith("file://"):
            continue
        target = confine(path / repo.removeprefix("file://"), root, key="file://")
        if target not in found:
            found.append(target)
            found += [
                p
                for p in _file_deps(target, _read_chart(target), root)
                if p not in found
            ]
    return found


def resolve_charts(entries: list[str], root: Path) -> list[Chart]:
    """Expand chart dirs and globs into the charts to publish.

    A glob skips library charts, so ``charts/*`` never publishes a helper
    library by accident. A directory named exactly is published whatever its
    type, which is how a library chart ships.

    Raises:
        ChartError: An entry escapes ``root``, an explicit directory holds no
            Chart.yaml, or nothing at all matches.

    """
    root = root.resolve()
    charts: list[Chart] = []
    for entry in entries:
        is_glob = any(c in entry for c in "*?[")
        if is_glob:
            matches = [
                p for p in sorted(root.glob(entry)) if (p / "Chart.yaml").is_file()
            ]
        elif (root / entry / "Chart.yaml").is_file():
            matches = [root / entry]
        else:
            raise ChartError(f"{entry} has no Chart.yaml")
        for match in matches:
            try:
                path = confine(match, root, key="charts")
                data = _read_chart(path)
                deps = tuple(_file_deps(path, data, root))
            except RepoPathError as exc:
                raise ChartError(str(exc)) from exc
            if any(c.path == path for c in charts):
                continue
            if is_glob and data.get("type") == "library":
                info(f"  {path.relative_to(root)}: library chart, not published")
                continue
            app_version = data.get("appVersion")
            charts.append(
                Chart(
                    path=path,
                    name=str(data["name"]),
                    app_version=str(app_version) if app_version else None,
                    has_deps=bool(data.get("dependencies")),
                    file_deps=deps,
                )
            )
    if not charts:
        raise ChartError(f"no application chart matches {entries}")
    return charts


def _stage(chart: Chart, root: Path, scratch: Path) -> Path:
    """Copy a chart and its ``file://`` targets to their relative paths in ``scratch``."""
    for source in (chart.path, *chart.file_deps):
        for link in (p for p in source.rglob("*") if p.is_symlink()):
            confine(link, root, key=f"symlink in {source.relative_to(root)}")
        shutil.copytree(
            source,
            scratch / source.relative_to(root),
            symlinks=True,
            dirs_exist_ok=True,
        )
    return scratch / chart.path.relative_to(root)


def _helm(*args: str, registry: str = "") -> tuple[int, str]:
    """Run helm with stdout and stderr merged, so its output reaches the logger."""
    flags = ["--plain-http"] if registry and _host(registry) in _PLAIN_HTTP else []
    result = run_cmd(
        ["helm", *args, *flags], check=False, capture=True, merge_stderr=True
    )
    return result.returncode, result.stdout or ""


def _host(registry: str) -> str:
    return urlparse(registry).hostname or ""


def package(chart: Chart, version: str, root: Path, scratch: Path) -> Path:
    """Package ``chart`` at ``version`` in ``scratch``, leaving the checkout untouched.

    Raises:
        ChartError: helm failed, or the chart symlinks out of ``root``.

    """
    try:
        staged = _stage(chart, root, scratch)
    except RepoPathError as exc:
        raise ChartError(str(exc)) from exc
    if chart.has_deps:
        rc, out = _helm("dependency", "build", str(staged))
        if rc != 0:
            raise ChartError(f"helm dependency build {chart.name} failed:\n{out}")
    out_dir = scratch / ".packaged" / chart.name
    args = ["package", str(staged), "--version", version, "-d", str(out_dir)]
    if not chart.app_version:
        args += ["--app-version", f"v{version}"]
    rc, out = _helm(*args)
    tgz = next(out_dir.glob("*.tgz"), None)
    if rc != 0 or tgz is None:
        raise ChartError(f"helm package {chart.name} failed:\n{out}")
    return tgz


def existing_digest(registry: str, name: str, version: str) -> str | None:
    """Return the digest ``name:version`` already has in the registry, else None."""
    rc, out = _helm(
        "show", "chart", f"{registry}/{name}", "--version", version, registry=registry
    )
    match = DIGEST_RE.search(out) if rc == 0 else None
    return match.group(1) if match else None


def is_new_package(registry: str, name: str) -> bool:
    """Report whether the registry holds no version of ``name`` at all."""
    rc, _ = _helm("show", "chart", f"{registry}/{name}", "--devel", registry=registry)
    return rc != 0


def push(tgz: Path, registry: str) -> str:
    """Push a packaged chart and return its digest.

    Raises:
        ChartError: The push failed or reported no digest.

    """
    rc, out = _helm("push", str(tgz), registry, registry=registry)
    match = DIGEST_RE.search(out)
    if rc != 0 or match is None:
        raise ChartError(f"helm push {tgz.name} failed:\n{out}")
    return match.group(1)


def _login(registry: str) -> None:
    """Log helm in to GHCR with the job token, when there is one.

    Raises:
        ChartError: The login was refused.

    """
    token = os.environ.get("GITHUB_TOKEN", "")
    if _host(registry) != "ghcr.io" or not token:
        return
    user = os.environ.get("GITHUB_ACTOR") or "github-actions"
    result = run_cmd(
        ["helm", "registry", "login", "ghcr.io", "-u", user, "--password-stdin"],
        check=False,
        capture=True,
        merge_stderr=True,
        stdin_text=token,
    )
    if result.returncode != 0:
        raise ChartError(f"helm registry login ghcr.io failed:\n{result.stdout}")


def _ensure_helm() -> bool:
    if shutil.which("helm"):
        return True
    bin_dir = install_tool("helm")
    if bin_dir is None:
        return False
    os.environ["PATH"] = os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")])
    return True


def digest_table(results: list[Published]) -> str:
    """Render the chart | version | digest markdown table."""
    rows = [f"| {r.chart} | {r.version} | `{r.digest}` |" for r in results]
    return "\n".join(["| Chart | Version | Digest |", "|---|---|---|", *rows])


def notes_path() -> Path | None:
    """Where the digest table waits for the GitHub Release body, in CI only."""
    runner_temp = os.environ.get("RUNNER_TEMP")
    return Path(runner_temp) / NOTES_FILE if runner_temp else None


def release_notes() -> str | None:
    """Return the chart section of the GitHub Release body, if any was written."""
    path = notes_path()
    if path is None or not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace").strip() or None


def _report(results: list[Published], registry: str, new: list[str]) -> None:
    table = digest_table(results)
    info(f"Helm charts in {registry}:\n{table}")
    section = f"## Helm charts\n\nRegistry: `{registry}`\n\n{table}\n"
    # Append to both: one job can publish committed and assembled charts in two calls.
    for target in (os.environ.get("GITHUB_STEP_SUMMARY"), notes_path()):
        if target:
            with Path(target).open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(section)
    if new:
        warn(
            f"First push of {', '.join(new)}: a new GHCR package starts private. "
            "Make it public in the package settings if consumers pull anonymously."
        )


def _assembled(
    config: CIConfig,
    root: Path,
    out: Path,
    *,
    image: str,
    registry: str,
    version: str,
) -> Chart:
    """Assemble the ``release.helm.contract`` chart in ``out``, its library built in.

    Raises:
        ChartError: The contract cannot be read or the chart not assembled.

    """
    from hyperi_ci.release.assemble import assemble_chart, release_contract

    raw = release_contract(str(config.get("release.helm.contract")), root)
    rc, built = assemble_chart(
        config,
        root,
        image=image,
        output_dir=out,
        registry=registry,
        version=version,
        contract=raw,
    )
    if rc != 0 or built is None:
        raise ChartError("the release.helm.contract chart was not assembled")
    app_version = _read_chart(built).get("appVersion")
    return Chart(
        path=built,
        name=built.name,
        app_version=str(app_version) if app_version else None,
        has_deps=False,
        file_deps=(),
    )


def publish_charts(
    config: CIConfig,
    root: Path,
    *,
    charts: list[str] | None = None,
    registry: str | None = None,
    version: str | None = None,
    image: str | None = None,
    dry_run: bool = False,
) -> tuple[int, list[Published]]:
    """Package every configured chart and push it, reusing any version already there.

    Flags beat config. With no ``charts`` flag, ``release.helm.enabled`` decides
    whether anything runs at all, and a set ``release.helm.contract`` adds the
    chart assembled from it, pinned to ``image``. That chart is never built
    without a pushed image to pin.

    Returns:
        ``(exit code, results)``. A dry run packages but pushes nothing, so
        its results carry no digest.

    """
    contract = None
    if charts is None:
        if not config.get("release.helm.enabled", False):
            info("release.helm.enabled is false -- no Helm charts to publish")
            return 0, []
        charts = list(config.get("release.helm.charts") or [])
        contract = config.get("release.helm.contract") or None
    if contract and not image:
        error(
            "release.helm.contract is set, but no pushed image reached "
            "publish-charts. The chart pins its image by digest, so a release "
            "that pushed no container has nothing to pin. Pass --image "
            "<repo>:<tag>@sha256:<digest> or set HYPERCI_CHART_IMAGE."
        )
        return 1, []
    registry = (registry or config.get("release.helm.registry") or "").rstrip("/")
    if not registry.startswith("oci://"):
        error(f"Helm registry must be an oci:// URL, got {registry!r}")
        return 1, []
    try:
        version = version or resolve_release_version()
    except ReleaseVersionError as exc:
        error(str(exc))
        return 1, []
    if not version:
        error("No release version -- pass --version or set HYPERCI_VERSION")
        return 1, []
    if not _ensure_helm():
        return 1, []

    results: list[Published] = []
    new: list[str] = []
    try:
        # A contract alone is a complete chart list.
        resolved = resolve_charts(charts, root) if charts or not contract else []
        if not dry_run:
            _login(registry)
        with (
            tempfile.TemporaryDirectory(prefix="hyperi-ci-charts-") as scratch,
            tempfile.TemporaryDirectory(prefix="hyperi-ci-assembled-") as built,
        ):
            targets = [(chart, root.resolve()) for chart in resolved]
            if contract and image:
                chart = _assembled(
                    config,
                    root,
                    Path(built),
                    image=image,
                    registry=registry,
                    version=version,
                )
                if any(c.name == chart.name for c in resolved):
                    raise ChartError(
                        f"{chart.name} is both a committed chart and the "
                        "release.helm.contract chart"
                    )
                targets.append((chart, Path(built).resolve()))
            for chart, base in targets:
                tgz = package(chart, version, base, Path(scratch))
                if dry_run:
                    info(f"  {chart.name} {version}: packaged {tgz.name}, not pushed")
                    results.append(Published(chart.name, version, None, None))
                    continue
                digest = existing_digest(registry, chart.name, version)
                if digest:
                    info(
                        f"  {chart.name} {version} already in {registry}, not re-pushed"
                    )
                else:
                    if _host(registry) == "ghcr.io" and is_new_package(
                        registry, chart.name
                    ):
                        new.append(chart.name)
                    digest = push(tgz, registry)
                ref = f"{registry.removeprefix('oci://')}/{chart.name}@{digest}"
                results.append(Published(chart.name, version, digest, ref))
    except ChartError as exc:
        error(str(exc))
        return 1, results

    if not dry_run:
        _report(results, registry, new)
        success(f"Published {len(results)} Helm chart(s) to {registry}")
    return 0, results


def as_json(results: list[Published]) -> list[dict]:
    """Return results as the list ``--output json`` prints."""
    return [asdict(r) for r in results]
