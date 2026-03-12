# Project:   HyperI CI
# File:      src/hyperi_ci/dispatch.py
# Purpose:   Stage dispatcher — routes to language-specific handlers
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Stage dispatcher for HyperI CI.

Single entry point for all CI pipeline stages. Handles language detection,
config loading, and dispatches to the appropriate language-specific handler.

Usage:
    from hyperi_ci.dispatch import run_stage
    rc = run_stage("quality")
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Any

from hyperi_ci.common import (
    error,
    group,
    info,
    is_ci,
    success,
    warn,
)
from hyperi_ci.config import CIConfig, load_config
from hyperi_ci.detect import detect_language
from hyperi_ci.quality import gitleaks

VALID_STAGES = ("setup", "quality", "test", "build", "publish")


def _find_handler_module(language: str, stage: str) -> Any | None:
    """Import a language-specific handler module if it exists.

    Looks for hyperi_ci.languages.<language>.<stage> and returns the module
    if it has a run() function.
    """
    module_name = f"hyperi_ci.languages.{language}.{stage}"
    try:
        mod = importlib.import_module(module_name)
        if hasattr(mod, "run"):
            return mod
    except ImportError:
        pass
    return None


def _normalize_rust_features(config: CIConfig, stage: str) -> str:
    """Extract and normalise Rust features from config.

    Handles the config cascade (stage-specific -> fallback -> "all")
    and converts arrays to pipe-separated strings.
    """
    features: Any = None

    if stage in ("build", "quality", "test"):
        features = config.get(f"{stage}.rust.features")

    if features is None and stage in ("quality", "test"):
        for fallback in ("quality", "test"):
            features = config.get(f"{fallback}.rust.features")
            if features is not None:
                break

    if features is None:
        features = "all"

    if isinstance(features, list):
        features = "|".join(str(f) for f in features)

    return str(features)


def _dispatch_to_handler(
    language: str,
    stage: str,
    config: CIConfig,
    extra_env: dict[str, str] | None = None,
) -> int:
    """Dispatch to a Python handler module.

    Returns -1 if no handler found, otherwise the handler's return code.
    """
    handler = _find_handler_module(language, stage)
    if handler:
        return handler.run(config, extra_env=extra_env)
    return -1


def stage_setup(language: str, config: CIConfig) -> int:
    """Environment setup — dispatch to language-specific handler."""
    rc = _dispatch_to_handler(language, "setup", config)
    if rc == -1:
        error(f"Setup handler not found for {language}")
        return 1
    return rc


def stage_quality(language: str, config: CIConfig) -> int:
    """Quality checks — gitleaks + language-specific checks."""
    if not config.get("quality.enabled", True):
        info("Quality checks disabled in configuration")
        return 0

    # Cross-language checks first
    with group("Gitleaks secret scanning"):
        rc = gitleaks.run(config)
        if rc != 0:
            return rc

    extra_env: dict[str, str] = {}
    if language == "rust":
        features = _normalize_rust_features(config, "quality")
        extra_env["RUST_FEATURES"] = features
        info(f"Rust features config: {features}")

    rc = _dispatch_to_handler(language, "quality", config, extra_env=extra_env)
    if rc == -1:
        error(f"Quality handler not found for {language}")
        return 1
    return rc


def stage_test(language: str, config: CIConfig) -> int:
    """Tests — dispatch to language-specific handler."""
    extra_env: dict[str, str] = {}
    if language == "rust":
        features = _normalize_rust_features(config, "test")
        extra_env["RUST_FEATURES"] = features
        info(f"Rust features config: {features}")

    rc = _dispatch_to_handler(language, "test", config, extra_env=extra_env)
    if rc == -1:
        warn(f"No test handler found for {language} — skipping tests")
        return 0
    return rc


def stage_build(language: str, config: CIConfig, *, local: bool = False) -> int:
    """Build — supports multiple strategies."""
    if not config.get("build.enabled", True):
        info("Build disabled in configuration")
        return 0

    strategies = config.get("build.strategies", ["native"])
    if isinstance(strategies, str):
        strategies = [strategies]

    for strategy in strategies:
        with group(f"Building with strategy: {strategy}"):
            extra_env: dict[str, str] = {"BUILD_STRATEGY": strategy}

            if strategy == "native":
                if language == "rust":
                    features = _normalize_rust_features(config, "build")
                    # Environment override (from workflow matrix) takes
                    # precedence over config — allows split-runner builds
                    # to specify a single target per matrix entry.
                    env_targets = os.environ.get("RUST_BUILD_TARGETS", "")
                    if env_targets:
                        extra_env["RUST_BUILD_TARGETS"] = env_targets
                    elif not local:
                        rust_targets = config.get("build.rust.targets", [])
                        if isinstance(rust_targets, list):
                            extra_env["RUST_BUILD_TARGETS"] = ",".join(rust_targets)
                    extra_env["RUST_ALL_FEATURES"] = (
                        "true" if features == "all" else "false"
                    )
                    if features not in ("all", "default"):
                        extra_env["RUST_FEATURES"] = features

                rc = _dispatch_to_handler(
                    language,
                    "build",
                    config,
                    extra_env=extra_env,
                )
                if rc == -1:
                    error(f"Build handler not found for {language}")
                    return 1
                if rc != 0:
                    return rc

            elif strategy == "nuitka":
                if language != "python":
                    warn(f"Nuitka strategy is Python-only, skipping for {language}")
                    continue
                rc = _dispatch_to_handler(
                    language,
                    "build",
                    config,
                    extra_env=extra_env,
                )
                if rc == -1:
                    error("Nuitka build handler not found for Python")
                    return 1
                if rc != 0:
                    return rc

            else:
                error(f"Unknown build strategy: {strategy}")
                return 1

    return 0


def stage_publish(language: str, config: CIConfig) -> int:
    """Publish — CI-only, dispatch to language-specific handler + binary upload."""
    if not is_ci():
        error("Publishing can ONLY be done in GitHub Actions")
        info("To publish: commit, push, and let semantic-release handle it")
        return 1

    if not config.get("publish.enabled", False):
        info("Publish disabled in configuration")
        return 0

    # Language-specific publish (crates, PyPI, npm, go proxy)
    rc = _dispatch_to_handler(language, "publish", config)
    if rc == -1:
        error(f"Publish handler not found for {language}")
        return 1
    if rc != 0:
        return rc

    # Generic binary publish (any language with dist/ artifacts)
    from hyperi_ci.publish_binaries import publish_binaries

    return publish_binaries(config)


_STAGE_HANDLERS = {
    "setup": stage_setup,
    "quality": stage_quality,
    "test": stage_test,
    "build": stage_build,
    "publish": stage_publish,
}


def run_stage(
    stage: str,
    *,
    project_dir: Path | None = None,
    local: bool = False,
) -> int:
    """Run a CI stage.

    Args:
        stage: Stage name (setup, quality, test, build, publish).
        project_dir: Project root directory. Defaults to cwd.
        local: If True, skip cross-compilation targets (native build only).

    Returns:
        Exit code (0 = success).
    """
    if stage not in _STAGE_HANDLERS:
        error(f"Unknown stage: {stage}")
        error(f"Valid stages: {', '.join(VALID_STAGES)}")
        return 1

    project_dir = project_dir or Path.cwd()
    info(f"HyperI CI — {stage}")

    language = detect_language(project_dir)
    if not language:
        if stage == "test":
            warn("Could not detect project language — skipping tests")
            return 0
        error("Could not detect project language")
        return 1

    info(f"Detected language: {language}")

    config = load_config(reload=True, project_dir=project_dir)

    handler = _STAGE_HANDLERS[stage]
    if stage == "build":
        rc = handler(language, config, local=local)
    else:
        rc = handler(language, config)

    if rc == 0:
        success(f"{stage} complete")
    return rc
