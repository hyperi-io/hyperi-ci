# Project:   HyperI CI
# File:      src/hyperi_ci/publish/binaries.py
# Purpose:   Language-agnostic binary artifact publishing
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Generic binary artifact publishing.

Uploads pre-built binaries from dist/ to:
- GitHub Releases (per-tag artefacts)
- Cloudflare R2 (``downloads.hyperi.io/<project>/<version|latest>/``)

Called from dispatch.py after the language-specific publish handler.
Any language that packages binaries to dist/ gets this for free.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from hyperi_ci.common import (
    error,
    group,
    info,
    mask,
    resolve_release_version,
    run_cmd,
    success,
    warn,
)
from hyperi_ci.config import CIConfig
from hyperi_ci.tools import missing_tool_notice

# R2 bucket and endpoint configuration
R2_BUCKET = "bin-repo"
R2_ACCOUNT_ID = "98d20454e2af7a9397ad9366a1641659"
R2_ENDPOINT = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
R2_PUBLIC_URL = "https://downloads.hyperi.io"

VALID_CHANNELS = ("spike", "alpha", "beta", "release")


def _resolve_gh_release_flags(channel: str) -> list[str]:
    """Return extra flags for gh release create based on channel."""
    if channel != "release":
        return ["--prerelease"]
    return []


def _resolve_r2_paths(project_name: str, version: str, channel: str) -> tuple[str, str]:
    """Return (versioned_prefix, latest_prefix) S3 paths for R2."""
    if channel == "release":
        versioned = f"s3://{R2_BUCKET}/{project_name}/v{version}/"
        latest = f"s3://{R2_BUCKET}/{project_name}/latest/"
    else:
        versioned = f"s3://{R2_BUCKET}/{project_name}/{channel}/v{version}/"
        latest = f"s3://{R2_BUCKET}/{project_name}/{channel}/latest/"
    return versioned, latest


def _read_version() -> str | None:
    """Read the version being published (HYPERCI_VERSION-first).

    See common.resolve_release_version (issue #27 + zero-config).
    """
    return resolve_release_version()


_PYTHON_DIST_SUFFIXES = (".whl", ".tar.gz", ".zip")


def _is_python_dist_artifact(path: Path) -> bool:
    """True for a Python packaging artefact (wheel or sdist).

    Used to honour ``destinations_oss.python: false``: a project that ships
    no Python distribution must not leak its wheel/sdist to R2 or a GitHub
    Release through the GENERIC binary publisher either (issue #105 BUG 2 —
    the opt-out was previously honoured only by the python publish handler).
    A ``.whl`` is unambiguously a wheel; the accompanying sdist (``.tar.gz`` /
    ``.zip``) is dropped alongside it. Only ever consulted when the python
    destination is opted out, so a Rust/Go tarball is never at risk.
    """
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in _PYTHON_DIST_SUFFIXES)


def _release_targets_head(tag: str) -> bool:
    """True iff the git tag for an existing release points at HEAD.

    An existing release AT HEAD is an idempotent re-run (safe to proceed);
    one at a DIFFERENT commit means a stale version was resolved, and
    re-publishing would overwrite a shipped release with different contents
    (issue #105 — a four-month-old tag rebuilt from today's HEAD clobbered
    ``latest``). When the tag cannot be resolved locally we cannot prove it
    is HEAD, so we treat it as a mismatch and refuse.
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


def _collect_artifacts(exclude_python: bool = False) -> list[Path]:
    """Collect publishable artifacts from dist/ directory.

    Returns sorted list of files, excluding hidden files. When
    ``exclude_python`` is set (the project opted out of a Python
    distribution), Python packaging artefacts (wheels + sdists) are dropped
    so they are never uploaded as generic binaries (issue #105 BUG 2).
    """
    dist = Path("dist")
    if not dist.is_dir():
        return []
    files = [
        f for f in sorted(dist.iterdir()) if f.is_file() and not f.name.startswith(".")
    ]
    if exclude_python:
        files = [f for f in files if not _is_python_dist_artifact(f)]
    return files


def create_github_release(config: CIConfig) -> int:
    """Create a GitHub Release for the current version.

    Always called during publish, regardless of whether there are binary
    artifacts. Libraries get a GH Release without attachments; binaries
    get artifacts uploaded separately by publish_binaries().

    Returns:
        Exit code (0 = success).

    """
    version = _read_version()
    if not version:
        error("No VERSION file — cannot determine release tag")
        return 1

    channel = config.get("publish.channel", "release")
    tag = f"v{version}"

    cmd = ["gh", "release", "create", tag, "--title", tag, "--generate-notes"]
    cmd.extend(_resolve_gh_release_flags(channel))

    info(f"Creating GitHub Release {tag}")
    result = run_cmd(cmd, check=False, capture=True)
    if result.returncode != 0:
        if "already exists" in result.stderr:
            # A release for this tag already exists. Allow an idempotent
            # re-run (same commit), but REFUSE to publish onto a release that
            # points at a different commit — logging "already exists" and
            # carrying on is how a stale-version dispatch overwrote `latest`
            # (issue #105). The git tag is the source of truth for what commit
            # the release shipped from.
            if _release_targets_head(tag):
                info(f"  GH Release {tag} already exists at HEAD — idempotent re-run")
                return 0
            error(
                f"GH Release {tag} already exists at a commit other than HEAD — "
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
    channel: str = "release", exclude_python: bool = False
) -> int:
    """Create GitHub Release and upload built binaries.

    Creates a GH Release for the tag (from VERSION file). For non-release
    channels (spike, alpha, beta), the release is marked as prerelease.
    Falls back to upload if the release already exists at HEAD (idempotent
    re-runs); refuses to clobber a release at a different commit (#105).

    Returns:
        Exit code (0 = success).

    """
    artifacts = _collect_artifacts(exclude_python=exclude_python)
    if not artifacts:
        warn("No artifacts found in dist/ — skipping GitHub Release upload")
        return 0

    version = _read_version()
    if not version:
        error("No VERSION file — cannot determine release tag")
        return 1

    tag = f"v{version}"
    info(f"Publishing {len(artifacts)} artifact(s) to GitHub Release {tag}")

    cmd = ["gh", "release", "create", tag, "--title", tag, "--generate-notes"]
    cmd.extend(_resolve_gh_release_flags(channel))
    cmd.extend(str(f) for f in artifacts)

    result = run_cmd(cmd, check=False, capture=True)
    if result.returncode != 0:
        if "already exists" in result.stderr:
            # Only clobber a release that ships from HEAD (idempotent re-run);
            # a different commit means a stale version resolved (issue #105).
            if not _release_targets_head(tag):
                error(
                    f"GH Release {tag} already exists at a commit other than "
                    f"HEAD — refusing to clobber its assets (issue #105)."
                )
                return 1
            info(f"  GH Release {tag} already exists at HEAD — uploading artifacts")
            upload_cmd = ["gh", "release", "upload", tag, "--clobber"]
            upload_cmd.extend(str(f) for f in artifacts)
            result = subprocess.run(upload_cmd)
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
    """Publish built binaries to Cloudflare R2 binary repository.

    Uploads all files from dist/ to R2. Channel controls path:
      release:  {project}/v{version}/  + {project}/latest/
      other:    {project}/{channel}/v{version}/  + {project}/{channel}/latest/

    Requires R2_ACCESS_KEY_ID + R2_SECRET_ACCESS_KEY env vars.

    Returns:
        Exit code (0 = success).

    """
    access_key = os.environ.get("R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")
    if not access_key or not secret_key:
        warn(
            "R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY not set — skipping R2 binary publish"
        )
        return 0

    mask(secret_key)

    if not shutil.which("aws"):
        error(missing_tool_notice("aws"))
        return 1

    artifacts = _collect_artifacts(exclude_python=exclude_python)
    if not artifacts:
        warn("No artifacts found in dist/ — skipping R2 binary publish")
        return 0

    project_name = Path.cwd().name
    version = _read_version() or "unknown"

    versioned_prefix, latest_prefix = _resolve_r2_paths(project_name, version, channel)

    # Common env for aws CLI — use R2 credentials as AWS credentials
    aws_env = {
        **os.environ,
        "AWS_ACCESS_KEY_ID": access_key,
        "AWS_SECRET_ACCESS_KEY": secret_key,
        "AWS_DEFAULT_REGION": "auto",
    }

    info(f"Publishing to R2: {R2_PUBLIC_URL}/{project_name}/v{version}/")

    # Clean latest/ before uploading so stale files from previous builds
    # (e.g. renamed binaries) don't linger alongside new ones
    info(f"  Cleaning latest/: {latest_prefix}")
    rm_result = subprocess.run(
        [
            "aws",
            "s3",
            "rm",
            latest_prefix,
            "--recursive",
            "--endpoint-url",
            R2_ENDPOINT,
        ],
        env=aws_env,
    )
    if rm_result.returncode != 0:
        warn("  Failed to clean latest/ — continuing with upload")

    for dest_prefix in (versioned_prefix, latest_prefix):
        label = "versioned" if "/v" in dest_prefix else "latest"
        info(f"  Uploading to {label}: {dest_prefix}")

        for artifact in artifacts:
            cmd = [
                "aws",
                "s3",
                "cp",
                str(artifact),
                f"{dest_prefix}{artifact.name}",
                "--endpoint-url",
                R2_ENDPOINT,
            ]
            result = subprocess.run(cmd, env=aws_env)
            if result.returncode != 0:
                error(f"  R2 upload failed for {artifact.name} ({label})")
                return result.returncode

    success(
        f"Published {len(artifacts)} artifact(s) to R2 — "
        f"{R2_PUBLIC_URL}/{project_name}/v{version}/"
    )
    return 0


def publish_binaries(config: CIConfig) -> int:
    """Publish binary artifacts from dist/ to configured destinations.

    This is the main entry point, called from dispatch.py after the
    language-specific publish handler completes. Checks for binary
    destinations in the config and uploads accordingly.

    Args:
        config: Merged CI configuration.

    Returns:
        Exit code (0 = success).

    """
    destinations = config.destination_for("binaries")
    if not destinations:
        return 0

    # Honour destinations_oss.python: false for the generic binary publisher
    # too — otherwise a private, container-only Python service still leaks its
    # wheel + sdist to R2 on every run (issue #105 BUG 2). A Rust/Go project
    # leaves python at its truthy default, so this never drops a real binary.
    exclude_python = not config.destination_for("python")

    artifacts = _collect_artifacts(exclude_python=exclude_python)
    if not artifacts:
        info("No dist/ artifacts — skipping binary publish")
        return 0

    channel = config.get("publish.channel", "release")
    info(f"Binary publish destinations: {', '.join(destinations)}")
    if channel != "release":
        info(f"Channel: {channel} (prerelease)")

    for dest in destinations:
        if dest == "github-releases":
            with group("Upload: GitHub Releases"):
                rc = _upload_binaries_github(
                    channel=channel, exclude_python=exclude_python
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
