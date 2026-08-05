# Project:   HyperI CI
# File:      src/hyperi_ci/cli.py
# Purpose:   CLI entry point for hyperi-ci tool (Typer via scalo)
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""CLI entry point for HyperI CI.

Usage:
    hyperi-ci run <stage>               Run a CI stage (setup, quality, test, build, publish)
    hyperi-ci check                     Pre-push checks (quality + test; --full adds build, --strict fails on warnings)
    hyperi-ci push                      Push with pre-checks (replaces bare git push)
    hyperi-ci init                      Initialise project (config, Makefile, workflow)
    hyperi-ci detect                    Detect project language
    hyperi-ci config                    Show merged configuration
    hyperi-ci trigger                   Trigger a GitHub Actions workflow run
    hyperi-ci watch [RUN_ID]            Watch a GitHub Actions run to completion
    hyperi-ci logs [RUN_ID]             Fetch and filter GitHub Actions run logs
    hyperi-ci release <tag>             Trigger publish for a version tag
    hyperi-ci update                    Update to the channel's release
    hyperi-ci autoupdate                Show/set self-update channel + freeze
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
import os
import sys
from importlib.metadata import distribution
from pathlib import Path
from typing import Annotated

import typer

from hyperi_ci import __version__
from hyperi_ci.config import load_config
from hyperi_ci.detect import detect_language
from hyperi_ci.dispatch import VALID_STAGES, run_stage
from hyperi_ci.version_source import build_version

app = typer.Typer(
    name="hyperi-ci",
    help="HyperI CI — polyglot CI/CD tool",
    no_args_is_help=True,
)


def _source_checkout() -> str | None:
    """Return the checkout path when this is an editable install, else None.

    PEP 610 records the origin of a non-index install in ``direct_url.json``,
    with ``dir_info.editable`` set for an editable one. Reported because a
    checkout's ``hyperi-ci`` shim precedes the installed tool on PATH inside the
    project, and a version with no provenance gets read as the released one.

    Returns:
        Filesystem path of the checkout, or None for an ordinary install.

    """
    try:
        raw = distribution("hyperi-ci").read_text("direct_url.json")
    except Exception:
        return None
    if not raw:
        return None
    try:
        origin = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(origin, dict) or not origin.get("dir_info", {}).get("editable"):
        return None
    url = origin.get("url")
    if not isinstance(url, str):
        return None
    return url.removeprefix("file://") or None


def _checkout_version(checkout: str) -> str:
    """Return the version a checkout would build as, not its frozen metadata.

    An editable install bakes the version into ``.dist-info`` when it is synced
    and never revisits it, so it keeps reporting whatever ``VERSION`` said then
    — a number that drifts further from the tree on every release, and belongs
    to no release at all. Re-resolving through the same function the build
    back-end uses keeps the report honest without a re-sync.

    ``HYPERCI_VERSION`` is excluded: it is process-wide rather than scoped to a
    tree, so a release run in another project would have this answer with that
    project's version under this checkout's path.

    Args:
        checkout: Filesystem path of the editable checkout.

    Returns:
        A bare ``X.Y.Z``, falling back to the frozen metadata if the checkout
        can no longer be read — ``--version`` must not be the thing that fails.

    """
    try:
        return build_version(Path(checkout), allow_env=False)
    except Exception as exc:  # noqa: BLE001 - --version must not be the failure
        # Warn rather than fall through quietly: the fallback is the frozen
        # number this function exists to replace, so a silent one reads as the
        # bug it fixes.
        from hyperi_ci.common import warn

        warn(
            f"cannot resolve the checkout's version ({exc}) — showing the installed one"
        )
        return __version__


def _version_callback(value: bool) -> None:
    if value:
        checkout = _source_checkout()
        if checkout:
            typer.echo(
                f"hyperi-ci {_checkout_version(checkout)} (editable checkout: {checkout})"
            )
        else:
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
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help=(
                "Fail on warn-tier quality findings (ty, semgrep, "
                "docstrings, ...) too - a zero-warnings pre-push gate that "
                "surfaces everything CI would show, before the push."
            ),
        ),
    ] = False,
) -> None:
    """Run local pre-push checks (quality + test by default).

    With ``--strict``, warn-tier quality findings (which CI tolerates but
    still prints) are treated as failures, so nothing is carried into a
    push unseen. Fix each, or flag it to ignore if it genuinely should be.
    A tool that is not installed locally (and has no uv fallback) is still
    warn-skipped even under ``--strict`` - strict enforces what runs, not
    what your machine has; CI, where the tools are present, is the backstop.
    """
    dir_path = Path(project_dir) if project_dir else None

    if strict:
        os.environ["HYPERCI_QUALITY_STRICT"] = "1"

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


@app.command(name="lint-manifests")
def lint_manifests_cmd(
    directory: Annotated[
        str,
        typer.Argument(help="Directory to lint (Helm charts / k8s manifests / IaC)"),
    ] = ".",
    sarif: Annotated[
        str | None,
        typer.Option(
            "--sarif",
            help=(
                "Write combined SARIF here (opt-in). The workflow uploads it to "
                "code scanning only where GitHub Code Security is enabled."
            ),
        ),
    ] = None,
) -> None:
    """Lint Kubernetes manifests, Helm charts and IaC in a gitops / infra repo.

    Runs kubeconform (schema-validation GATE), kube-linter (best-practice
    ADVISORY) and Checkov (IaC security ADVISORY). Only kubeconform gates: a
    schema-invalid manifest exits non-zero; the advisories never fail the build.

    Built for GitHub-Actions-native gitops repos (no ``.hyperi-ci.yaml``, no
    language pipeline) - call it from the existing workflow instead of adopting
    the whole hyperi-ci pipeline.
    """
    from hyperi_ci.config import load_config
    from hyperi_ci.quality import lint_manifests

    root = Path(directory)
    config = load_config(project_dir=root)
    rc = lint_manifests.run(root, config, sarif_path=sarif)
    raise typer.Exit(rc)


@app.command()
def deps(
    action: Annotated[
        str,
        typer.Argument(
            help="scan (default, everything) | drift | gaps | show <surface>",
        ),
    ] = "scan",
    surface: Annotated[
        str | None,
        typer.Argument(help="Surface id, for `show`"),
    ] = None,
    project_dir: Annotated[
        str | None,
        typer.Option("--root", "-C", help="Repository root (default: cwd)"),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Machine-readable output"),
    ] = False,
    full: Annotated[
        bool,
        typer.Option("--full", help="Lift the display cap on detail lists"),
    ] = False,
    kind: Annotated[
        str | None,
        typer.Option(
            "--kind",
            help="Limit to one surface kind (python, rust, node, container, ci, ...)",
        ),
    ] = None,
) -> None:
    """Enumerate dependency surfaces, audit floors against the lock, name gaps.

    The PREVENTATIVE half of the dependency chain: it runs locally, BEFORE a
    change reaches CI or the forge, and reports what you are about to leave
    stale. Renovate is the remediation half and runs after the fact. See
    docs/dependencies/deps-pinning.md.

    Bare ``deps`` (and ``deps scan``) prints the whole picture in one call --
    surfaces and their three states, every extracted pin, every dependency
    group with its declared constraint, floor-vs-lock drift, and what Renovate
    will never see. ``deps show <surface>`` dumps one surface uncapped.

    Multi-language by construction: every manifest in the tree is parsed in the
    same pass and each ecosystem reported separately. Language toolchains
    (cargo, uv, npm) are used to enrich the result when installed and skipped
    silently when not.

    Exit codes: ``drift`` exits 1 when it finds drift, so it can gate a script.
    Everything else is a report and exits 0.
    """
    import json as _json

    from hyperi_ci import deps as _deps
    from hyperi_ci.deps import render

    root = Path(project_dir) if project_dir else Path.cwd()

    if action == "scan":
        payload = _deps.report(root, kind=kind or "")
        typer.echo(
            _json.dumps(payload, indent=2) if as_json else render.report(payload, full)
        )
        raise typer.Exit(0)
    if action == "drift":
        result = _deps.drift(root)
        typer.echo(
            _json.dumps(result, indent=2)
            if as_json
            else render.drift_only(result, full)
        )
        raise typer.Exit(1 if result["drift"] else 0)
    if action == "gaps":
        result = _deps.gaps(root, _deps.scan(root))
        typer.echo(
            _json.dumps(result, indent=2) if as_json else render.gaps_only(result)
        )
        raise typer.Exit(0)
    if action == "show":
        if not surface:
            typer.echo("deps show: needs a surface id", err=True)
            raise typer.Exit(2)
        detail = _deps.show(root, surface)
        typer.echo(_json.dumps(detail, indent=2) if as_json else render.show(detail))
        raise typer.Exit(1 if "error" in detail else 0)

    typer.echo(f"deps: unknown action {action!r}", err=True)
    raise typer.Exit(2)


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
    seed_tag: Annotated[
        bool,
        typer.Option(
            "--seed-tag/--no-seed-tag",
            help="Create the repo's first v* tag when it has none",
        ),
    ] = True,
) -> None:
    """Initialise a project for hyperi-ci (generates config, Makefile, workflow).

    Also seeds the repo's first `v*` git tag when it has none, from the
    version the project declares in its own manifest (`--no-seed-tag` to
    skip). The version pipeline reads tags, so a tag-less repo has nothing
    to release from.

    Note: `--force` here means "overwrite existing files" — different from
    `push --force` which means "skip pre-push checks". See module docstring
    for the project-wide convention on per-command `--force` semantics.
    """
    from hyperi_ci.init import init_project

    dir_path = Path(project_dir) if project_dir else Path.cwd()
    rc = init_project(dir_path, language=language, force=force, seed_tag=seed_tag)
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
def describe(
    project_dir: Annotated[
        str | None,
        typer.Option("--project-dir", "-C", help="Project root directory"),
    ] = None,
    check: Annotated[
        bool,
        typer.Option("--check", help="Report drift against the GitHub repo blurb"),
    ] = False,
    show_source: Annotated[
        bool,
        typer.Option("--source", help="Also print where the description came from"),
    ] = False,
) -> None:
    """Print the project description every registry duplicates.

    Resolved from `.hyperi-ci.yaml` `description`, else the manifest that owns
    the published artefact (`[workspace.package]` for a Cargo workspace), else
    the GitHub repo blurb. This is what lands in
    `org.opencontainers.image.description` and on GHCR's package page.

    `--check` compares the resolved value against the GitHub repo description
    and reports drift. It never writes: the repo blurb is a repository
    setting, so changing it stays a human's call.
    """
    from hyperi_ci.common import error, info, success, warn
    from hyperi_ci.description_source import github_description, resolve_description

    dir_path = Path(project_dir) if project_dir else None
    cfg = load_config(reload=True, project_dir=dir_path)

    resolved = resolve_description(cfg, root=dir_path, allow_github=not check)
    if not resolved:
        error(
            "No description found. Add one to the manifest "
            "(Cargo.toml [workspace.package] for a workspace), or set "
            "`description:` in .hyperi-ci.yaml."
        )
        raise typer.Exit(1)

    text, source = resolved
    typer.echo(f"{text}\t{source}" if show_source else text)

    if not check:
        raise typer.Exit(0)

    blurb = github_description(cwd=dir_path)
    if blurb is None:
        warn("GitHub repo description is unset or could not be read")
    elif blurb != text:
        warn(f"GitHub repo description differs from {source}:")
        warn(f"  {source}: {text}")
        warn(f"  GitHub:  {blurb}")
        info(f'Align it with: gh repo edit --description "{text}"')
    else:
        success("GitHub repo description matches")
    raise typer.Exit(0)


@app.command("audit-callers")
def audit_callers(
    org: Annotated[
        str | None,
        typer.Option("--org", help="Sweep every repo in this org, reading main"),
    ] = None,
    repo: Annotated[
        str | None,
        typer.Option("--repo", help="Audit one repo (owner/name), reading main"),
    ] = None,
    project_dir: Annotated[
        str | None,
        typer.Option("--project-dir", "-C", help="Project root directory"),
    ] = None,
) -> None:
    """Report consumers whose ci.yml has fallen behind the dispatch contract.

    `workflow_dispatch.inputs` only work when declared in the workflow that
    receives the event, so a reusable workflow cannot add them to its caller.
    Every consumer declares and forwards them itself, and drifts on its own
    (issue #88).

    Reports three faults: an input not declared at all (the HTTP 422), one
    declared but not passed in `with:` (accepted then silently ignored), and
    one declared `required: true` (which breaks every dispatch that does not
    send it).

    Never writes. The caller file belongs to the consumer repo.
    """
    from hyperi_ci.caller_audit import (
        audit_local,
        audit_repo,
        org_repos,
    )
    from hyperi_ci.common import error, info, success, warn

    if org and repo:
        error("Use --org or --repo, not both")
        raise typer.Exit(2)

    if org:
        targets = org_repos(org)
        if not targets:
            error(f"No repos readable in {org}")
            raise typer.Exit(1)
        reports = [audit_repo(name) for name in targets]
        # A repo with no ci.yml, or one that calls nothing of ours, is not a
        # consumer -- reporting it would bury the real findings.
        reports = [r for r in reports if r.calls is not None]
    elif repo:
        reports = [audit_repo(repo)]
    else:
        reports = [audit_local(Path(project_dir) if project_dir else None)]

    drifted = [r for r in reports if not r.ok]
    for report in reports:
        if report.ok:
            continue
        if report.error:
            warn(f"{report.repo}: {report.error}")
            continue
        warn(f"{report.repo} ({report.calls}):")
        for finding in report.findings:
            warn(f"  {finding.describe()}")

    info(f"Audited {len(reports)} caller(s)")
    if not drifted:
        success("Every caller honours the dispatch contract")
        raise typer.Exit(0)

    error(f"{len(drifted)} caller(s) drifted — a release from HEAD will fail")
    info("Fix in the consumer repo's .github/workflows/ci.yml, or re-run")
    info("`hyperi-ci init` there to regenerate it.")
    raise typer.Exit(1)


@app.command("audit-gates")
def audit_gates(
    org: Annotated[
        str | None,
        typer.Option("--org", help="Sweep every repo in this org"),
    ] = None,
    repo: Annotated[
        str | None,
        typer.Option("--repo", help="Audit one repo (owner/name)"),
    ] = None,
    max_age_days: Annotated[
        float,
        typer.Option("--max-age-days", help="How old an answer may be"),
    ] = 7.0,
    workflow: Annotated[
        str,
        typer.Option("--workflow", help="Workflow file holding the gate"),
    ] = "ci.yml",
    limit: Annotated[
        int,
        typer.Option("--limit", help="How many runs back to look"),
    ] = 20,
    include_prerelease: Annotated[
        bool,
        typer.Option(
            "--include-prerelease",
            help="Audit pre-GA repos too (skipped by default)",
        ),
    ] = False,
    skip: Annotated[
        list[str] | None,
        typer.Option("--skip", help="Repo to leave out (repeatable)"),
    ] = None,
) -> None:
    """Report repos whose quality gate has not actually EXECUTED.

    A run whose gate was skipped still concludes `success`, so the repo reports
    green while nothing was verified. The run-level conclusion is the lie, so
    this reads job level and asks when each gate last produced a verdict
    (issue #96).

    A FAILING gate is deliberately not reported — GitHub already shows a red
    repo as red, and pre-GA repos are expected to be red. Only the invisible
    fault is reported: a gate that never ran.

    Repos declaring a pre-GA `publish.channel` are skipped, since a dormant
    gate is expected there; `--include-prerelease` audits them anyway. What was
    skipped is always named, never dropped silently.

    Never writes.
    """
    from hyperi_ci.common import error, info, success, warn
    from hyperi_ci.gate_audit import audit_repo, is_prerelease, org_repos

    if org and repo:
        error("Use --org or --repo, not both")
        raise typer.Exit(2)
    if not org and not repo:
        error("Give --org or --repo — there is no local equivalent to audit")
        raise typer.Exit(2)

    targets = org_repos(org) if org else [repo or ""]
    if not targets:
        error(f"No repos readable in {org}")
        raise typer.Exit(1)

    excluded = {name.strip() for name in (skip or []) if name.strip()}
    if excluded:
        targets = [
            t for t in targets if t not in excluded and t.split("/")[-1] not in excluded
        ]
        info(
            f"Skipping {len(excluded)} repo(s) by request: {', '.join(sorted(excluded))}"
        )

    if org and not include_prerelease:
        prerelease = [t for t in targets if is_prerelease(t)]
        if prerelease:
            targets = [t for t in targets if t not in set(prerelease)]
            info(
                f"Skipping {len(prerelease)} pre-GA repo(s) — a dormant gate is "
                f"expected there: {', '.join(sorted(prerelease))}"
            )

    reports = [
        audit_repo(name, max_age_days=max_age_days, workflow=workflow, limit=limit)
        for name in targets
    ]
    if org:
        # A repo that never runs the workflow is not a consumer; reporting it
        # would bury the real findings.
        reports = [r for r in reports if r.error is None]
    if not reports:
        error(f"No repo in {org} runs {workflow}")
        raise typer.Exit(1)

    drifted = [r for r in reports if not r.ok]
    for report in reports:
        if report.ok:
            continue
        if report.error:
            warn(f"{report.repo}: {report.error}")
            continue
        warn(f"{report.repo}:")
        for finding in report.findings:
            warn(f"  {finding.describe()}")

    info(f"Audited {len(reports)} repo(s)")
    if not drifted:
        success(f"Every gate has answered within {max_age_days:.0f} days")
        raise typer.Exit(0)

    never = [r for r in drifted if any(f.kind == "never" for f in r.findings)]
    if never:
        error(f"{len(never)} repo(s) report green having never run their gate")
    if len(drifted) > len(never):
        error(
            f"{len(drifted) - len(never)} repo(s) have not run their gate in "
            f"{max_age_days:.0f} days"
        )
    info("Land a change through a PR to force the gate, or schedule a full")
    info("run so the answer is never older than the window.")
    raise typer.Exit(1)


@app.command(name="release-notify")
def release_notify_cmd(
    version: Annotated[
        str,
        typer.Argument(help="Version released (with or without leading v)"),
    ],
    outcome: Annotated[
        str,
        typer.Option("--outcome", help="success or failure"),
    ] = "success",
    run_url: Annotated[
        str,
        typer.Option("--run-url", help="Link to the run, for a failure issue"),
    ] = "",
    project_dir: Annotated[
        str | None,
        typer.Option("--project-dir", "-C", help="Project root directory"),
    ] = None,
) -> None:
    """Announce a release, or record that one failed.

    `--outcome success` comments on every issue and PR carried by the release;
    `--outcome failure` opens a tracker issue so a release that dies overnight
    is waiting in the morning. Both are idempotent, and both always exit 0 — a
    notification must never be the thing that fails a release.

    Slack is off unless `notify.slack.webhook_env` names an env var holding a
    webhook URL.
    """
    from hyperi_ci.release_notify import notify_failure, notify_slack, notify_success

    dir_path = Path(project_dir) if project_dir else None
    cfg = load_config(reload=True, project_dir=dir_path)

    if outcome == "failure":
        rc = notify_failure(version=version, run_url=run_url)
        notify_slack(cfg, text=f"Release of v{version.removeprefix('v')} FAILED")
    else:
        rc = notify_success(version=version, cwd=str(dir_path) if dir_path else None)
        notify_slack(cfg, text=f"Released v{version.removeprefix('v')}")
    raise typer.Exit(rc)


@app.command()
def preflight(
    project_dir: Annotated[
        str | None,
        typer.Option("--project-dir", "-C", help="Project root directory"),
    ] = None,
) -> None:
    """Verify publish credentials before anything is built.

    semantic-release's `verifyConditions` equivalent. Checks only the
    destinations this project actually publishes to, and only blocks on the
    ones whose handler hard-fails without a token — a missing
    CARGO_REGISTRY_TOKEN otherwise surfaces after a 40-minute Rust build.
    Outside CI it is a no-op.
    """
    from hyperi_ci.preflight import run_preflight

    dir_path = Path(project_dir) if project_dir else None
    cfg = load_config(reload=True, project_dir=dir_path)
    raise typer.Exit(run_preflight(cfg, project_dir=dir_path))


@app.command(name="release-commit")
def release_commit_cmd(
    version: Annotated[
        str,
        typer.Argument(help="Version just released (with or without leading v)"),
    ],
    branch: Annotated[
        str,
        typer.Option("--branch", help="Branch to update"),
    ] = "main",
    project_dir: Annotated[
        str | None,
        typer.Option("--project-dir", "-C", help="Project root directory"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Report what would be committed"),
    ] = False,
) -> None:
    """Commit the rendered VERSION + CHANGELOG.md back to the branch.

    Runs after the tag exists, and only ever adds an UNTAGGED commit — the
    property whose absence made `@semantic-release/git` orphan tags (issue
    #37). Uses the GitHub Git Data API, so it works from a checkout with
    `persist-credentials: false`. Idempotent: a branch already matching the
    rendered artefacts is left alone.
    """
    from hyperi_ci.release_commit import commit_release_artefacts

    dir_path = Path(project_dir) if project_dir else None
    raise typer.Exit(
        commit_release_artefacts(
            version=version, branch=branch, project_dir=dir_path, dry_run=dry_run
        )
    )


@app.command(name="seed-version")
def seed_version_cmd(
    project_dir: Annotated[
        str | None,
        typer.Option("--project-dir", "-C", help="Project root directory"),
    ] = None,
    show_source: Annotated[
        bool,
        typer.Option("--source", help="Also print where the version came from"),
    ] = False,
) -> None:
    """Print the version a tag-less repo should start from.

    Read from the project's own manifest (pyproject.toml, Cargo.toml,
    package.json); a project with nothing to declare starts at 0.1.0.
    Prints the bare version to stdout so it can be captured — the
    predict-version composite uses it to resolve a first release, instead
    of trusting the committed VERSION file (issue #85).
    """
    from hyperi_ci.version_source import seed_version

    dir_path = Path(project_dir) if project_dir else None
    version, source = seed_version(dir_path)
    typer.echo(f"{version}\t{source}" if show_source else version)


@app.command(name="seed-tag")
def seed_tag_cmd(
    project_dir: Annotated[
        str | None,
        typer.Option("--project-dir", "-C", help="Project root directory"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Report what would be tagged, create nothing"),
    ] = False,
) -> None:
    """Create the repo's first v* tag from its declared version.

    Run once, at adoption: the version pipeline reads git tags, and a repo
    with none has nothing to start from. Refuses (successfully) when a v*
    tag already exists — the repo already has its truth. The tag is a
    starting marker, not a release; the first publish bumps from it.
    """
    from hyperi_ci.seed import seed_tag

    dir_path = Path(project_dir) if project_dir else None
    raise typer.Exit(seed_tag(project_dir=dir_path, dry_run=dry_run))


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
        typer.Argument(
            help="Language (rust, typescript, golang, python). "
            "Defaults to 'all' = every language.",
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
            help="Install every entry unconditionally (bypass manifest matching). "
            "Use for runner image bake; stay default for CI-time conditional install.",
        ),
    ] = False,
) -> None:
    """Detect and install native system dependencies for a language.

    Examples:
        hyperi-ci install-native-deps --all        # bake every language
        hyperi-ci install-native-deps rust --all   # bake only Rust
        hyperi-ci install-native-deps              # CI-time: conditional
        hyperi-ci install-native-deps rust         # CI-time: Rust if triggered

    """
    from hyperi_ci.native_deps import _NATIVE_DEPS_DIR, print_needed
    from hyperi_ci.native_deps import install_native_deps as _install

    dir_path = Path(project_dir) if project_dir else None

    # `all` fans out to every language YAML in config/native-deps/, matching
    # the install-toolchains contract so the two commands behave alike.
    if language == "all":
        languages = sorted(f.stem for f in _NATIVE_DEPS_DIR.glob("*.yaml"))
    else:
        languages = [language]

    for lang in languages:
        if dry_run:
            print_needed(lang, project_dir=dir_path, all_mode=all_mode)
            continue
        rc = _install(lang, project_dir=dir_path, all_mode=all_mode)
        if rc != 0:
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


@app.command(name="install-all")
def install_all_cmd(
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", "-n", help="Show what would be installed without installing"
        ),
    ] = False,
    skip_toolchains: Annotated[
        bool,
        typer.Option(
            "--skip-toolchains",
            help="Skip the language toolchain bootstrap (rustup, Go, Node) and "
            "install only the apt-sourced deps.",
        ),
    ] = False,
) -> None:
    """Install everything hyperi-ci might need, for a runner image bake.

    Every toolchain family and every language's native deps, unconditionally --
    no manifest matching, because an image is built without a project in front
    of it. This is the ONE command a runner image Dockerfile calls.

    Why it exists: a pre-baked tool only pays off if it is what hyperi-ci would
    have installed anyway. Anything else gets skipped as already-present (and
    so silently overrides the pinned version) or reinstalled over the top (and
    so wasted the image build). Baking BY this command keeps the image and the
    CI-time install path the same code, so they cannot drift.

    Entries marked `bake: false` are excluded -- non-coinstallable toolsets
    stay install-on-demand so baking a default cannot lock out a job needing a
    different version.

    Covers three things, in order: the language toolchains from
    `config/bootstrap.yaml` (rustup, Go, Node), the multi-version apt families
    from `config/toolchains/`, then every language's `config/native-deps/`.

    Examples:
        hyperi-ci install-all                   # bake everything
        hyperi-ci install-all --dry-run         # show the plan
        hyperi-ci install-all --skip-toolchains # apt deps only

    """
    from hyperi_ci.bootstrap import install_toolchain_bootstrap, print_bootstrap_plan
    from hyperi_ci.native_deps import _NATIVE_DEPS_DIR, _TOOLCHAINS_DIR, print_needed
    from hyperi_ci.native_deps import install_native_deps as _install

    plan: list[tuple[str, str]] = [
        ("toolchains", f.stem) for f in sorted(_TOOLCHAINS_DIR.glob("*.yaml"))
    ]
    plan += [("native-deps", f.stem) for f in sorted(_NATIVE_DEPS_DIR.glob("*.yaml"))]

    if not plan:
        typer.echo("install-all found no toolchain or native-deps config", err=True)
        raise typer.Exit(1)

    # Language toolchains first: the apt families below include BOLT and the
    # cross-compilers that a Rust build then links against.
    if not skip_toolchains:
        typer.echo("install-all: language toolchains", err=True)
        if dry_run:
            print_bootstrap_plan()
        else:
            rc = install_toolchain_bootstrap()
            if rc != 0:
                typer.echo(f"install-all failed on toolchains (exit {rc})", err=True)
                raise typer.Exit(rc)

    for category, name in plan:
        typer.echo(f"install-all: {category}/{name}", err=True)
        if dry_run:
            print_needed(name, category=category, all_mode=True)
            continue
        rc = _install(name, category=category, all_mode=True)
        if rc != 0:
            typer.echo(f"install-all failed on {category}/{name} (exit {rc})", err=True)
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


@app.command(name="check-commits")
def check_commits_cmd() -> None:
    """Validate the conventional-commit messages in the CI push/PR range.

    Landing-gate counterpart to `check-commit` (single message, local
    commit-msg hook). Resolves the range from the CI event - push
    before..after (what lands on main) or PR base..HEAD - validates each
    commit, and is FATAL on push but ADVISORY on pull_request (branch
    commits may be squashed away). CI-only; a no-op locally. Driven by the
    dedicated `commit-check` workflow job, NOT the run-checks-gated quality
    job - so a merge to main is validated even when it is not a publish.
    """
    from hyperi_ci.quality import deprecated_files
    from hyperi_ci.quality.commit_validation import run

    # The always-on `commit-check` CI job is the cheapest run that fires on
    # every push/PR, so surface the deprecated-file nudge here too (the
    # run-checks-gated quality job is skipped on non-publish pushes).
    deprecated_files.scan()
    raise typer.Exit(run())


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
def update(
    target_version: Annotated[
        str | None,
        typer.Argument(help="Specific version to install (default: latest)"),
    ] = None,
    pre: Annotated[
        bool,
        typer.Option("--pre", help="Include pre-releases when resolving latest"),
    ] = False,
) -> None:
    """Update hyperi-ci to its channel's release (or a specific version).

    Which release "latest" means is the channel's decision: `live` (the
    default) takes the newest release on PyPI, `stable` takes the newest one
    that has soaked for 7 days. See `hyperi-ci autoupdate`.
    """
    from hyperi_ci.upgrade import run_upgrade

    rc = run_upgrade(version=target_version, pre=pre)
    raise typer.Exit(rc)


@app.command(hidden=True)
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
    """Update hyperi-ci -- the deprecated spelling of `update`.

    The verb is `update` across the toolchain (`hyperi-ai update`). This
    spelling keeps working because it is in docs and CI images; removal is a
    4.0 change.
    """
    from hyperi_ci.common import warn

    warn("`hyperi-ci upgrade` is deprecated -- use `hyperi-ci update`.")
    update(target_version=target_version, pre=pre)


@app.command()
def autoupdate(
    action: Annotated[
        str,
        typer.Argument(
            # No square brackets: rich reads them as markup and eats the text.
            help="status (default) | enable | disable | channel live|stable "
            "| freeze | unfreeze",
        ),
    ] = "status",
    value: Annotated[
        str | None,
        typer.Argument(help="Target channel, for `channel`"),
    ] = None,
) -> None:
    """Show or change how hyperi-ci updates itself.

    Same channels as hyperi-ai, so one mental model covers both:

      live    the newest release on PyPI, adopted as soon as it exists (default)
      stable  the newest release aged past the 7-day cooldown

    hyperi-ci is a PyPI package, so neither channel follows unreleased commits
    the way hyperi-ai's clone does. `freeze` is an orthogonal kill-switch: no
    auto-update on any channel until `unfreeze`, and hyperi-ai's freeze counts
    here too. `disable` stops auto-update while leaving the channel set;
    `HYPERCI_AUTO_UPDATE=false` (what the CI images set) still works and wins.

    State lives in ~/.config/hyperi-ci/channel.json. With none of its own,
    hyperi-ci inherits hyperi-ai's channel choice -- `status` names the source.
    """
    from hyperi_ci import channel as _channel
    from hyperi_ci.upgrade import autoupdate_status

    if action == "status":
        typer.echo(json.dumps(autoupdate_status(), indent=2))
        return

    if action == "channel":
        if value is None:
            name, source = _channel.resolve_channel()
            typer.echo(f"hyperi-ci autoupdate: channel is '{name}' (from {source})")
            return
        try:
            _channel.write_channel(value)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
        # Echo what was persisted -- write_channel silently normalises
        # hyperi-ai's retired "edge" and "nightly" aliases to "live".
        typer.echo(f"hyperi-ci autoupdate: channel set to '{_channel.read_channel()}'")
        return

    if action in ("enable", "disable"):
        _channel.write_enabled(action == "enable")
        typer.echo(f"hyperi-ci autoupdate: {action}d")
        if os.environ.get("HYPERCI_AUTO_UPDATE", "").lower() in ("true", "false"):
            typer.echo(
                "note: HYPERCI_AUTO_UPDATE is set in this environment and "
                "overrides the stored flag"
            )
        return

    if action == "freeze":
        _channel.freeze()
        typer.echo(
            "hyperi-ci autoupdate: FROZEN (no updates on any channel). "
            "Clear with `hyperi-ci autoupdate unfreeze`."
        )
        return

    if action == "unfreeze":
        _channel.unfreeze()
        typer.echo("hyperi-ci autoupdate: unfrozen")
        if _channel.is_frozen():
            typer.echo(
                "note: still frozen by hyperi-ai -- clear that with "
                "`hyperi-ai autoupdate unfreeze`"
            )
        return

    typer.echo(f"Unknown action: {action}", err=True)
    typer.echo(
        "Valid: status, enable, disable, channel [live|stable], freeze, unfreeze",
        err=True,
    )
    raise typer.Exit(1)


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
    ArgoCD manifests, OpenTofu skeleton, and MkDocs documentation site
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
