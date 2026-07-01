# Project:   HyperI CI
# File:      src/hyperi_ci/cli.py
# Purpose:   CLI entry point for hyperi-ci tool (Typer via scalo)
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""CLI entry point for HyperI CI.

Usage:
    hyperi-ci run <stage>               Run a CI stage (setup, quality, test, build, publish)
    hyperi-ci check                     Pre-push checks (quality + test; --full adds build)
    hyperi-ci push                      Push with pre-checks (replaces bare git push)
    hyperi-ci init                      Initialise project (config, Makefile, workflow)
    hyperi-ci detect                    Detect project language
    hyperi-ci config                    Show merged configuration
    hyperi-ci trigger                   Trigger a GitHub Actions workflow run
    hyperi-ci watch [RUN_ID]            Watch a GitHub Actions run to completion
    hyperi-ci logs [RUN_ID]             Fetch and filter GitHub Actions run logs
    hyperi-ci release <tag>             Trigger publish for a version tag
    hyperi-ci check-commit              Validate commit message format
    hyperi-ci stitch <topology-dir>     Stitch a DeploymentTopology into an umbrella Helm chart
    hyperi-ci --version                 Show version

Conventions (all commands):
    -V, --version      Show version and exit (global only)
    -C, --project-dir  Project root directory
    -n, --dry-run      Show what would happen without executing
    -f, --force        Skip confirmations / overwrite (semantics per-command)

Help:
    hyperi-ci --help          List all commands
    hyperi-ci <cmd> --help    Show command-specific options

When adding new commands, respect these short-flag conventions so users can
rely on muscle memory. In particular:
  - Never repurpose -n for anything other than --dry-run
  - Never repurpose -C for anything other than --project-dir
  - --force semantics vary (overwrite vs skip-checks) — document in each command
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from hyperi_ci import __version__
from hyperi_ci.config import load_config
from hyperi_ci.detect import detect_language
from hyperi_ci.dispatch import VALID_STAGES, run_stage

app = typer.Typer(
    name="hyperi-ci",
    help="HyperI CI — polyglot CI/CD tool",
    no_args_is_help=True,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"hyperi-ci {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            help="Show version and exit",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """HyperI CI — polyglot CI/CD tool."""
    from hyperi_ci.upgrade import maybe_auto_update

    maybe_auto_update()


@app.command()
def run(
    stage: Annotated[str, typer.Argument(help="Stage to run")],
    project_dir: Annotated[
        str | None,
        typer.Option("--project-dir", "-C", help="Project root directory"),
    ] = None,
) -> None:
    """Run a CI stage (setup, quality, test, build, publish)."""
    if stage not in VALID_STAGES:
        typer.echo(f"Invalid stage: {stage}", err=True)
        typer.echo(f"Valid stages: {', '.join(VALID_STAGES)}", err=True)
        raise typer.Exit(1)

    dir_path = Path(project_dir) if project_dir else None
    rc = run_stage(stage, project_dir=dir_path)
    raise typer.Exit(rc)


@app.command()
def check(
    project_dir: Annotated[
        str | None,
        typer.Option("--project-dir", "-C", help="Project root directory"),
    ] = None,
    full: Annotated[
        bool,
        typer.Option("--full", help="Include build stage (native target only)"),
    ] = False,
    quick: Annotated[
        bool,
        typer.Option("--quick", help="Quality checks only (skip tests)"),
    ] = False,
) -> None:
    """Run local pre-push checks (quality + test by default)."""
    dir_path = Path(project_dir) if project_dir else None

    stages = ["quality"]
    if not quick:
        stages.append("test")
    if full:
        stages.append("build")

    for stage in stages:
        rc = run_stage(stage, project_dir=dir_path, local=True)
        if rc != 0:
            raise typer.Exit(rc)

    raise typer.Exit(0)


@app.command()
def push(
    publish: Annotated[
        bool,
        typer.Option(
            "--publish",
            "--release",  # back-compat alias
            help=(
                "Stamp HEAD with `Publish: true` trailer before pushing — "
                "the single CI run will tag + publish via the version-first "
                "pipeline. (--release is a deprecated alias for --publish.)"
            ),
        ),
    ] = False,
    bump_patch: Annotated[
        bool,
        typer.Option(
            "--bump-patch",
            help=(
                "Force a +0.0.1 patch release even when HEAD commits "
                "aren't release-worthy (e.g. docs-only). Adds an empty "
                "`fix(release): force patch bump` marker commit and "
                "publishes. Implies --publish."
            ),
        ),
    ] = False,
    bump_minor: Annotated[
        bool,
        typer.Option(
            "--bump-minor",
            help=(
                "Force a +0.1.0 minor release even when HEAD commits "
                "aren't release-worthy. Adds an empty "
                "`feat(release): force minor bump` marker commit and "
                "publishes. Implies --publish. (Major bumps require a "
                "human-written BREAKING CHANGE: footer per HyperI "
                "commit-type discipline.)"
            ),
        ),
    ] = False,
    no_ci: Annotated[
        bool,
        typer.Option("--no-ci", help="Amend last commit with [skip ci] and push"),
    ] = False,
    allow_feat: Annotated[
        bool,
        typer.Option(
            "--allow-feat",
            help=(
                "Equivalent to setting HYPERCI_ALLOW_FEAT=1 — opts in to a "
                "feat: commit (MINOR bump). Required when HEAD is a feat: "
                "commit and you're using --publish, since the trailer "
                "amend re-invokes the commit-msg hook gate."
            ),
        ),
    ] = False,
    allow_breaking: Annotated[
        bool,
        typer.Option(
            "--allow-breaking",
            help=(
                "Equivalent to setting HYPERCI_ALLOW_BREAKING=1 — opts in "
                "to a commit containing the BREAKING-CHANGE marker (MAJOR "
                "bump). Required when HEAD has the marker and you're "
                "using --publish."
            ),
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Show what would happen without pushing"),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force", "-f", help="Skip pre-push checks (does NOT force-push)"
        ),
    ] = False,
    project_dir: Annotated[
        str | None,
        typer.Option("--project-dir", "-C", help="Project root directory"),
    ] = None,
) -> None:
    """Push with pre-checks. Replaces bare ``git push``.

    Default flow: runs quality + test checks, rebases, then pushes.

    With ``--publish`` (canonical) or ``--release`` (alias): amends the
    head commit with the ``Publish: true`` trailer, then pushes. The
    resulting CI run goes through the version-first pipeline — predicts
    the next version, stamps it into Cargo.toml/VERSION before build,
    creates the tag, and publishes to all configured registries — all
    in one workflow.

    With ``--bump-patch`` or ``--bump-minor``: same as ``--publish`` but
    adds an empty release-marker commit on top of HEAD. Use this when
    you want to ship a release whose actual commits are no-bump types
    (``docs:``, ``chore:``, etc.) — saves you from inventing a fake
    ``fix:`` commit. The marker IS a real commit in git history with a
    clear conventional message stating "this is a forced bump."

    With ``--no-ci``: amends the last commit with ``[skip ci]`` and
    pushes (skips CI altogether).
    """
    import os

    from hyperi_ci.push import push as do_push

    if bump_patch and bump_minor:
        typer.echo("--bump-patch and --bump-minor are mutually exclusive", err=True)
        raise typer.Exit(1)
    bump = "patch" if bump_patch else "minor" if bump_minor else None

    # CLI flag → env var: the commit-msg hook (which fires during the
    # trailer amend inside _publish_push) reads HYPERCI_ALLOW_FEAT /
    # HYPERCI_ALLOW_BREAKING. Setting them here means a single
    # `hyperi-ci push --publish --allow-feat` works without exporting
    # the env var manually.
    if allow_feat:
        os.environ["HYPERCI_ALLOW_FEAT"] = "1"
    if allow_breaking:
        os.environ["HYPERCI_ALLOW_BREAKING"] = "1"

    dir_path = Path(project_dir) if project_dir else None
    rc = do_push(
        publish=publish,
        no_ci=no_ci,
        bump=bump,
        dry_run=dry_run,
        force=force,
        project_dir=dir_path,
    )
    raise typer.Exit(rc)


@app.command()
def init(
    project_dir: Annotated[
        str | None,
        typer.Option("--project-dir", "-C", help="Project root directory"),
    ] = None,
    language: Annotated[
        str | None,
        typer.Option("--language", "-l", help="Override detected language"),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force", "-f", help="Overwrite existing files (init-specific semantic)"
        ),
    ] = False,
) -> None:
    """Initialise a project for hyperi-ci (generates config, Makefile, workflow).

    Note: `--force` here means "overwrite existing files" — different from
    `push --force` which means "skip pre-push checks". See module docstring
    for the project-wide convention on per-command `--force` semantics.
    """
    from hyperi_ci.init import init_project

    dir_path = Path(project_dir) if project_dir else Path.cwd()
    rc = init_project(dir_path, language=language, force=force)
    raise typer.Exit(rc)


@app.command()
def detect(
    project_dir: Annotated[
        str | None,
        typer.Option("--project-dir", "-C", help="Project root directory"),
    ] = None,
) -> None:
    """Detect project language."""
    dir_path = Path(project_dir) if project_dir else None
    language = detect_language(dir_path)
    if language:
        typer.echo(language)
    else:
        typer.echo("unknown", err=True)
        raise typer.Exit(1)


@app.command(name="stamp-version")
def stamp_version_cmd(
    version: Annotated[
        str,
        typer.Argument(help="Release version to stamp (with or without leading v)"),
    ],
    project_dir: Annotated[
        str | None,
        typer.Option("--project-dir", "-C", help="Project root directory"),
    ] = None,
) -> None:
    """Stamp the version into VERSION + the language manifest.

    Central, version-first step run by every language workflow before
    build. Writes the VERSION file (language-agnostic) and delegates the
    manifest stamp (Cargo.toml / pyproject.toml / package.json) to the
    detected language. Go is a no-op (version injected via ldflags).
    """
    from hyperi_ci.stamp import stamp_version

    dir_path = Path(project_dir) if project_dir else None
    raise typer.Exit(stamp_version(version, project_dir=dir_path))


@app.command()
def config(
    project_dir: Annotated[
        str | None,
        typer.Option("--project-dir", "-C", help="Project root directory"),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON instead of YAML"),
    ] = False,
) -> None:
    """Show merged configuration (YAML by default, --json for scripts)."""
    import yaml

    dir_path = Path(project_dir) if project_dir else None
    cfg = load_config(reload=True, project_dir=dir_path)

    if as_json:
        typer.echo(json.dumps(cfg._raw, indent=2, default=str))
    else:
        typer.echo(yaml.safe_dump(cfg._raw, sort_keys=False, default_flow_style=False))


@app.command()
def migrate(
    project_dir: Annotated[
        str | None,
        typer.Option("--project-dir", "-C", help="Project root directory"),
    ] = None,
    language: Annotated[
        str | None,
        typer.Option("--language", "-l", help="Override detected language"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Show what would be done"),
    ] = False,
) -> None:
    """Migrate a project from old ci/ submodule to hyperi-ci."""
    from hyperi_ci.migrate import migrate_project

    dir_path = Path(project_dir) if project_dir else Path.cwd()
    rc = migrate_project(dir_path, language=language, dry_run=dry_run)
    raise typer.Exit(rc)


@app.command()
def trigger(
    workflow: Annotated[
        str,
        typer.Option("--workflow", "-w", help="Workflow filename"),
    ] = "ci.yml",
    ref: Annotated[
        str | None,
        typer.Option("--ref", "-r", help="Branch or tag to run on"),
    ] = None,
    watch_run: Annotated[
        bool,
        typer.Option("--watch", help="Watch run to completion after triggering"),
    ] = False,
    timeout: Annotated[
        int,
        typer.Option("--timeout", "-t", help="Timeout in seconds"),
    ] = 1800,
    interval: Annotated[
        int,
        typer.Option("--interval", "-i", help="Poll interval in seconds"),
    ] = 30,
) -> None:
    """Trigger a GitHub Actions workflow run.

    Dispatches the workflow via `gh workflow run`. Use --watch to block
    until the run completes — equivalent to running `hyperi-ci trigger`
    then `hyperi-ci watch` as separate commands.
    """
    from hyperi_ci.trigger import trigger_workflow

    rc = trigger_workflow(
        workflow=workflow,
        ref=ref,
        watch=watch_run,
        timeout=timeout,
        interval=interval,
    )
    raise typer.Exit(rc)


@app.command()
def watch(
    run_id: Annotated[
        str | None,
        typer.Argument(help="Run ID (auto-detects latest if omitted)"),
    ] = None,
    timeout: Annotated[
        int,
        typer.Option(
            "--timeout",
            "-t",
            help=(
                "Timeout in seconds. Default 3600 (60 min) covers Tier 2 "
                "Rust builds. Pass 0 to disable timeout."
            ),
        ),
    ] = 3600,
    interval: Annotated[
        int,
        typer.Option("--interval", "-i", help="Initial poll interval in seconds"),
    ] = 30,
    repo: Annotated[
        str | None,
        typer.Option(
            "--repo",
            "-R",
            help=(
                "Target repo as owner/name (e.g. hyperi-io/dfe-loader). "
                "Defaults to the cwd's git remote — set this when watching "
                "a run in a different repo than your cwd."
            ),
        ),
    ] = None,
) -> None:
    """Watch a GitHub Actions run to completion."""
    from hyperi_ci.watch import watch_run

    rc = watch_run(run_id=run_id, timeout=timeout, interval=interval, repo=repo)
    raise typer.Exit(rc)


@app.command()
def logs(
    run_id: Annotated[
        str | None,
        typer.Argument(help="Run ID (auto-detects latest if omitted)"),
    ] = None,
    job: Annotated[
        str | None,
        typer.Option("--job", "-j", help="Filter by job name (substring)"),
    ] = None,
    step: Annotated[
        str | None,
        typer.Option("--step", "-s", help="Filter by step name (substring)"),
    ] = None,
    grep: Annotated[
        str | None,
        typer.Option("--grep", "-g", help="Filter lines by pattern"),
    ] = None,
    tail: Annotated[
        int | None,
        typer.Option("--tail", help="Show last N lines"),
    ] = None,
    failed: Annotated[
        bool,
        typer.Option("--failed", help="Show only failed job logs"),
    ] = False,
) -> None:
    """Fetch and filter GitHub Actions run logs."""
    from hyperi_ci.logs import fetch_logs

    rc = fetch_logs(
        run_id=run_id,
        job_filter=job,
        step_filter=step,
        grep_pattern=grep,
        tail_lines=tail,
        failed_only=failed,
    )
    raise typer.Exit(rc)


@app.command(name="install-native-deps")
def install_native_deps(
    language: Annotated[
        str,
        typer.Argument(help="Language (rust, typescript, golang, python)"),
    ],
    project_dir: Annotated[
        str | None,
        typer.Option("--project-dir", "-C", help="Project root directory"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", "-n", help="Show what would be installed without installing"
        ),
    ] = False,
    all_mode: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Install every entry unconditionally (bypass manifest matching). "
            "Use for runner image bake; stay default for CI-time conditional install.",
        ),
    ] = False,
) -> None:
    """Detect and install native system dependencies for a language."""
    from hyperi_ci.native_deps import install_native_deps as _install
    from hyperi_ci.native_deps import print_needed

    dir_path = Path(project_dir) if project_dir else None
    if dry_run:
        print_needed(language, project_dir=dir_path, all_mode=all_mode)
        return
    rc = _install(language, project_dir=dir_path, all_mode=all_mode)
    raise typer.Exit(rc)


@app.command(name="install-toolchains")
def install_toolchains(
    family: Annotated[
        str,
        typer.Argument(
            help="Toolchain family (llvm, gcc). Defaults to 'all' = every family.",
        ),
    ] = "all",
    project_dir: Annotated[
        str | None,
        typer.Option("--project-dir", "-C", help="Project root directory"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", "-n", help="Show what would be installed without installing"
        ),
    ] = False,
    all_mode: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Install every version unconditionally (bypass manifest matching). "
            "Used for runner image bake. Default is conditional install.",
        ),
    ] = False,
) -> None:
    """Install multi-version toolchain families (LLVM, GCC).

    By default fans out across every family in `config/toolchains/` and
    matches project manifests to decide what to install. Pass `--all` on
    a runner image bake to install every version of every family
    regardless of manifest.

    Examples:
        hyperi-ci install-toolchains --all        # bake everything
        hyperi-ci install-toolchains llvm --all   # bake only LLVM
        hyperi-ci install-toolchains              # CI-time: conditional
        hyperi-ci install-toolchains llvm         # CI-time: LLVM if triggered

    """
    from hyperi_ci.native_deps import _TOOLCHAINS_DIR, print_needed
    from hyperi_ci.native_deps import install_native_deps as _install

    dir_path = Path(project_dir) if project_dir else None

    # `all` fans out to every toolchain YAML in config/toolchains/
    if family == "all":
        families = sorted(f.stem for f in _TOOLCHAINS_DIR.glob("*.yaml"))
    else:
        families = [family]

    for fam in families:
        if dry_run:
            print_needed(
                fam, project_dir=dir_path, category="toolchains", all_mode=all_mode
            )
            continue
        rc = _install(
            fam, project_dir=dir_path, category="toolchains", all_mode=all_mode
        )
        if rc != 0:
            raise typer.Exit(rc)


@app.command(name="install-deps")
def install_deps_cmd(
    language: Annotated[
        str,
        typer.Argument(help="Language (e.g. typescript)"),
    ],
    project_dir: Annotated[
        str | None,
        typer.Option("--project-dir", "-C", help="Project root directory"),
    ] = None,
) -> None:
    """Install project dependencies for a language."""
    from hyperi_ci.install_deps import install_deps

    dir_path = Path(project_dir) if project_dir else None
    rc = install_deps(language, project_dir=dir_path)
    raise typer.Exit(rc)


@app.command(name="check-commit")
def check_commit_cmd(
    message_file: Annotated[
        str | None,
        typer.Argument(help="Path to commit message file (reads stdin if omitted)"),
    ] = None,
    list_types: Annotated[
        bool,
        typer.Option("--list", help="List all accepted commit types"),
    ] = False,
) -> None:
    """Validate a commit message against conventional commit rules.

    Used by .githooks/commit-msg hook. Reads from file or stdin.
    """
    from hyperi_ci.quality.commit_validation import (
        format_rejection,
        format_type_list,
        validate_message,
    )

    if list_types:
        typer.echo(format_type_list())
        raise typer.Exit(0)

    if message_file:
        msg = Path(message_file).read_text().strip()
    elif not sys.stdin.isatty():
        msg = sys.stdin.read().strip()
    else:
        typer.echo(
            "No commit message provided. Pass a file or pipe via stdin.", err=True
        )
        raise typer.Exit(1)

    result = validate_message(msg)
    if result.valid:
        raise typer.Exit(0)

    typer.echo(format_rejection(result, msg), err=True)
    raise typer.Exit(1)


def _publish_impl(
    tag: str | None,
    list_tags: bool,
    dry_run: bool,
    bump: str | None = None,
    version: str | None = None,
) -> None:
    """Shared implementation for the ``publish`` and ``release`` commands."""
    from hyperi_ci.common import explicit_version
    from hyperi_ci.publish import (
        dispatch_from_head,
        dispatch_publish,
        list_unpublished,
    )

    if list_tags:
        rc = list_unpublished()
        raise typer.Exit(rc)

    # --version is a from-head release at an exact version (issue #37 escape
    # hatch). It travels in the same `bump` channel the CI already threads, so
    # consumers need no new workflow input. It's mutually exclusive with both
    # a TAG (re-publish) and --bump (resolve-from-HEAD).
    if version is not None:
        if tag or bump:
            typer.echo(
                "--version is mutually exclusive with a TAG and --bump.",
                err=True,
            )
            raise typer.Exit(1)
        normalised = explicit_version(version)
        if normalised is None:
            typer.echo(
                f"Invalid --version '{version}' — expected an explicit X.Y.Z.",
                err=True,
            )
            raise typer.Exit(1)
        bump = normalised

    if tag and bump:
        typer.echo(
            "Pass either a TAG (re-publish an existing tag) or --bump "
            "(release the current HEAD) — not both.",
            err=True,
        )
        raise typer.Exit(1)

    if tag:
        # Re-publish an existing tag (idempotent retry of a partial publish).
        rc = dispatch_publish(tag, dry_run=dry_run)
        raise typer.Exit(rc)

    # No tag → release/retry the current HEAD. The CI resolves the version,
    # creates the tag, and publishes — no artificial commit, no local tag
    # push (issue #35). `bump` defaults to auto (semantic-release picks the
    # version from commits); --bump patch|minor forces a release; an explicit
    # X.Y.Z (from --version) tags HEAD at exactly that version.
    rc = dispatch_from_head(bump=bump or "auto", dry_run=dry_run)
    raise typer.Exit(rc)


@app.command()
def publish(
    tag: Annotated[
        str | None,
        typer.Argument(help="Existing tag to re-publish (e.g. v1.3.0 or 'latest')"),
    ] = None,
    bump: Annotated[
        str | None,
        typer.Option(
            "--bump",
            help="Release the current HEAD with a forced bump: patch | minor "
            "(no release-worthy commit needed).",
        ),
    ] = None,
    version: Annotated[
        str | None,
        typer.Option(
            "--version",
            help="Release the current HEAD at an exact X.Y.Z version. Tags HEAD "
            "directly — use to step past a taken/orphaned tag (issue #37).",
        ),
    ] = None,
    list_tags: Annotated[
        bool,
        typer.Option("--list", help="List unpublished version tags"),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Show what would be dispatched"),
    ] = False,
) -> None:
    """Release or retry a release — the CI creates the tag (issue #35).

    The primary release path is ``hyperi-ci push --publish`` (version-first
    single run, gated by the ``Publish: true`` trailer). This command is the
    "I need to release/retry that" escape hatch — no artificial ``fix:`` commit:

    - ``hyperi-ci publish`` — release the current ``main`` HEAD. Dispatches a
      from-head run; the CI resolves the version (semantic-release), tags HEAD,
      and publishes. Also finishes a release that died before the tag was cut.
    - ``hyperi-ci publish --bump patch|minor`` — force a release of HEAD even
      with no release-worthy commit since the last tag.
    - ``hyperi-ci publish --version X.Y.Z`` — release HEAD at an exact version.
      Tags HEAD directly, skipping a taken/orphaned tag the auto tagger would
      otherwise collide with (issue #37).
    - ``hyperi-ci publish <tag>`` — re-dispatch an existing tag (idempotent
      retry of a partial publish; fills in registries that were missed).

    The CLI only triggers the workflow; the runner does the tagging and
    publishing, so it works under branch protection and from the Actions UI too.
    """
    _publish_impl(
        tag=tag, list_tags=list_tags, dry_run=dry_run, bump=bump, version=version
    )


@app.command()
def release(
    tag: Annotated[
        str | None,
        typer.Argument(help="Tag to publish (e.g. v1.3.0) or 'latest'"),
    ] = None,
    list_tags: Annotated[
        bool,
        typer.Option("--list", help="List unpublished version tags"),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Show what would be dispatched"),
    ] = False,
) -> None:
    """Dispatch a publish run (deprecated alias of ``publish``; will be removed in v3.0)."""
    import warnings

    warnings.warn(
        "`hyperi-ci release` is deprecated; use `hyperi-ci publish`.",
        DeprecationWarning,
        stacklevel=2,
    )
    _publish_impl(tag=tag, list_tags=list_tags, dry_run=dry_run)


@app.command(name="tag-head", hidden=True)
def tag_head_cmd(
    bump: Annotated[
        str,
        typer.Option("--bump", help="patch | minor"),
    ],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Show what would be tagged"),
    ] = False,
) -> None:
    """CI-internal: create the next tag at HEAD for a forced bump (issue #35).

    Run by the from-head dispatch path in `_release-tail.yml` when
    `bump` is patch/minor. Not a routine command — operators use
    `hyperi-ci publish` instead.
    """
    from hyperi_ci.push import tag_head

    raise typer.Exit(tag_head(bump=bump, dry_run=dry_run))


@app.command()
def upgrade(
    target_version: Annotated[
        str | None,
        typer.Argument(help="Specific version to install (default: latest)"),
    ] = None,
    pre: Annotated[
        bool,
        typer.Option("--pre", help="Include pre-releases when resolving latest"),
    ] = False,
) -> None:
    """Upgrade hyperi-ci to the latest version (or a specific version)."""
    from hyperi_ci.upgrade import run_upgrade

    rc = run_upgrade(version=target_version, pre=pre)
    raise typer.Exit(rc)


@app.command(name="init-contract")
def init_contract_cmd(
    app_name: Annotated[
        str,
        typer.Option(
            "--app-name",
            help="Application name (lowercase, hyphenated; e.g. my-app)",
        ),
    ],
    output_dir: Annotated[
        str,
        typer.Option(
            "--output-dir",
            "-o",
            help="Where to write deployment-contract.json (default: ci/)",
        ),
    ] = "ci",
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            "-f",
            help="Overwrite an existing contract instead of erroring",
        ),
    ] = False,
) -> None:
    """Scaffold a starter ci/deployment-contract.json (Tier 3 onboarding).

    Writes a contract with sensible defaults derived from app_name.
    The file validates against the Pydantic DeploymentContract so
    the very first emit-artefacts run works without manual editing.

    Tier 3 only — Rust apps build their contract via
    the scalo crate's DeploymentContract source, Python apps via the scalo package's
    Application.deployment_contract(). Calling this in a Tier 1/2 repo
    would create a contract that drifts from the framework's source
    of truth.
    """
    from hyperi_ci.deployment.scaffold import init_contract

    rc = init_contract(Path(output_dir), app_name, force=force)
    raise typer.Exit(rc)


@app.command(name="emit-artefacts")
def emit_artefacts_cmd(
    output_dir: Annotated[
        str,
        typer.Argument(
            help="Output directory for generated artefacts (e.g. ci/, ci-tmp/)",
        ),
    ],
    contract: Annotated[
        str | None,
        typer.Option(
            "--from",
            help=(
                "Path to deployment-contract.json "
                "(default: ci/deployment-contract.json)"
            ),
        ),
    ] = None,
) -> None:
    """Generate deployment artefacts from a contract JSON (Tier 3 templater).

    Reads ``ci/deployment-contract.json`` and writes the generated
    Dockerfile, Dockerfile.runtime, container-manifest.json,
    argocd-application.yaml, Helm chart, and the schema reference into
    ``output_dir``.

    Used by:
      - Tier 3 apps in their CI (Generate stage)
      - All tiers' Quality stage drift check (output to /tmp/drift/)
      - Local dev to regenerate ci/ after editing the contract

    Exits non-zero if the contract is missing, invalid, or declares a
    schema_version newer than this hyperi-ci can consume.
    """
    from hyperi_ci.deployment.cli import emit_artefacts

    contract_path = Path(contract) if contract else None
    rc = emit_artefacts(Path(output_dir), contract_path)
    raise typer.Exit(rc)


@app.command(name="overlay-render")
def overlay_render_cmd(
    kind: Annotated[
        str | None,
        typer.Option(
            "--kind",
            "-k",
            help=(
                "Artefact to render: dockerfile | helm | argocd. "
                "Default: emit all three into the output directory "
                "(mirrors the deployment contract's bulk behaviour)."
            ),
        ),
    ] = None,
    output: Annotated[
        str | None,
        typer.Option(
            "--output",
            "-o",
            help=(
                "Output path. For single-kind renders, stdout if omitted "
                "(Helm requires --output since it's a directory). For "
                "all-three renders (default), defaults to ./ci-overlay/."
            ),
        ),
    ] = None,
    project_dir: Annotated[
        str,
        typer.Option(
            "--project-dir",
            "-C",
            help="Project root directory (default: cwd)",
        ),
    ] = ".",
    binary: Annotated[
        str | None,
        typer.Option(
            "--binary",
            help=(
                "Override the consumer binary used for emit-* subcommand "
                "calls. Default: <project_dir>/<project_name> via PATH."
            ),
        ),
    ] = None,
) -> None:
    """Render deployment artefacts with `publish.<kind>.overlays` applied.

    Subprocesses into the consumer's emit-{dockerfile,chart,argocd}
    subcommand to fetch the contract-generated base, then splices any
    overlays declared in `.hyperi-ci.yaml` and writes the final
    artefact(s).

    Use this for local container builds when the project declares
    container overlays (since bare `docker build .` against the repo's
    checked-in Dockerfile won't have the overlay content):

        hyperi-ci overlay-render --kind dockerfile -o /tmp/Dockerfile.final
        docker buildx build -f /tmp/Dockerfile.final .

    Or render everything (Dockerfile + Helm chart + ArgoCD Application)
    into one directory for inspection:

        hyperi-ci overlay-render -o /tmp/ci-overlay
    """
    from hyperi_ci.deployment.overlay.cli import render

    rc = render(
        kind=kind,
        project_dir=Path(project_dir).resolve(),
        output=Path(output) if output else None,
        binary=binary,
    )
    raise typer.Exit(rc)


@app.command(name="stitch")
def stitch_cmd(
    topology_dir: Annotated[
        str,
        typer.Argument(
            help="Path to the topology directory (must contain topology.yaml)",
        ),
    ],
    output_dir: Annotated[
        str | None,
        typer.Option(
            "--output-dir",
            "-o",
            help="Where to write the stitched umbrella chart (default: ./stitched/<topology-name>/)",
        ),
    ] = None,
    oci_base: Annotated[
        str,
        typer.Option(
            "--oci-base",
            help="OCI registry URL for per-app charts",
        ),
    ] = "oci://ghcr.io/hyperi-io/helm-charts",
    skip_helm_dep_update: Annotated[
        bool,
        typer.Option(
            "--skip-helm-dep-update",
            help="Skip `helm dep update` (useful for CI dry-runs)",
        ),
    ] = False,
    skip_helm_lint: Annotated[
        bool,
        typer.Option(
            "--skip-helm-lint",
            help="Skip `helm lint`",
        ),
    ] = False,
) -> None:
    """Stitch a DeploymentTopology directory into an umbrella Helm chart.

    Reads ``<topology-dir>/topology.yaml``, resolves each app's version
    range against the OCI registry, then generates a complete Chart.yaml +
    values.yaml ready for ``helm package``.

    Exit codes:
      0  stitched successfully
      2  topology not found / invalid
      3  OCI version resolution failed
      4  helm tooling failure
    """
    from scalo.deployment.topology import load_topology
    from scalo.deployment.topology.errors import (
        TopologyError,
        TopologyValidationError,
        VersionResolutionError,
    )

    from hyperi_ci.common import error as _error
    from hyperi_ci.common import info as _info
    from hyperi_ci.common import success as _success
    from hyperi_ci.deployment.topology.resolve import resolve_versions
    from hyperi_ci.deployment.topology.stitch import stitch_topology

    topo_path = Path(topology_dir)

    # Load and validate the topology
    _info(f"Loading topology from {topo_path}")
    try:
        topology = load_topology(topo_path)
    except TopologyValidationError as exc:
        _error(f"Invalid topology: {exc}")
        raise typer.Exit(2) from exc
    except TopologyError as exc:
        _error(f"Topology error: {exc}")
        raise typer.Exit(2) from exc

    topology_name = topology.metadata.get("name", "topology")

    # Compute output directory
    out_path = Path(output_dir) if output_dir else Path("stitched") / topology_name

    _info(f"Topology: {topology_name!r} → {out_path}")

    # Build chart → version-range map for hyperi-io apps
    hyperi_charts: dict[str, str] = {
        app.name: app.version for app in topology.spec.apps
    }

    # Resolve versions
    resolved: dict[str, str] = {}

    if hyperi_charts:
        _info(f"Resolving {len(hyperi_charts)} app chart(s) from {oci_base}")
        try:
            resolved.update(resolve_versions(registry=oci_base, charts=hyperi_charts))
        except VersionResolutionError as exc:
            _error(f"Version resolution failed: {exc}")
            raise typer.Exit(3) from exc

    # Third-party charts — group by repository, resolve each group separately
    by_repo: dict[str, dict[str, str]] = {}
    for tp in topology.spec.thirdParty:
        by_repo.setdefault(tp.repository, {})[tp.name] = tp.version

    for repo, charts in by_repo.items():
        _info(f"Resolving {len(charts)} third-party chart(s) from {repo}")
        try:
            resolved.update(resolve_versions(registry=repo, charts=charts))
        except VersionResolutionError as exc:
            _error(f"Version resolution failed: {exc}")
            raise typer.Exit(3) from exc

    # Stitch the umbrella chart
    _info(f"Stitching umbrella chart into {out_path}")
    try:
        result = stitch_topology(
            topology,
            topology_dir=topo_path if topo_path.is_dir() else topo_path.parent,
            output_dir=out_path,
            resolved=resolved,
            oci_base=oci_base,
            run_helm_dep_update=not skip_helm_dep_update,
            run_helm_lint=not skip_helm_lint,
        )
    except TopologyError as exc:
        _error(f"Stitch failed: {exc}")
        raise typer.Exit(4) from exc

    _success(f"Stitched {topology_name!r} → {result.chart_dir}")
    for chart_name, version in sorted(result.resolved_versions.items()):
        typer.echo(f"  {chart_name}: {version}")
    raise typer.Exit(0)


@app.command(name="init-gitops")
def init_gitops_cmd(
    target: str = typer.Argument(
        ...,
        help="Destination directory for the new gitops repo.",
    ),
    org: str = typer.Option(
        "hyperi-io",
        "--org",
        help="GitHub org name substituted into CODEOWNERS.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Write into a non-empty directory (existing files are preserved).",
    ),
) -> None:
    """Scaffold a new hyperi-io/gitops monorepo from bundled templates.

    Creates the standard directory structure, GitHub Actions workflows,
    ArgoCD manifests, Terraform skeleton, and MkDocs documentation site
    in TARGET.

    Example:
        hyperi-ci init-gitops ./my-gitops-repo --org my-github-org

    """
    from pathlib import Path as _Path

    from hyperi_ci.common import error as _error
    from hyperi_ci.init_gitops import GitopsInitError, init_gitops

    try:
        rc = init_gitops(_Path(target), org=org, force=force)
    except GitopsInitError as exc:
        _error(str(exc))
        raise typer.Exit(code=2) from exc
    raise typer.Exit(code=rc)


@app.command(name="init-topology")
def init_topology_cmd(
    name: str = typer.Argument(
        ...,
        help="Topology name (lowercase RFC-1123-ish, e.g. 'default').",
    ),
    gitops_root: str = typer.Option(
        ".",
        "--gitops-root",
        help="Path to the gitops repo root (default: current directory).",
    ),
    apps: list[str] = typer.Option(
        [],
        "--app",
        help="HyperI application chart name (repeat for multiple apps).",
    ),
) -> None:
    """Scaffold a new topology directory inside an existing gitops repo.

    Creates topologies/<NAME>/ with topology.yaml, values.yaml, glue/,
    and README.md.

    Example:
        hyperi-ci init-topology production --app dfe-loader --app dfe-receiver

    """
    from pathlib import Path as _Path

    from hyperi_ci.common import error as _error
    from hyperi_ci.common import warn as _warn
    from hyperi_ci.init_gitops import GitopsInitError, init_topology

    if not apps:
        _warn("no --app specified; topology will have an empty apps list")

    try:
        rc = init_topology(gitops_root=_Path(gitops_root), name=name, apps=apps)
    except GitopsInitError as exc:
        _error(str(exc))
        raise typer.Exit(code=2) from exc
    raise typer.Exit(code=rc)


def main() -> int:
    """CLI entry point."""
    # Force UTF-8 with replacement on stdout/stderr so log lines containing
    # arbitrary bytes (gh CLI output, GH Actions log files, container build
    # output) never crash the CLI with UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    app()
    return 0


if __name__ == "__main__":
    sys.exit(main())
