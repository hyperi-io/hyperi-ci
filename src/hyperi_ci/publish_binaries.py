# Project:   HyperI CI
# File:      src/hyperi_ci/publish_binaries.py
# Purpose:   Language-agnostic binary artifact publishing
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Generic binary artifact publishing.

Uploads pre-built binaries from dist/ to configured destinations:
- GitHub Releases (OSS)
- JFrog Artifactory generic repository (internal)
- Cloudflare R2 binary repository (internal)

Called from dispatch.py after the language-specific publish handler.
Any language that packages binaries to dist/ gets this for free.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from hyperi_ci.common import error, group, info, mask, success, warn
from hyperi_ci.config import CIConfig, load_org_config

# R2 bucket and endpoint configuration
R2_BUCKET = "bin-repo"
R2_ACCOUNT_ID = "98d20454e2af7a9397ad9366a1641659"
R2_ENDPOINT = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
R2_PUBLIC_URL = "https://downloads.hyperi.io"


def _read_version() -> str | None:
    """Read version from VERSION file (written by semantic-release)."""
    version_file = Path("VERSION")
    if not version_file.exists():
        return None
    version = version_file.read_text().strip()
    return version if version else None


def _collect_artifacts() -> list[Path]:
    """Collect publishable artifacts from dist/ directory.

    Returns sorted list of files, excluding hidden files.
    """
    dist = Path("dist")
    if not dist.is_dir():
        return []
    return [
        f for f in sorted(dist.iterdir()) if f.is_file() and not f.name.startswith(".")
    ]


def _upload_binaries_github() -> int:
    """Upload built binaries and checksums to GitHub Releases.

    Reads the release tag from the VERSION file (prefixed with 'v').
    In the publish job, GITHUB_REF_NAME is the branch name (e.g. 'release'),
    not the tag — semantic-release writes the version to the VERSION file.

    Uses gh CLI with --clobber for idempotent re-runs.

    Returns:
        Exit code (0 = success).
    """
    artifacts = _collect_artifacts()
    if not artifacts:
        warn("No artifacts found in dist/ — skipping GitHub Release upload")
        return 0

    version = _read_version()
    if not version:
        error("No VERSION file — cannot determine release tag")
        return 1

    tag = f"v{version}"
    info(f"Uploading {len(artifacts)} artifact(s) to GitHub Release {tag}")

    cmd = ["gh", "release", "upload", tag, "--clobber"]
    cmd.extend(str(f) for f in artifacts)

    result = subprocess.run(cmd)
    if result.returncode != 0:
        error("GitHub Release upload failed")
        return result.returncode

    success(f"Uploaded {len(artifacts)} artifact(s) to GitHub Release {tag}")
    return 0


def _upload_to_artifactory(file_path: Path, target_url: str) -> bool:
    """Upload a single file to JFrog Artifactory via HTTP PUT.

    Args:
        file_path: Local file to upload.
        target_url: Full Artifactory URL for the upload target.

    Returns:
        True on success (HTTP 200/201).
    """
    username = os.environ.get("ARTIFACTORY_USERNAME", "")
    password = os.environ.get("ARTIFACTORY_PASSWORD", "")

    cmd = [
        "curl",
        "-sS",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code}",
        "-u",
        f"{username}:{password}",
        "-T",
        str(file_path),
        target_url,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    http_code = result.stdout.strip()

    if http_code in ("200", "201"):
        return True

    error(f"  Upload failed for {file_path.name} (HTTP {http_code})")
    return False


def _publish_jfrog_binaries() -> int:
    """Publish built binaries to JFrog Artifactory generic repository.

    Uploads all files from dist/ to the JFrog generic repo under
    {project}/{version}/ path.

    Requires ARTIFACTORY_USERNAME + ARTIFACTORY_PASSWORD env vars.

    Returns:
        Exit code (0 = success).
    """
    username = os.environ.get("ARTIFACTORY_USERNAME")
    password = os.environ.get("ARTIFACTORY_PASSWORD")
    if not username or not password:
        warn(
            "ARTIFACTORY_USERNAME/ARTIFACTORY_PASSWORD not set"
            " — skipping JFrog binary publish"
        )
        return 0

    mask(password)

    artifacts = _collect_artifacts()
    if not artifacts:
        warn("No artifacts found in dist/ — skipping JFrog binary publish")
        return 0

    org = load_org_config()
    project_name = Path.cwd().name
    version = _read_version() or "unknown"

    base_url = org.artifactory_base_url
    repo = os.environ.get("BINARY_REPO", "hyperi-binaries")

    info(f"Publishing to: {base_url}/{repo}/{project_name}/{version}/")

    uploaded = 0
    for artifact in artifacts:
        target_url = f"{base_url}/{repo}/{project_name}/{version}/{artifact.name}"
        info(f"  Uploading: {artifact.name}")
        if _upload_to_artifactory(artifact, target_url):
            uploaded += 1
        else:
            return 1

    success(f"Published {uploaded} artifact(s) to JFrog Artifactory")
    return 0


def _publish_r2_binaries() -> int:
    """Publish built binaries to Cloudflare R2 binary repository.

    Uploads all files from dist/ to R2 under:
      {project}/{version}/{filename}   — versioned path
      {project}/latest/{filename}      — latest alias (overwritten each release)

    Public URL: https://releases.hyperi.io/{project}/{version}/{filename}

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
        error("aws CLI not found — required for R2 upload")
        return 1

    artifacts = _collect_artifacts()
    if not artifacts:
        warn("No artifacts found in dist/ — skipping R2 binary publish")
        return 0

    project_name = Path.cwd().name
    version = _read_version() or "unknown"

    versioned_prefix = f"s3://{R2_BUCKET}/{project_name}/v{version}/"
    latest_prefix = f"s3://{R2_BUCKET}/{project_name}/latest/"

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

    artifacts = _collect_artifacts()
    if not artifacts:
        info("No dist/ artifacts — skipping binary publish")
        return 0

    info(f"Binary publish destinations: {', '.join(destinations)}")

    for dest in destinations:
        if dest == "github-releases":
            with group("Upload: GitHub Releases"):
                rc = _upload_binaries_github()
                if rc != 0:
                    return rc

        elif dest == "jfrog-generic":
            with group("Upload: JFrog Artifactory"):
                rc = _publish_jfrog_binaries()
                if rc != 0:
                    return rc

        elif dest == "r2-binaries":
            with group("Upload: Cloudflare R2"):
                rc = _publish_r2_binaries()
                if rc != 0:
                    return rc

        else:
            error(f"Unknown binary publish destination: {dest}")
            return 1

    return 0
