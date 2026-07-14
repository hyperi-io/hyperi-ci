# Project:   HyperI CI
# File:      src/hyperi_ci/container/stage.py
# Purpose:   Container build stage handler
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Container build stage.

Three-state ``publish.container.enabled`` gate:

* ``auto`` (default): build when a container artefact is detected
  (Dockerfile in repo, or scalo contract source). Library projects
  and projects with no signal skip silently.
* ``true``: build is required. Template languages (python /
  typescript) build from the language template even with no detected
  artefact — this is how a Python/TS service opts in now that a bare
  console-script no longer auto-containerises (issue #51). Contract /
  custom languages fail loudly if no signal is present — surfaces a
  regression where a project lost its containerisable artefact.
* ``false``: explicit skip.

Every container is built and (in publish mode) pushed to GHCR. The
legacy ``publish.target`` field is accepted for back-compat but ignored
— JFrog publishing was removed in v2.1.4.

Push modes (resolved by :mod:`hyperi_ci.publish_mode` — the SSOT):

* ``publish``  — release dispatch / Publish-trailer push to main: full
  tag set, pushed.
* ``dev``      — branch-mode dev image (plan decision 3): mutable
  ``branch-<slug>`` + ``sha-<short>`` tags to GHCR only, behind the
  ``publish.container.dev_push`` opt-in on pull_request / branch CI
  runs. Never version tags, never ``latest``.
* ``validate`` — push-to-main and local runs: build, no push.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from hyperi_ci.common import (
    error,
    group,
    info,
    resolve_release_version,
    success,
    warn,
)
from hyperi_ci.config import CIConfig, OrgConfig, load_org_config
from hyperi_ci.container.build import build_and_push, resolve_tags
from hyperi_ci.container.detect import Decision, detect
from hyperi_ci.container.labels import build_oci_labels
from hyperi_ci.container.registry import resolve_registry_bases
from hyperi_ci.publish_mode import (
    DEV,
    PUBLISH,
    VALIDATE,
    dev_branch_slug,
    resolve_push_mode,
)

_TEMPLATE_LANGUAGES = {"python", "typescript"}
_CONTRACT_LANGUAGES = {"rust"}


def _read_version() -> str:
    """Resolve the version this container should be tagged with.

    Shares the HYPERCI_VERSION-first resolver with the publish stages (one
    SSoT — common.resolve_release_version, issue #27). Container needs a
    concrete tag even with no env/VERSION, so it falls back to the ref then
    "0.0.0".
    """
    return resolve_release_version() or os.environ.get(
        "GITHUB_REF_NAME", "0.0.0"
    ).removeprefix("v")


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
    """Return the DEPRECATED bool view (delegates to :mod:`hyperi_ci.publish_mode`).

    Kept for out-of-tree callers; in-tree code uses the tri-state
    :func:`hyperi_ci.publish_mode.resolve_push_mode` (branch-mode).
    """
    return resolve_push_mode() == PUBLISH


def _is_push_to_main() -> bool:
    """Return ``not _is_publish_mode()`` (deprecated alias for out-of-tree callers).

    The legacy ``push_to_main`` flag was the validate-only signal,
    named confusingly. Will be removed once consumers update.
    """
    return not _is_publish_mode()


def _dev_push_opt_in(container_cfg: dict) -> bool:
    """Return the ``publish.container.dev_push`` opt-in, coerced to bool."""
    raw = container_cfg.get("dev_push", False)
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "1", "yes")
    return bool(raw)


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


def should_build_container(config: CIConfig, *, language: str = "") -> tuple[bool, str]:
    """Resolve whether the container stage will build — filesystem only.

    Mirrors :func:`run`'s gate so the workflow can decide BEFORE booting
    Docker Buildx (issue #33): ``enabled: false`` never builds;
    ``enabled: true`` always builds — template languages via the language
    template when no artefact is detected (the Python/TS service opt-in,
    issue #51), contract/custom languages then fail loudly in :func:`run`;
    ``enabled: auto`` builds iff :func:`detect` finds a signal. A library
    (e.g. a Rust crate — no GHCR deployment) has no signal, so the job
    never pulls buildkit from Docker Hub nor logs in to GHCR.

    Returns ``(build, reason)``.
    """
    container_cfg = config.get("publish.container", {})
    if not isinstance(container_cfg, dict):
        container_cfg = {}
    enabled = _normalise_enabled(container_cfg.get("enabled", "auto"))
    if enabled == "false":
        return False, "publish.container.enabled: false"
    if enabled == "true":
        return True, "publish.container.enabled: true"
    decision = detect(
        language=language,
        project_dir=Path.cwd(),
        dockerfile=container_cfg.get("dockerfile", "Dockerfile"),
    )
    return decision.build, decision.reason


def run(config: CIConfig, *, language: str = "") -> int:
    """Run the container build stage.

    Args:
        config: Merged CI configuration.
        language: Detected project language.

    Returns:
        Exit code (0 = success or skipped).

    """
    # Resolve-only: emit the build decision for the workflow to gate Docker
    # setup on, then return without any Docker work (issue #33). Keeps
    # libraries from booting Buildx / touching GHCR at all.
    if os.environ.get("HYPERCI_CONTAINER_RESOLVE_ONLY"):
        build, reason = should_build_container(config, language=language)
        info(f"Container resolve: build={'true' if build else 'false'} — {reason}")
        gh_out = os.environ.get("GITHUB_OUTPUT")
        if gh_out:
            with open(gh_out, "a", encoding="utf-8") as fh:
                fh.write(f"build={'true' if build else 'false'}\n")
        return 0

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
            # Build is required. Template languages (python / typescript)
            # can always build from the language template with no detected
            # artefact — this is how a genuine Python service opts in now
            # that a bare console-script no longer auto-containerises
            # (issue #51). Contract / custom languages (e.g. a Rust crate
            # with no Dockerfile and no scalo contract) genuinely have
            # nothing to ship, so a required build is a hard fail.
            if language in _TEMPLATE_LANGUAGES:
                info(
                    "publish.container.enabled: true — building "
                    f"{language} via template despite: {decision.reason}"
                )
                decision = Decision(
                    build=True,
                    reason=(
                        f"forced by publish.container.enabled: true "
                        f"({language} template)"
                    ),
                    mode="template",
                )
            else:
                error(
                    "publish.container.enabled: true but no container artefact "
                    f"detected — {decision.reason}",
                )
                return 1
        else:
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

    push_mode = resolve_push_mode(dev_push=_dev_push_opt_in(container_cfg))
    mode = _resolve_mode(
        language=language,
        decision=decision,
        container_cfg=container_cfg,
    )
    info(f"Container build mode: {mode} ({push_mode})")

    with group(f"Container Build ({mode})"):
        if mode == "contract":
            return _build_contract(
                config=config,
                container_cfg=container_cfg,
                org=org,
                registry_bases=registry_bases,
                push_mode=push_mode,
            )
        if mode == "template":
            return _build_template(
                language=language,
                config=config,
                container_cfg=container_cfg,
                org=org,
                registry_bases=registry_bases,
                push_mode=push_mode,
            )
        if mode == "custom":
            return _build_custom(
                container_cfg=container_cfg,
                config=config,
                org=org,
                registry_bases=registry_bases,
                push_mode=push_mode,
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
    push_mode: str,
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
        push_mode=push_mode,
    )


def _build_template(
    *,
    language: str,
    config: CIConfig,
    container_cfg: dict,
    org: OrgConfig,
    registry_bases: list[str],
    push_mode: str,
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
        push_mode=push_mode,
    )


def _build_contract(
    *,
    config: CIConfig,
    container_cfg: dict,
    org: OrgConfig,
    registry_bases: list[str],
    push_mode: str,
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
        push_mode=push_mode,
        extra_labels=manifest.labels,
    )


def _build_from_content(
    *,
    dockerfile_content: str,
    container_cfg: dict,
    config: CIConfig,
    org: OrgConfig,
    registry_bases: list[str],
    push_mode: str,
    extra_labels: dict[str, str] | None = None,
) -> int:
    """Write ``dockerfile_content`` to a temp file then build.

    Before writing, splice any ``publish.container.overlays:`` declared
    in ``.hyperi-ci.yaml`` into the Dockerfile content. See
    ``deployment/overlay/`` and the framework spec.
    """
    dockerfile_content = _splice_dockerfile_overlays(
        dockerfile_content=dockerfile_content,
        container_cfg=container_cfg,
    )

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
            push_mode=push_mode,
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
    push_mode: str,
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
        mode=push_mode,
        branch_slug=dev_branch_slug() if push_mode == DEV else "",
    )

    from hyperi_ci.init import detect_license

    labels = build_oci_labels(
        repo=f"{org.github_org}/{image_name}",
        revision=os.environ.get("GITHUB_SHA", _read_sha()),
        version=version,
        title=image_name,
        licenses=detect_license(Path.cwd()),
    )
    if extra_labels:
        labels.update(extra_labels)
    cfg_labels = container_cfg.get("labels", {})
    if cfg_labels:
        labels.update(cfg_labels)

    platforms = container_cfg.get("platforms", ["linux/amd64", "linux/arm64"])
    build_args = container_cfg.get("build_args", {})
    context = container_cfg.get("context", ".")

    # Outside a GA publish the Build job only produces linux-amd64 (saves
    # CI time on push-to-main validates AND branch dev builds). If the
    # project's Dockerfile COPYs from dist/<name>-linux-<arch> then the
    # arm64 platform run fails because the binary isn't there. Constrain
    # non-publish paths to platforms whose binaries are actually present.
    if push_mode != PUBLISH:
        configured_platforms = list(platforms)
        platforms = _filter_platforms_to_available_binaries(
            platforms=platforms,
            image_name=image_name,
        )
        if not platforms:
            # No silent-success — if the project has container builds enabled,
            # missing binaries means the Build → Container artefact handoff
            # is broken. Fail loud so we never report "container green" without
            # actually producing an image.
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
            push=push_mode != VALIDATE,
        )
    finally:
        if rewrote:
            effective_dockerfile.unlink(missing_ok=True)

    if rc == 0 and push_mode == VALIDATE:
        success("Container Dockerfile validated (no push on push-to-main)")
    elif rc == 0 and push_mode == DEV:
        success(
            "Dev image pushed (branch artifact class — GHCR only, "
            "mutable branch tag; GA publish untouched)"
        )
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


def _splice_dockerfile_overlays(
    *,
    dockerfile_content: str,
    container_cfg: dict,
) -> str:
    """Apply ``publish.container.overlays`` to ``dockerfile_content``.

    No-op when no overlays are declared. Imports the overlay module
    lazily so projects without overlays don't pay the import cost.

    Anchor catalog + splice mechanics live in
    ``hyperi_ci.deployment.overlay.anchors.dockerfile``. Errors (missing
    anchor, missing fragment file, malformed declaration) propagate so
    the build fails loudly with an actionable message.
    """
    raw_overlays = container_cfg.get("overlays")
    if not raw_overlays:
        return dockerfile_content

    from hyperi_ci.deployment.overlay import apply_overlays
    from hyperi_ci.deployment.overlay.anchors.dockerfile import (
        DockerfileAnchorResolver,
    )
    from hyperi_ci.deployment.overlay.model import parse_simple_overlays

    overlays = parse_simple_overlays(raw_overlays, artefact="container")
    binary_name = container_cfg.get("binary_name") or Path.cwd().name
    resolver = DockerfileAnchorResolver(binary_name=binary_name)

    info(
        f"  Container: applying {len(overlays)} overlay(s) to Dockerfile "
        f"(anchors used: {sorted({o.anchor for o in overlays})})"
    )
    return apply_overlays(
        base=dockerfile_content,
        overlays=overlays,
        resolver=resolver,
        base_dir=Path.cwd(),
        artefact="container",
    )


def _detect_rust_version() -> str:
    toolchain_file = Path("rust-toolchain.toml")
    if toolchain_file.exists():
        for line in toolchain_file.read_text().splitlines():
            if "channel" in line and "=" in line:
                return line.split("=")[1].strip().strip('"').strip("'")
    return "stable"
