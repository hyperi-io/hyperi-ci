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
- Cloudflare R2 binary repository (internal, Phase 2)

Called from dispatch.py after the language-specific publish handler.
Any language that packages binaries to dist/ gets this for free.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from hyperi_ci.common import error, group, info, mask, success, warn
from hyperi_ci.config import CIConfig, load_org_config


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
        error(
            "ARTIFACTORY_USERNAME and ARTIFACTORY_PASSWORD required for JFrog publish"
        )
        return 1

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
            warn("R2 binary publish not yet implemented — skipping")

        else:
            error(f"Unknown binary publish destination: {dest}")
            return 1

    return 0
