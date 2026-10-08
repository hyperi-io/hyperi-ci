# Project:   HyperI CI
# File:      src/hyperi_ci/release/binaries.py
# Purpose:   Language-agnostic binary artifact publishing
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Upload pre-built ``dist/`` artefacts to GitHub Releases and Cloudflare R2.

Language-agnostic: runs after the language release handler for any project
that packages binaries into ``dist/``.
"""

import filecmp
import os
import re
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from hyperi_ci.common import (
    error,
    group,
    holds_latest,
    info,
    mask,
    resolve_release_version,
    run_cmd,
    skip_optimize,
    success,
    warn,
)
from hyperi_ci.config import CIConfig, load_org_config
from hyperi_ci.native_deps import ensure_aws_cli
from hyperi_ci.release.charts import release_notes as chart_release_notes
from hyperi_ci.release_branches import effective_release_channel
from hyperi_ci.stamp import repo_paths
from hyperi_ci.tools import missing_tool_notice

VALID_CHANNELS = ("alpha", "beta", "release")

CHANGELOG_FILE = "CHANGELOG.md"

# A rendered release heading: `## [1.2.3](compare-url) (date)` for a patch,
# `# [1.3.0](...)` for a minor or major. A `### Bug Fixes` sub-heading inside
# an entry does not match, so the next match is the next release.
_ENTRY_HEADING = re.compile(r"^#{1,3}\s+\[?v?(\d+\.\d+\.\d+[^\]\s]*)\]?")


def _resolve_gh_release_flags(channel: str) -> list[str]:
    """Return extra flags for gh release create based on channel."""
    if channel != "release":
        return ["--prerelease"]
    return []


def _latest_flags(version: str) -> list[str]:
    """Return ``--latest=false`` when an older version is being released.

    GitHub marks a newly published release Latest unless told otherwise, so
    back-filling an old tag would take the flag off the newest release.
    """
    if holds_latest(version, "the GitHub Release Latest flag"):
        return ["--latest=false"]
    return []


def _resolve_channel(config: CIConfig, version: str | None) -> str:
    """Return the channel this version actually ships on.

    A prerelease version overrides ``release.channel`` with its own label, or
    a ``1.2.0-beta.1`` would overwrite the GA ``latest/`` on R2 (issue #144).
    """
    configured = config.setting("release.channel")
    resolved = effective_release_channel(configured, version)
    if resolved != configured:
        info(f"Prerelease version {version} -- publishing on channel {resolved}")
    return resolved


def _resolve_r2_paths(project_name: str, version: str, channel: str) -> tuple[str, str]:
    """Return (versioned_prefix, latest_prefix) S3 paths for R2."""
    bucket = load_org_config().r2_bucket
    if channel == "release":
        versioned = f"s3://{bucket}/{project_name}/v{version}/"
        latest = f"s3://{bucket}/{project_name}/latest/"
    else:
        versioned = f"s3://{bucket}/{project_name}/{channel}/v{version}/"
        latest = f"s3://{bucket}/{project_name}/{channel}/latest/"
    return versioned, latest


_PYTHON_DIST_SUFFIXES = (".whl", ".tar.gz", ".zip")


def _is_python_dist_artifact(path: Path) -> bool:
    """Return True for a Python packaging artefact (wheel or sdist).

    Only consulted when the python destination is opted out, so a Rust or Go
    ``.tar.gz`` matching the sdist suffix is never dropped.
    """
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in _PYTHON_DIST_SUFFIXES)


def _release_targets_head(tag: str) -> bool:
    """Return True iff the git tag for an existing release points at HEAD.

    A release at another commit means a stale version resolved (issue #105).
    An unresolvable tag counts as a mismatch, so the caller refuses.
    """
    tag_commit = run_cmd(
        ["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}"],
        capture=True,
        check=False,
    )
    if tag_commit.returncode != 0 or not tag_commit.stdout.strip():
        return False
    head_commit = run_cmd(
        ["git", "rev-parse", "HEAD^{commit}"],
        capture=True,
        check=False,
    )
    if head_commit.returncode != 0 or not head_commit.stdout.strip():
        return False
    return tag_commit.stdout.strip() == head_commit.stdout.strip()


def _top_changelog_entry(version: str, changelog: str) -> str | None:
    """Return the topmost changelog entry, heading included.

    Returns None unless that heading names ``version``. A retroactive
    publish checks out a tag whose CHANGELOG.md stops at the previous
    release, and those notes describe that release, not this one.
    """
    lines = changelog.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        match = _ENTRY_HEADING.match(line)
        if match is None:
            continue
        if start is None:
            if match.group(1) != version:
                return None
            start = index
            continue
        return "\n".join(lines[start:index]).strip() or None
    if start is None:
        return None
    return "\n".join(lines[start:]).strip() or None


UNOPTIMIZED_RELEASE_BANNER = (
    "> **Built without the optimisation stage.** No PGO and no BOLT ran for "
    "this release, so its binaries are slower than a standard release of the "
    "same code. Cut deliberately for a fast deploy-and-test cycle. Do not use "
    "it to measure performance, and prefer a later optimised release for "
    "anything long-lived."
)


@contextmanager
def _release_notes_flags(version: str, config: CIConfig) -> Iterator[list[str]]:
    """Yield gh flags carrying the release body.

    The body is the unoptimised-build banner, the matching CHANGELOG.md entry
    and the chart digest table, in that order, above GitHub's generated
    notes. Yields no flags when all three are empty.

    Args:
        version: Version being released, matched against the changelog.
        config: Merged CI config. The banner reads ``build.skip_optimize``
            from it, as the build and image label do, so all three agree.

    """
    changelog = Path(CHANGELOG_FILE)
    entry = None
    if changelog.is_file():
        entry = _top_changelog_entry(
            version, changelog.read_text(encoding="utf-8", errors="replace")
        )
    banner = UNOPTIMIZED_RELEASE_BANNER if skip_optimize(config) else None
    sections = [s for s in (banner, entry, chart_release_notes()) if s]
    if not sections:
        yield []
        return
    with tempfile.NamedTemporaryFile(
        "w", suffix=".md", delete=False, encoding="utf-8", newline="\n"
    ) as handle:
        handle.write("\n\n".join(sections) + "\n")
        notes_file = handle.name
    try:
        yield ["--notes-file", notes_file]
    finally:
        Path(notes_file).unlink(missing_ok=True)


def _collect_artifacts(exclude_python: bool = False) -> list[Path]:
    """Return the sorted, non-hidden regular files in dist/.

    ``exclude_python`` drops wheels and sdists (issue #105).
    """
    dist = Path("dist")
    if not dist.is_dir():
        return []
    files = [
        f
        for f in sorted(dist.iterdir())
        if f.is_file() and not f.is_symlink() and not f.name.startswith(".")
    ]
    if exclude_python:
        files = [f for f in files if not _is_python_dist_artifact(f)]
    return files


def _release_asset_paths(config: CIConfig) -> tuple[list[Path], str | None]:
    """Resolve `release.assets` to real files, or name what is wrong.

    Returns:
        ``(paths, problem)``. ``problem`` is None when every listed file exists;
        otherwise it names the first offender and ``paths`` is empty.

    """
    assets = config.setting("release.assets") or []
    if isinstance(assets, str):
        assets = [assets]

    # The upload holds every publish credential, so an absolute or escaping
    # path could publish something like /proc/self/environ.
    try:
        names = repo_paths(assets, Path.cwd(), "release.assets")
    except ValueError as exc:
        return [], str(exc)

    paths: list[Path] = []
    for entry in names:
        source = Path(entry)
        if source.is_symlink():
            return [], f"release.assets: {source} is a symlink"
        if not source.exists():
            return [], f"release.assets: {source} does not exist"
        if not source.is_file():
            return [], f"release.assets: {source} is not a file"
        paths.append(source)
    return paths, None


def _upload_release_assets(tag: str, assets: list[Path]) -> int:
    """Attach assets to a release that already exists, for the idempotent re-run."""
    if not assets:
        return 0

    cmd = ["gh", "release", "upload", tag, "--clobber"]
    cmd.extend(str(path) for path in assets)
    result = run_cmd(cmd, check=False, capture=True)
    if result.returncode != 0:
        error(f"Failed to attach release.assets to {tag}")
        if result.stderr:
            error(result.stderr)
        return result.returncode

    success(f"Attached {len(assets)} release asset(s) to {tag}")
    return 0


def stage_release_assets(config: CIConfig) -> int:
    """Copy `release.assets` entries into dist/ so they also reach R2.

    A missing file fails the release, because a catalogue pin against an
    absent asset breaks (issue #125).

    Returns:
        Exit code (0 = success).

    """
    paths, problem = _release_asset_paths(config)
    if problem:
        error(f"{problem} -- refusing to release")
        return 1
    if not paths:
        return 0

    dist = Path("dist")
    dist.mkdir(parents=True, exist_ok=True)

    for source in paths:
        target = dist / source.name
        if target.exists() and not filecmp.cmp(source, target, shallow=False):
            error(
                f"release.assets: dist/{source.name} already exists with different "
                f"content -- rename the asset rather than clobbering a built artefact"
            )
            return 1

        shutil.copy2(source, target)
        info(f"  staged {source} -> dist/{source.name}")

    success(f"Staged {len(paths)} release asset(s) into dist/")
    return 0


def create_github_release(config: CIConfig) -> int:
    """Create a GitHub Release for the current version, with `release.assets`.

    Assets attach here rather than in :func:`publish_binaries` so they reach
    the release whatever the `binaries` destination is (issue #125).

    Returns:
        Exit code (0 = success).

    """
    version = resolve_release_version()
    if not version:
        error("No VERSION file -- cannot determine release tag")
        return 1

    assets, problem = _release_asset_paths(config)
    if problem:
        error(f"{problem} -- refusing to release")
        return 1

    channel = _resolve_channel(config, version)
    tag = f"v{version}"
    latest_flags = _latest_flags(version)

    info(f"Creating GitHub Release {tag}")
    with _release_notes_flags(version, config) as notes_flags:
        cmd = ["gh", "release", "create", tag, "--title", tag, "--generate-notes"]
        cmd.extend(notes_flags)
        cmd.extend(_resolve_gh_release_flags(channel))
        cmd.extend(latest_flags)
        cmd.extend(str(path) for path in assets)
        result = run_cmd(cmd, check=False, capture=True)
    if result.returncode != 0:
        if "already exists" in result.stderr:
            # Re-running at the tag's commit is idempotent; any other commit
            # means a stale version resolved and would overwrite `latest` (#105).
            if _release_targets_head(tag):
                info(f"  GH Release {tag} already exists at HEAD -- idempotent re-run")
                return _upload_release_assets(tag, assets)
            error(
                f"GH Release {tag} already exists at a commit other than HEAD -- "
                f"refusing to overwrite a shipped release (issue #105). A bare "
                f"dispatch or a stale manifest seed resolved an old version; ship "
                f"a new version instead of re-publishing {tag}."
            )
            return 1
        error("GitHub Release creation failed")
        if result.stderr:
            error(result.stderr)
        return result.returncode

    success(f"Created GitHub Release {tag}")
    return 0


def _upload_binaries_github(
    config: CIConfig, channel: str = "release", exclude_python: bool = False
) -> int:
    """Create the GitHub Release and upload built binaries to it.

    Uploads onto an existing release only when its tag is at HEAD (#105).

    Args:
        config: Merged CI config, which the release body reads.
        channel: Channel the version ships on.
        exclude_python: Drop wheels and sdists from the upload.

    Returns:
        Exit code (0 = success).

    """
    artifacts = _collect_artifacts(exclude_python=exclude_python)
    if not artifacts:
        warn("No artifacts found in dist/ -- skipping GitHub Release upload")
        return 0

    version = resolve_release_version()
    if not version:
        error("No VERSION file -- cannot determine release tag")
        return 1

    tag = f"v{version}"
    latest_flags = _latest_flags(version)
    info(f"Publishing {len(artifacts)} artifact(s) to GitHub Release {tag}")

    with _release_notes_flags(version, config) as notes_flags:
        cmd = ["gh", "release", "create", tag, "--title", tag, "--generate-notes"]
        cmd.extend(notes_flags)
        cmd.extend(_resolve_gh_release_flags(channel))
        cmd.extend(latest_flags)
        cmd.extend(str(f) for f in artifacts)
        result = run_cmd(cmd, check=False, capture=True)
    if result.returncode != 0:
        if "already exists" in result.stderr:
            if not _release_targets_head(tag):
                error(
                    f"GH Release {tag} already exists at a commit other than "
                    f"HEAD -- refusing to clobber its assets (issue #105)."
                )
                return 1
            info(f"  GH Release {tag} already exists at HEAD -- uploading artifacts")
            upload_cmd = ["gh", "release", "upload", tag, "--clobber"]
            upload_cmd.extend(str(f) for f in artifacts)
            result = run_cmd(upload_cmd, check=False)
            if result.returncode != 0:
                error("GitHub Release upload failed")
                return result.returncode
        else:
            error("GitHub Release creation failed")
            if result.stderr:
                error(result.stderr)
            return result.returncode

    success(f"Published {len(artifacts)} artifact(s) to GitHub Release {tag}")
    return 0


def _publish_r2_binaries(channel: str = "release", exclude_python: bool = False) -> int:
    """Publish dist/ to the R2 versioned prefix and, usually, ``latest/``.

    ``latest/`` is left alone when a higher stable ``v*`` tag exists, so an
    older tag cannot move it backwards. Skips with a warning when
    R2_ACCESS_KEY_ID or R2_SECRET_ACCESS_KEY is unset.

    Returns:
        Exit code (0 = success).

    """
    access_key = os.environ.get("R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")
    if not access_key or not secret_key:
        warn(
            "R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY not set -- skipping R2 binary publish"
        )
        return 0

    mask(secret_key)

    # Installed on demand: reaching R2 is a trigger no manifest pattern expresses.
    if not ensure_aws_cli():
        error(missing_tool_notice("aws"))
        return 1

    artifacts = _collect_artifacts(exclude_python=exclude_python)
    if not artifacts:
        warn("No artifacts found in dist/ -- skipping R2 binary publish")
        return 0

    org = load_org_config()
    endpoint = org.r2_endpoint
    project_name = Path.cwd().name
    version = resolve_release_version() or "unknown"
    public_url = f"{org.r2_public_url}/{project_name}/v{version}/"

    versioned_prefix, latest_prefix = _resolve_r2_paths(project_name, version, channel)
    move_latest = not holds_latest(version, f"R2 {latest_prefix}")

    aws_env = {
        "AWS_ACCESS_KEY_ID": access_key,
        "AWS_SECRET_ACCESS_KEY": secret_key,
        "AWS_DEFAULT_REGION": "auto",
    }

    info(f"Publishing to R2: {public_url}")

    destinations = [("versioned", versioned_prefix)]
    if move_latest:
        # A renamed binary would otherwise linger in latest/ beside its successor.
        info(f"  Cleaning latest/: {latest_prefix}")
        rm_result = run_cmd(
            [
                "aws",
                "s3",
                "rm",
                latest_prefix,
                "--recursive",
                "--endpoint-url",
                endpoint,
            ],
            check=False,
            env=aws_env,
        )
        if rm_result.returncode != 0:
            warn("  Failed to clean latest/ -- continuing with upload")
        destinations.append(("latest", latest_prefix))

    for label, dest_prefix in destinations:
        info(f"  Uploading to {label}: {dest_prefix}")

        for artifact in artifacts:
            cmd = [
                "aws",
                "s3",
                "cp",
                str(artifact),
                f"{dest_prefix}{artifact.name}",
                "--endpoint-url",
                endpoint,
            ]
            result = run_cmd(cmd, check=False, env=aws_env)
            if result.returncode != 0:
                error(f"  R2 upload failed for {artifact.name} ({label})")
                return result.returncode

    success(f"Published {len(artifacts)} artifact(s) to R2 -- {public_url}")
    return 0


def publish_binaries(config: CIConfig) -> int:
    """Publish dist/ artefacts to each configured `binaries` destination.

    Args:
        config: Merged CI configuration.

    Returns:
        Exit code (0 = success).

    """
    destinations = config.destination_for("binaries")
    if not destinations:
        return 0

    # Without this a container-only Python service leaks its wheel and sdist
    # to R2 on every run (issue #105).
    exclude_python = not config.destination_for("python")

    artifacts = _collect_artifacts(exclude_python=exclude_python)
    if not artifacts:
        info("No dist/ artifacts -- skipping binary publish")
        return 0

    channel = _resolve_channel(config, resolve_release_version())
    info(f"Binary publish destinations: {', '.join(destinations)}")
    if channel != "release":
        info(f"Channel: {channel} (prerelease)")

    for dest in destinations:
        if dest == "github-releases":
            with group("Upload: GitHub Releases"):
                rc = _upload_binaries_github(
                    config, channel=channel, exclude_python=exclude_python
                )
                if rc != 0:
                    return rc

        elif dest == "r2-binaries":
            with group("Upload: Cloudflare R2"):
                rc = _publish_r2_binaries(
                    channel=channel, exclude_python=exclude_python
                )
                if rc != 0:
                    return rc

        else:
            error(f"Unknown binary publish destination: {dest}")
            return 1

    return 0
