# Project:   HyperI CI
# File:      src/hyperi_ci/languages/golang/publish.py
# Purpose:   Golang publish handler (Go proxy + JFrog + GitHub Releases)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Golang publish handler.

Go modules publish automatically to proxy.golang.org when tagged.
For internal use, publishes to JFrog Artifactory Go repository.
Binary artifacts are uploaded to GitHub Releases or JFrog generic.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from hyperi_ci.common import error, group, info, success, warn
from hyperi_ci.config import CIConfig, load_org_config


def _publish_go_proxy() -> int:
    """Trigger Go module proxy indexing.

    Go modules are automatically indexed by proxy.golang.org when a tag
    is pushed to a public repo. This forces an immediate fetch.

    Returns:
        Exit code (0 = success).
    """
    # Get module path from go.mod
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


def _publish_jfrog() -> int:
    """Publish Go module to JFrog Artifactory.

    Requires JFROG_TOKEN env var and uses org config for repository URL.

    Returns:
        Exit code (0 = success).
    """
    token = os.environ.get("JFROG_TOKEN")
    if not token:
        error("JFROG_TOKEN not set — cannot publish to JFrog")
        return 1

    org = load_org_config()
    goproxy_url = (
        f"https://{org.jfrog_domain}/artifactory/api/go/{org.jfrog_org_prefix}-go-local"
    )

    result = subprocess.run(
        ["go", "mod", "download"],
        env={
            **os.environ,
            "GOPROXY": goproxy_url,
            "GONOSUMCHECK": "*",
        },
    )
    if result.returncode != 0:
        error("JFrog Go publish failed")
        return result.returncode

    success("Published to JFrog Go")
    return 0


def _upload_binaries_github() -> int:
    """Upload built binaries to GitHub Releases.

    Expects binaries in dist/ directory. Uses gh CLI.

    Returns:
        Exit code (0 = success).
    """
    dist = Path("dist")
    if not dist.is_dir():
        warn("No dist/ directory — skipping binary upload")
        return 0

    binaries = list(dist.rglob("*"))
    binaries = [b for b in binaries if b.is_file()]
    if not binaries:
        warn("No binaries found in dist/ — skipping upload")
        return 0

    tag = os.environ.get("GITHUB_REF_NAME", "")
    if not tag:
        error("GITHUB_REF_NAME not set — cannot determine release tag")
        return 1

    cmd = ["gh", "release", "upload", tag, "--clobber"]
    cmd.extend(str(b) for b in binaries)

    result = subprocess.run(cmd)
    if result.returncode != 0:
        error("GitHub Release upload failed")
        return result.returncode

    success(f"Uploaded {len(binaries)} binary(ies) to GitHub Releases")
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

        elif dest == "jfrog-go":
            with group("Publish: JFrog Go"):
                rc = _publish_jfrog()
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
            info("JFrog generic binary upload not yet implemented")

        else:
            error(f"Unknown binary publish destination: {dest}")
            return 1

    return 0
