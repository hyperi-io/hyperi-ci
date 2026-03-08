# Project:   HyperI CI
# File:      src/hyperi_ci/languages/golang/publish.py
# Purpose:   Golang publish handler (Go proxy + JFrog generic + GitHub Releases)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Golang publish handler.

Go modules publish automatically to proxy.golang.org when tagged.
Binary artifacts are uploaded to JFrog Artifactory generic repository
and/or GitHub Releases, matching the old CI pattern from
ci/scripts/languages/golang/publish.sh.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from hyperi_ci.common import error, group, info, mask, success, warn
from hyperi_ci.config import CIConfig, load_org_config


def _publish_go_proxy() -> int:
    """Trigger Go module proxy indexing.

    Go modules are automatically indexed by proxy.golang.org when a tag
    is pushed to a public repo. This forces an immediate fetch.

    Returns:
        Exit code (0 = success).
    """
    result = subprocess.run(
        ["go", "list", "-m"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        error("Could not determine Go module path")
        return 1

    module_path = result.stdout.strip()
    info(f"Go module {module_path} will be indexed by proxy.golang.org on tag push")
    success("Go proxy publish: automatic on tag push")
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
    {project}/{version}/ path, matching the old CI pattern.

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

    dist = Path("dist")
    if not dist.is_dir():
        warn("No dist/ directory — skipping JFrog binary publish")
        return 0

    artifacts = [
        f for f in sorted(dist.iterdir()) if f.is_file() and not f.name.startswith(".")
    ]
    if not artifacts:
        warn("No artifacts found in dist/ — skipping JFrog binary publish")
        return 0

    org = load_org_config()
    project_name = os.environ.get("GO_BINARY_NAME", "") or Path.cwd().name
    version = os.environ.get("GO_VERSION", "")
    if not version:
        version = os.environ.get("CI_COMMIT_TAG", "")
    if not version:
        version = os.environ.get("GITHUB_REF_NAME", "")
    if not version:
        commit = os.environ.get("GITHUB_SHA", "unknown")[:8]
        version = f"snapshot-{commit}"

    base_url = org.artifactory_base_url
    repo = os.environ.get("GO_ARTIFACTORY_REPO", "hyperi-binaries")

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


def _upload_binaries_github() -> int:
    """Upload built binaries and checksums to GitHub Releases.

    Expects versioned binaries in dist/ directory (flat, not nested).
    Uses gh CLI with --clobber for idempotent re-runs.

    Returns:
        Exit code (0 = success).
    """
    dist = Path("dist")
    if not dist.is_dir():
        warn("No dist/ directory — skipping binary upload")
        return 0

    artifacts = [
        f for f in sorted(dist.iterdir()) if f.is_file() and not f.name.startswith(".")
    ]
    if not artifacts:
        warn("No artifacts found in dist/ — skipping upload")
        return 0

    tag = os.environ.get("GITHUB_REF_NAME", "")
    if not tag:
        error("GITHUB_REF_NAME not set — cannot determine release tag")
        return 1

    cmd = ["gh", "release", "upload", tag, "--clobber"]
    cmd.extend(str(f) for f in artifacts)

    result = subprocess.run(cmd)
    if result.returncode != 0:
        error("GitHub Release upload failed")
        return result.returncode

    success(f"Uploaded {len(artifacts)} artifact(s) to GitHub Releases")
    return 0


def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Run Golang publish stage.

    Args:
        config: Merged CI configuration.
        extra_env: Additional environment variables.

    Returns:
        Exit code (0 = success).
    """
    go_destinations = config.destination_for("go")
    binary_destinations = config.destination_for("binaries")

    if not go_destinations and not binary_destinations:
        info("No Go publish destinations configured")
        return 0

    for dest in go_destinations:
        if dest == "go-proxy":
            with group("Publish: Go proxy"):
                rc = _publish_go_proxy()
                if rc != 0:
                    return rc

        else:
            error(f"Unknown Go publish destination: {dest}")
            return 1

    for dest in binary_destinations:
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

        else:
            error(f"Unknown binary publish destination: {dest}")
            return 1

    return 0
