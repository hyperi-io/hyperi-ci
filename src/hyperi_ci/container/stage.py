# Project:   HyperI CI
# File:      src/hyperi_ci/container/stage.py
# Purpose:   Container build stage handler
#
# License:   FSL-1.1-ALv2
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Container build stage: mode detection, Dockerfile generation, build + push."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from hyperi_ci.common import error, group, info
from hyperi_ci.config import CIConfig, load_org_config
from hyperi_ci.container.build import build_and_push, resolve_tags
from hyperi_ci.container.labels import build_oci_labels

_TEMPLATE_LANGUAGES = {"python", "typescript"}
_CONTRACT_LANGUAGES = {"rust"}


def detect_mode(config: dict, *, language: str) -> str:
    """Detect container build mode from config and language.

    Args:
        config: Raw config dict (or nested container section).
        language: Detected project language.

    Returns:
        Mode string: "contract", "template", or "custom".

    """
    container = config.get("container", {})
    explicit_mode = container.get("mode", "")

    if explicit_mode:
        return explicit_mode

    if language in _CONTRACT_LANGUAGES:
        return "contract"
    if language in _TEMPLATE_LANGUAGES:
        return "template"
    return "custom"


def _read_version() -> str:
    version_file = Path("VERSION")
    if version_file.exists():
        return version_file.read_text().strip()
    return os.environ.get("GITHUB_REF_NAME", "0.0.0").removeprefix("v")


def _read_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _is_push_to_main() -> bool:
    return (
        os.environ.get("GITHUB_EVENT_NAME") == "push"
        and os.environ.get("GITHUB_REF") == "refs/heads/main"
    )


def _build_contract(config: CIConfig, language: str) -> int:
    """Build container image using contract mode (Rust apps)."""
    from hyperi_ci.container.compose import compose_contract_dockerfile
    from hyperi_ci.container.manifest import load_manifest

    manifest_dir = Path(".ci")
    manifest_path = manifest_dir / "container-manifest.json"

    if not manifest_path.exists():
        binary_name = Path.cwd().name
        dist_dir = Path("dist")
        binary_candidates = list(dist_dir.glob(f"{binary_name}*"))
        if not binary_candidates:
            error(f"No binary found in dist/ matching '{binary_name}'")
            return 1

        binary = binary_candidates[0]
        info(f"Generating contract artifacts from: {binary}")
        manifest_dir.mkdir(exist_ok=True)
        result = subprocess.run(
            [str(binary), "generate-artefacts", "--output-dir", str(manifest_dir)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            error(f"Failed to generate contract artifacts: {result.stderr}")
            return result.returncode

    if not manifest_path.exists():
        error(f"Contract manifest not found at {manifest_path}")
        return 1

    manifest = load_manifest(manifest_path)
    info(f"Contract manifest: {manifest.binary_name} on {manifest.base_image}")

    rust_version = _detect_rust_version()
    dockerfile_content = compose_contract_dockerfile(
        manifest, rust_version=rust_version
    )

    return _build_from_content(config, dockerfile_content, extra_labels=manifest.labels)


def _build_template(config: CIConfig, language: str) -> int:
    """Build container image using template mode (Python/Node apps)."""
    from hyperi_ci.container.templates import (
        render_node_template,
        render_python_template,
    )

    container_cfg = config.get("publish.container", {})
    if not isinstance(container_cfg, dict):
        container_cfg = {}

    if language == "python":
        dockerfile_content = render_python_template(
            python_version=container_cfg.get("python_version", "3.12"),
            port=container_cfg.get("port", 8080),
            health_path=container_cfg.get("health_path", "/healthz"),
            entrypoint=container_cfg.get("entrypoint", Path.cwd().name),
            cmd=container_cfg.get("cmd", "run"),
        )
    elif language == "typescript":
        dockerfile_content = render_node_template(
            node_version=container_cfg.get("node_version", "22"),
            port=container_cfg.get("port", 3000),
        )
    else:
        error(f"No template available for language: {language}")
        return 1

    return _build_from_content(config, dockerfile_content)


def _build_custom(config: CIConfig, language: str) -> int:
    """Build container image using custom mode (repo's own Dockerfile)."""
    container_cfg = config.get("publish.container", {})
    if not isinstance(container_cfg, dict):
        container_cfg = {}

    dockerfile = Path(container_cfg.get("dockerfile", "Dockerfile"))
    if not dockerfile.exists():
        error(f"Dockerfile not found: {dockerfile}")
        return 1

    context = container_cfg.get("context", ".")
    org = load_org_config()
    registry = container_cfg.get("registry", f"{org.ghcr_registry}/{org.ghcr_org}")
    image_name = Path.cwd().name
    version = _read_version()
    sha = _read_sha()
    channel = config.get("publish.channel", "release")

    tags = resolve_tags(
        registry=registry,
        image_name=image_name,
        version=version,
        sha=sha,
        channel=channel,
        is_push_to_main=_is_push_to_main(),
    )

    labels = build_oci_labels(
        repo=f"{org.github_org}/{image_name}",
        revision=os.environ.get("GITHUB_SHA", _read_sha()),
        version=version,
        title=image_name,
    )

    extra_labels = container_cfg.get("labels", {})
    if extra_labels:
        labels.update(extra_labels)

    platforms = container_cfg.get("platforms", ["linux/amd64"])
    build_args = container_cfg.get("build_args", {})

    return build_and_push(
        dockerfile_path=dockerfile,
        context=context,
        tags=tags,
        platforms=platforms,
        labels=labels,
        build_args=build_args if build_args else None,
    )


def _build_from_content(
    config: CIConfig,
    dockerfile_content: str,
    extra_labels: dict[str, str] | None = None,
) -> int:
    """Write Dockerfile content to temp file and build."""
    container_cfg = config.get("publish.container", {})
    if not isinstance(container_cfg, dict):
        container_cfg = {}

    org = load_org_config()
    registry = container_cfg.get("registry", f"{org.ghcr_registry}/{org.ghcr_org}")
    image_name = Path.cwd().name
    version = _read_version()
    sha = _read_sha()
    channel = config.get("publish.channel", "release")

    tags = resolve_tags(
        registry=registry,
        image_name=image_name,
        version=version,
        sha=sha,
        channel=channel,
        is_push_to_main=_is_push_to_main(),
    )

    labels = build_oci_labels(
        repo=f"{org.github_org}/{image_name}",
        revision=os.environ.get("GITHUB_SHA", _read_sha()),
        version=version,
        title=image_name,
    )
    if extra_labels:
        labels.update(extra_labels)

    cfg_labels = container_cfg.get("labels", {})
    if cfg_labels:
        labels.update(cfg_labels)

    platforms = container_cfg.get("platforms", ["linux/amd64"])
    build_args = container_cfg.get("build_args", {})
    context = container_cfg.get("context", ".")

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".Dockerfile", delete=False, dir="."
    ) as f:
        f.write(dockerfile_content)
        dockerfile_path = Path(f.name)

    try:
        return build_and_push(
            dockerfile_path=dockerfile_path,
            context=context,
            tags=tags,
            platforms=platforms,
            labels=labels,
            build_args=build_args if build_args else None,
        )
    finally:
        dockerfile_path.unlink(missing_ok=True)


def _detect_rust_version() -> str:
    toolchain_file = Path("rust-toolchain.toml")
    if toolchain_file.exists():
        for line in toolchain_file.read_text().splitlines():
            if "channel" in line and "=" in line:
                return line.split("=")[1].strip().strip('"').strip("'")
    return "stable"


def run(config: CIConfig, *, language: str = "") -> int:
    """Run the container build stage.

    Args:
        config: Merged CI configuration.
        language: Detected project language.

    Returns:
        Exit code (0 = success).

    """
    container_cfg = config.get("publish.container", {})
    if not isinstance(container_cfg, dict):
        container_cfg = {}

    if not container_cfg.get("enabled", False):
        info("Container build disabled — skipping")
        return 0

    mode = detect_mode(config._raw, language=language)
    info(f"Container build mode: {mode}")

    with group(f"Container Build ({mode})"):
        if mode == "contract":
            return _build_contract(config, language)
        if mode == "template":
            return _build_template(config, language)
        if mode == "custom":
            return _build_custom(config, language)

        error(f"Unknown container mode: {mode}")
        return 1
