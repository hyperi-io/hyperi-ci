# Project:   HyperI CI
# File:      src/hyperi_ci/cli.py
# Purpose:   CLI entry point for hyperi-ci tool (Typer via hyperi-pylib)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""CLI entry point for HyperI CI.

Usage:
    hyperi-ci run <stage>       Run a CI stage (setup, quality, test, build, publish)
    hyperi-ci check             Pre-push checks (quality + test; --full adds build)
    hyperi-ci push              Push with pre-checks (replaces bare git push)
    hyperi-ci init              Initialise project (config, Makefile, workflow)
    hyperi-ci detect            Detect project language
    hyperi-ci config            Show merged configuration
    hyperi-ci trigger           Trigger a GitHub Actions workflow run
    hyperi-ci watch [RUN_ID]    Watch a GitHub Actions run to completion
    hyperi-ci logs [RUN_ID]     Fetch and filter GitHub Actions run logs
    hyperi-ci release <tag>     Trigger publish for a version tag
    hyperi-ci check-commit      Validate commit message format
    hyperi-ci --version         Show version

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
    release: Annotated[
        bool,
        typer.Option(
            "--release", help="After CI passes, auto-dispatch publish for new version"
        ),
    ] = False,
    no_ci: Annotated[
        bool,
        typer.Option("--no-ci", help="Amend last commit with [skip ci] and push"),
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
    """Push with pre-checks. Replaces bare 'git push'.

    Default: runs quality + test checks, rebases, then pushes.
    Use --release to auto-publish after CI passes.
    Use --no-ci to skip CI on this push.
    """
    from hyperi_ci.push import push as do_push

    dir_path = Path(project_dir) if project_dir else None
    rc = do_push(
        release=release,
        no_ci=no_ci,
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
) -> None:
    """Watch a GitHub Actions run to completion."""
    from hyperi_ci.watch import watch_run

    rc = watch_run(run_id=run_id, timeout=timeout, interval=interval)
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
    """Trigger a publish workflow for a version tag.

    Lists available tags or dispatches a publish for a specific tag.
    Replaces the old release-merge flow — no release branch needed.
    """
    from hyperi_ci.release import dispatch_publish, list_unpublished

    if list_tags:
        rc = list_unpublished()
        raise typer.Exit(rc)

    if not tag:
        typer.echo(
            "Specify a tag to publish, or use --list to see available tags.", err=True
        )
        raise typer.Exit(1)

    rc = dispatch_publish(tag, dry_run=dry_run)
    raise typer.Exit(rc)


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
