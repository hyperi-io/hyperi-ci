# Project:   HyperI CI
# File:      src/hyperi_ci/container/stage.py
# Purpose:   Container build stage handler
#
# License:   FSL-1.1-ALv2
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Container build stage.

Three-state ``publish.container.enabled`` gate:

* ``auto`` (default): build when a container artefact is detected
  (Dockerfile in repo, or rustlib contract source). Library projects
  and projects with no signal skip silently.
* ``true``: build is required. Fails loudly if no signal is present —
  surfaces a regression where a project lost its containerisable
  artefact.
* ``false``: explicit skip.

Routing follows ``publish.target``:

* ``oss`` → GHCR
* ``internal`` → JFrog Docker
* ``both`` → both registries (one buildx, multiple ``--tag`` args)

Push-to-main runs in **validate** mode (build, no push). Release
dispatch runs in **push** mode. Branch / PR pushes don't reach this
handler — the workflow's outer gate skips the Container job.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from hyperi_ci.common import error, group, info, success, warn
from hyperi_ci.config import CIConfig, OrgConfig, load_org_config
from hyperi_ci.container.build import build_and_push, resolve_tags
from hyperi_ci.container.detect import Decision, detect
from hyperi_ci.container.labels import build_oci_labels
from hyperi_ci.container.registry import resolve_registry_bases

_TEMPLATE_LANGUAGES = {"python", "typescript"}
_CONTRACT_LANGUAGES = {"rust"}


def _read_version() -> str:
    version_file = Path("VERSION")
    if version_file.exists():
        return version_file.read_text().strip()
    return os.environ.get("GITHUB_REF_NAME", "0.0.0").removeprefix("v")


def _read_sha() -> str:
    long_sha = os.environ.get("GITHUB_SHA")
    if long_sha:
        return long_sha[:8]
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _is_publish_mode() -> bool:
    """True iff the workflow has signalled this is a publish run.

    Set by the rust-ci.yml ``container`` job from ``setup.will-publish``.
    Maps directly to docker buildx ``--push``: when True we push to
    every configured registry; when False we just build and discard.

    Falls back to legacy event-based detection if HYPERCI_PUBLISH_MODE
    isn't set (older workflows or local invocations) — in that case
    workflow_dispatch implies publish, push-to-main implies validate.
    """
    flag = os.environ.get("HYPERCI_PUBLISH_MODE", "").strip().lower()
    if flag in ("true", "1", "yes"):
        return True
    if flag in ("false", "0", "no"):
        return False
    # Legacy fallback: workflow_dispatch == publish, push to main == validate.
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        return True
    if (
        os.environ.get("GITHUB_EVENT_NAME") == "push"
        and os.environ.get("GITHUB_REF") == "refs/heads/main"
    ):
        return False
    return False


def _is_push_to_main() -> bool:
    """Deprecated alias — kept for any out-of-tree callers.

    Returns ``not _is_publish_mode()`` to match the original semantics
    (``push_to_main`` was the validate-only flag, named confusingly).
    Will be removed once consumers update.
    """
    return not _is_publish_mode()


def _resolve_mode(*, language: str, decision: Decision, container_cfg: dict) -> str:
    """Pick the build mode.

    Order of precedence:

    1. Explicit ``container.mode`` set by the project.
    2. The detector's recommended mode (``contract``, ``template``,
       ``custom``) when the artefact was actually detected.
    3. Language default fallback (Rust → contract, Python/TS → template,
       otherwise → custom).
    """
    explicit = container_cfg.get("mode", "")
    if explicit:
        return explicit
    if decision.mode:
        return decision.mode
    if language in _CONTRACT_LANGUAGES:
        return "contract"
    if language in _TEMPLATE_LANGUAGES:
        return "template"
    return "custom"


def run(config: CIConfig, *, language: str = "") -> int:
    """Run the container build stage.

    Args:
        config: Merged CI configuration.
        language: Detected project language.

    Returns:
        Exit code (0 = success or skipped).

    """
    container_cfg = config.get("publish.container", {})
    if not isinstance(container_cfg, dict):
        container_cfg = {}

    enabled = _normalise_enabled(container_cfg.get("enabled", "auto"))

    if enabled == "false":
        info("Container build disabled (publish.container.enabled: false) — skipping")
        return 0

    project_dir = Path.cwd()
    dockerfile_name = container_cfg.get("dockerfile", "Dockerfile")
    decision = detect(
        language=language,
        project_dir=project_dir,
        dockerfile=dockerfile_name,
    )

    if not decision.build:
        if enabled == "true":
            error(
                "publish.container.enabled: true but no container artefact "
                f"detected — {decision.reason}",
            )
            return 1
        info(f"Container build skipped — {decision.reason}")
        return 0

    info(f"Container build will run — {decision.reason}")

    target = config.get("publish.target", "internal")
    org = load_org_config()
    try:
        registry_bases = resolve_registry_bases(target=target, org=org)
    except ValueError as exc:
        error(str(exc))
        return 1

    push_to_main = _is_push_to_main()
    mode = _resolve_mode(
        language=language,
        decision=decision,
        container_cfg=container_cfg,
    )
    info(f"Container build mode: {mode} ({'validate' if push_to_main else 'push'})")

    with group(f"Container Build ({mode})"):
        if mode == "contract":
            return _build_contract(
                config=config,
                container_cfg=container_cfg,
                org=org,
                registry_bases=registry_bases,
                push_to_main=push_to_main,
            )
        if mode == "template":
            return _build_template(
                language=language,
                config=config,
                container_cfg=container_cfg,
                org=org,
                registry_bases=registry_bases,
                push_to_main=push_to_main,
            )
        if mode == "custom":
            return _build_custom(
                container_cfg=container_cfg,
                config=config,
                org=org,
                registry_bases=registry_bases,
                push_to_main=push_to_main,
                dockerfile_name=dockerfile_name,
            )

        error(f"Unknown container mode: {mode!r}")
        return 1


def _normalise_enabled(raw: object) -> str:
    """Coerce the YAML ``enabled`` value into ``true`` / ``false`` / ``auto``."""
    if raw is True:
        return "true"
    if raw is False:
        return "false"
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"true", "false", "auto"}:
            return lowered
        warn(
            f"Unknown publish.container.enabled value {raw!r} — falling back to 'auto'",
        )
    return "auto"


def _build_custom(
    *,
    container_cfg: dict,
    config: CIConfig,
    org: OrgConfig,
    registry_bases: list[str],
    push_to_main: bool,
    dockerfile_name: str,
) -> int:
    dockerfile = Path(dockerfile_name)
    if not dockerfile.exists():
        error(f"Dockerfile not found: {dockerfile}")
        return 1

    return _dispatch_build(
        dockerfile_path=dockerfile,
        container_cfg=container_cfg,
        config=config,
        org=org,
        registry_bases=registry_bases,
        push_to_main=push_to_main,
    )


def _build_template(
    *,
    language: str,
    config: CIConfig,
    container_cfg: dict,
    org: OrgConfig,
    registry_bases: list[str],
    push_to_main: bool,
) -> int:
    from hyperi_ci.container.templates import (
        render_node_template,
        render_python_template,
    )

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
        error(f"No template available for language: {language!r}")
        return 1

    return _build_from_content(
        dockerfile_content=dockerfile_content,
        container_cfg=container_cfg,
        config=config,
        org=org,
        registry_bases=registry_bases,
        push_to_main=push_to_main,
    )


def _build_contract(
    *,
    config: CIConfig,
    container_cfg: dict,
    org: OrgConfig,
    registry_bases: list[str],
    push_to_main: bool,
) -> int:
    from hyperi_ci.container.compose import compose_contract_dockerfile
    from hyperi_ci.container.manifest import load_manifest

    # Lookup order:
    #   1. ci-tmp/ — produced fresh by the Build stage (`hyperi-ci run
    #      generate`). The canonical CI path.
    #   2. ci/    — committed-and-regenerated artefacts (drift-checked
    #      by the Quality stage). Used for local Container builds where
    #      you skip the Build stage.
    #   3. .ci/   — legacy path from before the Build/Container split.
    #      Kept for one release for back-compat.
    #
    # We deliberately do NOT fall back to subprocess-invoking the binary
    # here. The Container runner is bare — it has no Rust toolchain
    # installed and so lacks runtime libs (librdkafka, libssl, libgit2,
    # ...) that the binary dynamically links against. The Build runner
    # has all of these via `install-native-deps rust`, which is why
    # generate-artefacts now runs there.
    manifest_path: Path | None = None
    for candidate_dir in (Path("ci-tmp"), Path("ci"), Path(".ci")):
        candidate = candidate_dir / "container-manifest.json"
        if candidate.exists():
            manifest_path = candidate
            info(f"Using deployment artefacts from: {candidate_dir}/")
            break

    if manifest_path is None:
        error(
            "No deployment artefacts found. Looked in ci-tmp/, ci/, and "
            ".ci/ for container-manifest.json. The Build stage runs "
            "`hyperi-ci run generate` to produce these — check that the "
            "Build job uploaded ci-tmp/ as part of build-dist-* and that "
            "the Container job's download-artifact step picked it up. "
            "For local Container builds, run `hyperi-ci run generate` "
            "first to populate ci-tmp/."
        )
        return 1

    manifest = load_manifest(manifest_path)
    info(f"Contract manifest: {manifest.binary_name} on {manifest.base_image}")

    rust_version = _detect_rust_version()
    dockerfile_content = compose_contract_dockerfile(
        manifest, rust_version=rust_version
    )

    return _build_from_content(
        dockerfile_content=dockerfile_content,
        container_cfg=container_cfg,
        config=config,
        org=org,
        registry_bases=registry_bases,
        push_to_main=push_to_main,
        extra_labels=manifest.labels,
    )


def _build_from_content(
    *,
    dockerfile_content: str,
    container_cfg: dict,
    config: CIConfig,
    org: OrgConfig,
    registry_bases: list[str],
    push_to_main: bool,
    extra_labels: dict[str, str] | None = None,
) -> int:
    """Write ``dockerfile_content`` to a temp file then build."""
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".Dockerfile",
        delete=False,
        dir=".",
    ) as f:
        f.write(dockerfile_content)
        dockerfile_path = Path(f.name)

    try:
        return _dispatch_build(
            dockerfile_path=dockerfile_path,
            container_cfg=container_cfg,
            config=config,
            org=org,
            registry_bases=registry_bases,
            push_to_main=push_to_main,
            extra_labels=extra_labels,
        )
    finally:
        dockerfile_path.unlink(missing_ok=True)


def _dispatch_build(
    *,
    dockerfile_path: Path,
    container_cfg: dict,
    config: CIConfig,
    org: OrgConfig,
    registry_bases: list[str],
    push_to_main: bool,
    extra_labels: dict[str, str] | None = None,
) -> int:
    image_name = Path.cwd().name
    version = _read_version()
    sha = _read_sha()
    channel = config.get("publish.channel", "release")

    tags = resolve_tags(
        registry_bases=registry_bases,
        image_name=image_name,
        version=version,
        sha=sha,
        channel=channel,
        is_push_to_main=push_to_main,
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

    platforms = container_cfg.get("platforms", ["linux/amd64", "linux/arm64"])
    build_args = container_cfg.get("build_args", {})
    context = container_cfg.get("context", ".")

    # On push-to-main the Build job only produces linux-amd64 (saves CI
    # time). If the project's Dockerfile COPYs from dist/<name>-linux-<arch>
    # then the arm64 platform run fails because the binary isn't there.
    # Constrain the validate-only path to platforms whose binaries are
    # actually present in dist/.
    if push_to_main:
        configured_platforms = list(platforms)
        platforms = _filter_platforms_to_available_binaries(
            platforms=platforms,
            image_name=image_name,
        )
        if not platforms:
            # No silent-success — if the project has container builds enabled,
            # missing binaries means the Build → Container artefact handoff
            # is broken. Fail loud so we never report "container green" without
            # actually producing an image. See:
            # /projects/hyperi-ci/docs/superpowers/specs/2026-05-01-container-stage-binary-placement-bug.md
            error(
                f"Container build configured for {configured_platforms} but no "
                f"matching dist/{image_name}-linux-<arch> binaries present. "
                f"Build stage failed to produce artefacts OR the Container job "
                f"can't see them (check actions/upload-artifact + "
                f"actions/download-artifact version compatibility)."
            )
            return 1

    # Bare `COPY <app> ...` lines in the Dockerfile reference a file in
    # the build context root that the upstream Build stage doesn't put
    # there — it puts arch-suffixed binaries in `dist/<app>-linux-<arch>`.
    # Rewrite the Dockerfile to use ${TARGETARCH} substitution so multi-arch
    # buildx works in a single invocation. No-op for Dockerfiles that
    # already use the parameterised form (ci-test-* / dfe-loader pattern).
    from hyperi_ci.container.binary_stage import stage_binary_dockerfile

    effective_dockerfile = stage_binary_dockerfile(dockerfile_path)
    rewrote = effective_dockerfile != dockerfile_path

    try:
        rc = build_and_push(
            dockerfile_path=effective_dockerfile,
            context=context,
            tags=tags,
            platforms=platforms,
            labels=labels,
            build_args=build_args if build_args else None,
            push=not push_to_main,
        )
    finally:
        if rewrote:
            effective_dockerfile.unlink(missing_ok=True)

    if rc == 0 and push_to_main:
        success("Container Dockerfile validated (no push on push-to-main)")
    return rc


_PLATFORM_TO_OS_ARCH = {
    "linux/amd64": "linux-amd64",
    "linux/arm64": "linux-arm64",
}


def _filter_platforms_to_available_binaries(
    *,
    platforms: list[str],
    image_name: str,
    dist_dir: Path | None = None,
) -> list[str]:
    """Drop platforms whose pre-built binary is missing from ``dist/``.

    On push-to-main the Build job builds a single arch by default
    (saves CI time); on workflow_dispatch it builds the full matrix.
    Multi-arch buildx fails on push-to-main with "binary not found"
    when one architecture's artefact is absent.

    Returns the subset of ``platforms`` whose corresponding binary
    exists in ``dist/``. Platforms not in the os-arch map (e.g.
    ``linux/s390x`` or future targets) pass through unchanged so we
    don't silently drop a build the project actually wants.
    """
    cwd = dist_dir or Path("dist")
    kept: list[str] = []
    for platform in platforms:
        os_arch = _PLATFORM_TO_OS_ARCH.get(platform)
        if os_arch is None:
            kept.append(platform)
            continue
        candidate = cwd / f"{image_name}-{os_arch}"
        if candidate.exists():
            kept.append(platform)
        else:
            info(
                f"  Container: skipping {platform} — "
                f"{candidate} not present (not built by current Build job)"
            )
    return kept


def _detect_rust_version() -> str:
    toolchain_file = Path("rust-toolchain.toml")
    if toolchain_file.exists():
        for line in toolchain_file.read_text().splitlines():
            if "channel" in line and "=" in line:
                return line.split("=")[1].strip().strip('"').strip("'")
    return "stable"
