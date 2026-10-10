# Project:   HyperI CI
# File:      src/hyperi_ci/dispatch.py
# Purpose:   Stage dispatcher -- routes to language-specific handlers
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Stage dispatcher: the entry point for every CI pipeline stage.

Detects the language, loads config and routes to the language handler via
``run_stage(stage)``.
"""

import importlib
import os
from pathlib import Path
from typing import Any, Protocol, cast

from hyperi_ci import native_tools, release_prepare, vendor
from hyperi_ci.common import (
    ReleaseVersionError,
    announce,
    error,
    group,
    info,
    is_ci,
    resolve_release_version,
    run_cmd,
    success,
    warn,
)
from hyperi_ci.config import VALID_PROJECT_STATUSES, CIConfig, load_config
from hyperi_ci.detect import detect_language
from hyperi_ci.languages.quality_common import (
    GateReasonRequiredError,
    mode_ceiling,
    note_quality_disabled,
    resolve_tool_mode,
)
from hyperi_ci.languages.rust._jobs import capped_cargo_jobs
from hyperi_ci.languages.tiering import (
    TEST_TIER_ENV,
    InvalidTestTierError,
    resolve_test_tier,
)
from hyperi_ci.quality import (
    charset,
    commit_validation,
    deprecated_files,
    droast,
    gitleaks,
    hadolint,
    lint_docs,
    lint_iac,
    repo_advisor,
    semgrep,
)


class StageRunFn(Protocol):
    """Type contract every per-language stage handler's ``run`` exposes.

    A handler module under ``hyperi_ci.languages.<lang>.<stage>`` exports a
    ``run`` matching this protocol. The dispatcher checks at runtime that it is
    present and callable, so a bad handler fails explicitly rather than with an
    AttributeError mid-stage.
    """

    def __call__(
        self, config: CIConfig, *, extra_env: dict[str, str] | None = ...
    ) -> int:
        """Run the stage with ``config``; optional ``extra_env`` overlays the process env."""
        ...


VALID_STAGES = (
    "setup",
    "quality",
    "test",
    "build",
    "container",
    "release",
    "publish",
)

# Appended to the "Project status: <X>" log line. `ga` adds nothing.
_STATUS_CLARIFIER: dict[str, str] = {
    "experimental": " -- pre-GA, no API commitment",
    "alpha": " -- pre-GA, expect breaks",
    "beta": " -- pre-GA, polishing",
    "ga": "",
    "legacy": " -- being phased out, plan migration",
    "deprecated": " -- do not adopt, scheduled for removal",
}

# Languages sharing a handler package: `detect_language()`'s name on the left,
# the handler module on the right, so logs still say "javascript".
_LANGUAGE_ALIASES = {
    "javascript": "typescript",
}

# `publish` stays accepted because hyperi-io/vector-vrl invokes
# `hyperi-ci run publish` directly from a hand-rolled workflow.
_STAGE_ALIASES = {
    "publish": "release",
}


def _find_handler_module(language: str, stage: str) -> Any | None:
    """Import a language-specific handler module if it exists.

    Looks for ``hyperi_ci.languages.<language>.<stage>``, through
    ``_LANGUAGE_ALIASES``, and returns the module if it has a callable ``run``.

    Returns ``None`` only when the module does not exist (ImportError). A module
    whose ``run`` is missing or not callable is a packaging bug and raises
    ``TypeError``, so it is not mistaken for "no handler".
    """
    stage = _STAGE_ALIASES.get(stage, stage)
    canonical = _LANGUAGE_ALIASES.get(language, language)
    if canonical != language:
        info(f"Using {canonical} handler for {language} project")
    module_name = f"hyperi_ci.languages.{canonical}.{stage}"
    try:
        # The module name comes from a closed set (languages + known stages).
        mod = importlib.import_module(module_name)  # nosemgrep: non-literal-import
    except ImportError:
        return None
    run_fn = getattr(mod, "run", None)
    if run_fn is None or not callable(run_fn):
        raise TypeError(
            f"{module_name}.run is missing or not callable. "
            f"Stage handlers must export `def run(config, *, extra_env=None) -> int`."
        )
    return mod


def _normalize_rust_features(config: CIConfig, stage: str) -> str:
    """Return Rust features as a string: stage-specific -> fallback -> "all".

    Arrays become pipe-separated strings.
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
    if not handler:
        return -1
    if _LANGUAGE_ALIASES.get(language, language) != "rust":
        return handler.run(config, extra_env=extra_env)
    # cargo sizes jobs by CPUs alone, which OOM-kills a runner whose memory
    # limit is below one rustc per CPU.
    with capped_cargo_jobs(config):
        return handler.run(config, extra_env=extra_env)


def stage_setup(language: str, config: CIConfig) -> int:
    """Environment setup -- dispatch to language-specific handler."""
    rc = _dispatch_to_handler(language, "setup", config)
    if rc == -1:
        error(f"Setup handler not found for {language}")
        return 1
    return rc


def _run_local_gates(config: CIConfig) -> int:
    """Run the CI-only gates a repo declares, so a local green means something.

    Gates that run as their own CI JOB are outside the quality stage, so
    `hyperi-ci check` can pass while CI fails. A repo lists those commands under
    `quality.local_gates` to run them in its local check. They are declared per
    repo because a path named here would be wrong for every other consumer.
    """
    gates = config.setting("quality.local_gates") or []
    if not gates:
        return 0

    for gate in gates:
        name = gate.get("name") if isinstance(gate, dict) else None
        command = gate.get("command") if isinstance(gate, dict) else None
        if not name or not isinstance(command, list) or not command:
            error(
                "quality.local_gates entries need a `name` and a `command` "
                f"list; got: {gate!r}"
            )
            return 1
        with group(f"Local gate: {name}"):
            result = run_cmd(command, check=False)
            if result.returncode != 0:
                error(
                    f"  {name}: failed locally, and it is a required job in "
                    f"CI. Fix it here rather than finding it after the push."
                )
                return result.returncode
            success(f"  {name}: ok")
    return 0


def _run_lint_iac(config: CIConfig) -> int:
    """Run lint-iac's quality dimensions over the repo under ``quality.iac``.

    ``warn`` caps every dimension's mode at ``warn``, so a blocking tool reports
    warnings and the stage passes. ``blocking`` runs each dimension at its own
    mode and returns the result, and ``disabled`` runs nothing. Each dimension
    opens its own log group, so this opens none.
    """
    mode = resolve_tool_mode("iac", config, default="warn")
    if mode == "disabled":
        info("lint-iac: quality.iac is disabled - skipping")
        return 0
    if mode == "blocking":
        return lint_iac.run(Path.cwd(), config, dimensions=lint_iac.QUALITY_DIMENSIONS)
    with mode_ceiling("warn"):
        rc = lint_iac.run(Path.cwd(), config, dimensions=lint_iac.QUALITY_DIMENSIONS)
    if rc != 0:
        warn(
            "lint-iac: a dimension failed to run, which does not fail the "
            "quality stage while quality.iac is warn"
        )
    return 0


def stage_quality(language: str, config: CIConfig, *, local: bool = False) -> int:
    """Quality checks -- gitleaks + language-specific checks."""
    # A non-fatal nudge, so it runs first and even when quality is disabled.
    with group("Deprecated file check"):
        deprecated_files.scan()

    if not config.setting("quality.enabled"):
        note_quality_disabled(
            _LANGUAGE_ALIASES.get(language, language),
            str(config.get("quality.reason") or ""),
        )
        return 0

    # Advisory only (quality.alint). The language scopes the default config so
    # other ecosystems' root-only rules skip nested monorepo packages (#75).
    with group("Repo hygiene advisory (alint)"):
        repo_advisor.run(config, language=language)

    with group("Gitleaks secret scanning"):
        rc = gitleaks.run(config)
        if rc != 0:
            return rc

    # The ASCII-only rule covers every language, so it runs once here (#169).
    with group("Character policy"):
        rc = charset.run(config)
        if rc != 0:
            return rc

    if config.get("vendor"):
        with group("Vendored files"):
            rc = vendor.run(config, Path.cwd())
            if rc != 0:
                return rc

    # Cross-language, so semgrep runs once here, not per handler.
    with group("Semgrep SAST scanning"):
        rc = semgrep.run(config, language=language)
        if rc != 0:
            return rc

    # hadolint GATES on error severity (including a broken RUN shell), droast
    # ADVISES and never blocks. Both skip a repo with no Dockerfile.
    with group("hadolint Dockerfile linting"):
        rc = hadolint.run(config)
        if rc != 0:
            return rc
    with group("droast Dockerfile advisory"):
        droast.run(config)

    rc = _run_lint_iac(config)
    if rc != 0:
        return rc

    # Every doc check defaults to `warn` until a repo promotes it.
    docs_rc = lint_docs.run(Path.cwd(), config)
    if docs_rc != 0:
        return docs_rc

    # In CI the `commit-check` job validates messages. A LOCAL `hyperi-ci check`
    # has no such job, so it validates origin/main..HEAD here before the push.
    if local:
        with group("Commit message validation"):
            rc = commit_validation.run(config, local=True)
            if rc != 0:
                return rc

        rc = _run_local_gates(config)
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
    """Run tests -- dispatch to language-specific handler."""
    if not config.setting("test.enabled"):
        info("Tests disabled in configuration")
        return 0

    try:
        test_tier = resolve_test_tier(config)
    except InvalidTestTierError as exc:
        error(str(exc))
        return 1
    info(f"Test tier: {test_tier}")

    extra_env: dict[str, str] = {TEST_TIER_ENV: test_tier.value}
    if language == "rust":
        features = _normalize_rust_features(config, "test")
        extra_env["RUST_FEATURES"] = features
        info(f"Rust features config: {features}")

    if native_tools.prepare(config) != 0:
        return 1

    rc = _dispatch_to_handler(language, "test", config, extra_env=extra_env)
    if rc == -1:
        # A missing handler is a packaging bug, not "no tests". A project with
        # none sets `test.enabled: false`.
        error(
            f"No test handler found for language {language!r}. "
            f"This is a hyperi-ci bug (handler module "
            f"hyperi_ci.languages.{language}.test missing or has no run() function)."
        )
        return 1
    return rc


def stage_build(language: str, config: CIConfig, *, local: bool = False) -> int:
    """Build -- supports multiple strategies."""
    if not config.setting("build.enabled"):
        info("Build disabled in configuration")
        return 0

    strategies = config.setting("build.strategies")
    if isinstance(strategies, str):
        strategies = [strategies]

    for strategy in strategies:
        with group(f"Building with strategy: {strategy}"):
            extra_env: dict[str, str] = {"BUILD_STRATEGY": strategy}

            if strategy == "native":
                if language == "rust":
                    features = _normalize_rust_features(config, "build")
                    # The env var (from the workflow matrix) beats config, so
                    # split-runner builds get one target per entry.
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

            else:
                error(f"Unknown build strategy: {strategy}")
                return 1

    from hyperi_ci.release.assemble import emit_contract

    return emit_contract(config, Path.cwd())


def check_prepared(language: str) -> int:
    """Refuse a prepared directory written for another version, language or commit.

    It came from a job that ran repo code, so it is checked against this run
    before anything reads it. A CI run with none is on a release tail that
    predates the split, and warns because the repo's code then runs beside the
    tokens.
    """
    ok, prepared = release_prepare.load_or_report("release")
    if not ok:
        return 1
    if prepared is None:
        if is_ci():
            announce(
                f"{release_prepare.PREPARED_ENV} is not set, so this release runs "
                "the repo's own code (semver checks, npm pack) in the process "
                "that holds the publish tokens. Move the caller to the current "
                "hyperi-ci release tail (issue #409).",
                "hyperi-ci release without a prepare job",
                level="warning",
            )
        return 0
    version = resolve_release_version()
    if prepared.version != version:
        error(
            f"Prepared v{prepared.version} but this run releases v{version} -- "
            "refusing to upload artefacts built for another version"
        )
        return 1
    canonical = _LANGUAGE_ALIASES.get(language, language)
    if prepared.language != canonical:
        error(f"Prepared a {prepared.language} release, but this is {canonical}")
        return 1
    here = release_prepare.head_commit(Path.cwd())
    if prepared.head and here and prepared.head != here:
        error(
            f"Prepared from {prepared.head[:8]} but this checkout is {here[:8]} -- "
            "a commit landed while the release ran, so the tag would go on a "
            "commit the artefacts were not built from. Re-run the release."
        )
        return 1
    info(f"Uploading v{version} as prepared; no repo code runs in this stage")
    return 0


def stage_release(language: str, config: CIConfig) -> int:
    """Release -- CI-only, dispatch to language-specific handler + binary upload."""
    if not is_ci():
        error("Releasing can ONLY be done in GitHub Actions")
        info("To release: commit, push, and let semantic-release handle it")
        return 1

    if not config.setting("release.enabled"):
        info("Release disabled in configuration")
        return 0

    channel = config.setting("release.channel")
    if channel != "release":
        info(
            f"Channel '{channel}' -- non-release channels currently publish "
            "to the same OSS destinations as 'release'. Pre-GA staging on "
            "private registries was retired in v2.1.4."
        )

    rc = check_prepared(language)
    if rc != 0:
        return rc

    rc = _dispatch_to_handler(language, "release", config)
    if rc == -1:
        error(f"Release handler not found for {language}")
        return 1
    if rc != 0:
        return rc

    from hyperi_ci.release import (
        create_github_release,
        publish_binaries,
        stage_release_assets,
    )

    # Staged first, so a missing asset fails before anything is published.
    rc = stage_release_assets(config)
    if rc != 0:
        return rc

    rc = create_github_release(config)
    if rc != 0:
        return rc

    # Last, so a binary-upload failure cannot cost the registry publish or the
    # GitHub Release.
    return publish_binaries(config)


def stage_container(language: str, config: CIConfig) -> int:
    """Container build -- cross-language stage, delegates to container package."""
    from hyperi_ci.container.stage import run as container_run

    return container_run(config, language=language)


_STAGE_HANDLERS = {
    "setup": stage_setup,
    "quality": stage_quality,
    "test": stage_test,
    "build": stage_build,
    "container": stage_container,
    "release": stage_release,
}


def run_stage(
    stage: str,
    *,
    project_dir: Path | None = None,
    local: bool = False,
) -> int:
    """Run a CI stage.

    Args:
        stage: Stage name (setup, quality, test, build, release).
        project_dir: Project root directory. Defaults to cwd.
        local: If True, ignore `build.rust.targets` and build the host target.

    Returns:
        Exit code (0 = success).

    """
    stage = _STAGE_ALIASES.get(stage, stage)
    if stage not in _STAGE_HANDLERS:
        error(f"Unknown stage: {stage}")
        error(f"Valid stages: {', '.join(VALID_STAGES)}")
        return 1

    # Handlers run tools in the process cwd, so `-C` must move the process
    # (issue #109).
    project_dir = (project_dir or Path.cwd()).resolve()
    os.chdir(project_dir)
    info(f"HyperI CI -- {stage}")

    language = detect_language(project_dir)
    if not language:
        if stage == "test":
            warn("Could not detect project language -- skipping tests")
            return 0
        error("Could not detect project language")
        return 1

    info(f"Detected language: {language}")

    config = load_config(reload=True, project_dir=project_dir)

    # Logged at INFO when declared, as a beta project is a fact, not an error.
    status = str(config.get("project.status") or "").strip().lower()
    if status in VALID_PROJECT_STATUSES:
        info(f"Project status: {status}{_STATUS_CLARIFIER.get(status, '')}")

    handler = _STAGE_HANDLERS[stage]
    try:
        if stage in ("build", "quality"):
            # These two take `local`, but _STAGE_HANDLERS is typed without it.
            rc = cast("Any", handler)(language, config, local=local)
        else:
            rc = handler(language, config)
    except GateReasonRequiredError as exc:
        # A relaxed security gate with no reason is a config defect, and
        # continuing would be a silent skip.
        announce(str(exc), exc.title, level="error")
        return 1
    except ReleaseVersionError as exc:
        error(f"{stage}: {exc}")
        return 1

    if rc == 0:
        success(f"{stage} complete")
    return rc
