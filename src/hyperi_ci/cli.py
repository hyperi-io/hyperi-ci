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
    hyperi-ci init              Initialise project (config, Makefile, workflow)
    hyperi-ci detect            Detect project language
    hyperi-ci config            Show merged configuration
    hyperi-ci trigger           Trigger a GitHub Actions workflow run
    hyperi-ci watch [RUN_ID]    Watch a GitHub Actions run to completion
    hyperi-ci logs [RUN_ID]     Fetch and filter GitHub Actions run logs
    hyperi-ci release-merge     Merge main into release (auto conflict resolution)
    hyperi-ci --version         Show version
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
        typer.Option("--force", "-f", help="Overwrite existing files"),
    ] = False,
) -> None:
    """Initialise a project for hyperi-ci (generates config, Makefile, workflow)."""
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
) -> None:
    """Show merged configuration."""
    dir_path = Path(project_dir) if project_dir else None
    cfg = load_config(reload=True, project_dir=dir_path)
    typer.echo(json.dumps(cfg._raw, indent=2, default=str))


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
    """Trigger a GitHub Actions workflow run."""
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
        typer.Option("--timeout", "-t", help="Timeout in seconds"),
    ] = 1800,
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
        typer.Option("--tail", "-n", help="Show last N lines"),
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


@app.command(name="init-release")
def init_release_cmd(
    project_dir: Annotated[
        str | None,
        typer.Option("--project-dir", "-C", help="Project root directory"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Show what would be done"),
    ] = False,
    check_only: Annotated[
        bool,
        typer.Option("--check", help="Check release setup status only"),
    ] = False,
) -> None:
    """Set up release branch and configure two-channel semantic-release."""
    from hyperi_ci.init_release import init_release

    dir_path = Path(project_dir) if project_dir else Path.cwd()
    rc = init_release(dir_path, dry_run=dry_run, check_only=check_only)
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
) -> None:
    """Detect and install native system dependencies for a language."""
    from hyperi_ci.native_deps import install_native_deps as _install
    from hyperi_ci.native_deps import print_needed

    dir_path = Path(project_dir) if project_dir else None
    if dry_run:
        print_needed(language, project_dir=dir_path)
        return
    rc = _install(language, project_dir=dir_path)
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
    """Install project dependencies for a language (e.g. npm/yarn/pnpm for TypeScript)."""
    from hyperi_ci.install_deps import install_deps

    dir_path = Path(project_dir) if project_dir else None
    rc = install_deps(language, project_dir=dir_path)
    raise typer.Exit(rc)


@app.command(name="release-merge")
def release_merge_cmd(
    base_branch: Annotated[
        str,
        typer.Option("--base", "-b", help="Target branch (default: release)"),
    ] = "release",
    head_branch: Annotated[
        str,
        typer.Option("--head", help="Source branch (default: main)"),
    ] = "main",
) -> None:
    """Merge main into release with auto version conflict resolution.

    Clones to a temp directory, merges, resolves VERSION/Cargo.toml/CHANGELOG.md
    conflicts (keeps release versions), pushes, and creates a PR.

    Never touches your working tree. Requires gh CLI (prints manual commands if unavailable).
    """
    from hyperi_ci.release_merge import release_merge

    rc = release_merge(base_branch=base_branch, head_branch=head_branch)
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
    app()
    return 0


if __name__ == "__main__":
    sys.exit(main())
