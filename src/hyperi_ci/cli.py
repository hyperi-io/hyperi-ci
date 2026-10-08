# Project:   HyperI CI
# File:      src/hyperi_ci/cli.py
# Purpose:   CLI entry point for hyperi-ci tool (Typer via scalo)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
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
    hyperi-ci watch [RUN_ID] [-w NAME]  Watch a GitHub Actions run to completion
    hyperi-ci logs [RUN_ID] [-w NAME]   Fetch and filter GitHub Actions run logs
    hyperi-ci release [<tag>]           Release/retry HEAD, or re-release a tag
    hyperi-ci update                    Update to the channel's release
    hyperi-ci autoupdate                Show/set self-update channel + freeze
    hyperi-ci check-commit              Validate commit message format
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
  - --force semantics vary (overwrite vs skip-checks) -- document in each command
"""

import json
import os
import sys
from importlib.metadata import distribution
from pathlib import Path
from typing import Annotated

import typer

from hyperi_ci import __version__
from hyperi_ci.config import CIConfig, load_config
from hyperi_ci.detect import detect_language
from hyperi_ci.dispatch import VALID_STAGES, run_stage
from hyperi_ci.languages.tiering import SuiteTier
from hyperi_ci.stamp import SKIP_STAMP_CMD_ENV
from hyperi_ci.version_source import build_version

app = typer.Typer(
    name="hyperi-ci",
    help="HyperI CI -- polyglot CI/CD tool",
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
    -- a number that drifts further from the tree on every release, and belongs
    to no release at all. Re-resolving through the same function the build
    back-end uses keeps the report honest without a re-sync.

    ``HYPERCI_VERSION`` is excluded: it is process-wide rather than scoped to a
    tree, so a release run in another project would have this answer with that
    project's version under this checkout's path.

    Args:
        checkout: Filesystem path of the editable checkout.

    Returns:
        A bare ``X.Y.Z``, falling back to the frozen metadata if the checkout
        can no longer be read -- ``--version`` must not be the thing that fails.

    """
    try:
        return build_version(Path(checkout), allow_env=False)
    except Exception as exc:  # noqa: BLE001 - --version must not be the failure
        # Warn rather than fall through quietly: the fallback is the frozen
        # number this function exists to replace, so a silent one reads as the
        # bug it fixes.
        from hyperi_ci.common import warn

        warn(
            f"cannot resolve the checkout's version ({exc}) -- showing the installed one"
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
    """HyperI CI -- polyglot CI/CD tool."""
    from hyperi_ci.upgrade import maybe_auto_update

    maybe_auto_update()


_TIER_HELP = (
    "Test tier: core (the project's default selection) or full (also the "
    "deselected and ignored tests). Overrides test.tier and HYPERCI_TEST_TIER."
)


def _apply_test_tier(tier: SuiteTier | None) -> None:
    """Hand ``--tier`` to the config cascade, above the env var it overwrites."""
    if tier is not None:
        os.environ["HYPERCI_TEST_TIER"] = tier.value


@app.command()
def run(
    stage: Annotated[str, typer.Argument(help="Stage to run")],
    project_dir: Annotated[
        str | None,
        typer.Option("--project-dir", "-C", help="Project root directory"),
    ] = None,
    tier: Annotated[
        SuiteTier | None,
        typer.Option("--tier", help=_TIER_HELP),
    ] = None,
) -> None:
    """Run a CI stage (setup, quality, test, build, release)."""
    if stage not in VALID_STAGES:
        typer.echo(f"Invalid stage: {stage}", err=True)
        typer.echo(f"Valid stages: {', '.join(VALID_STAGES)}", err=True)
        raise typer.Exit(1)
    if tier is not None and stage != "test":
        typer.echo(f"--tier applies to the test stage, not {stage}", err=True)
        raise typer.Exit(1)

    _apply_test_tier(tier)
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
    tier: Annotated[
        SuiteTier | None,
        typer.Option("--tier", help=_TIER_HELP),
    ] = None,
) -> None:
    """Run local pre-push checks (quality + test by default).

    A merge to main whose pushed range is not release-worthy runs no
    quality or test job in CI, so this local run is the only gate it gets.

    With ``--strict``, warn-tier quality findings (which CI tolerates but
    still prints) are treated as failures, so nothing is carried into a
    push unseen. Fix each, or flag it to ignore if it genuinely should be.
    A tool that is not installed locally (and has no uv fallback) is still
    warn-skipped even under ``--strict`` - strict enforces what runs, not
    what your machine has; CI, where the tools are present, is the backstop.

    ``--tier full`` runs the tests the project deselects or ignores by
    default as well. ``--full`` is unrelated: it adds the build stage.
    """
    dir_path = Path(project_dir) if project_dir else None

    if strict:
        os.environ["HYPERCI_QUALITY_STRICT"] = "1"
    _apply_test_tier(tier)

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


_SARIF_HELP = (
    "Write combined SARIF here (opt-in). The workflow uploads it to "
    "code scanning only where GitHub Code Security is enabled."
)


def _lint_iac(
    directory: str, sarif: str | None, dimensions: tuple[str, ...] | None = None
) -> int:
    """Load the directory's config and run lint-iac over it."""
    from hyperi_ci.config import load_config
    from hyperi_ci.quality import lint_iac

    root = Path(directory)
    config = load_config(project_dir=root)
    return lint_iac.run(
        root, config, sarif_path=sarif, dimensions=dimensions or lint_iac.DIMENSIONS
    )


@app.command(name="lint-iac")
def lint_iac_cmd(
    directory: Annotated[
        str,
        typer.Argument(
            help="Directory to lint (charts, manifests, tofu, ansible, compose)"
        ),
    ] = ".",
    sarif: Annotated[str | None, typer.Option("--sarif", help=_SARIF_HELP)] = None,
) -> None:
    """Lint the infrastructure code in a repo, every dimension it holds.

    Each dimension switches on by marker: Dockerfiles (hadolint), compose files
    (resolution and image pins), Helm charts (render per ci values, render
    twice, kubeconform -strict), kustomizations, plain manifests, kube-linter
    and Checkov (advisory), OpenTofu (fmt, init, validate per root), ansible
    (galaxy install, ansible-lint, yamllint), and ``iac.generated`` entries
    (regenerate and fail on a diff).

    Dimensions run one at a time with a per-tool timeout
    (``iac.timeout_seconds``). It never plans, applies, installs a chart or
    starts a cluster, so the job needs no credentials.
    """
    raise typer.Exit(_lint_iac(directory, sarif))


def _deprecated_verb(old: str) -> None:
    """Say once that ``old`` is now an alias of lint-iac."""
    from hyperi_ci.common import announce

    announce(
        f"`hyperi-ci {old}` is deprecated and runs part of `hyperi-ci lint-iac`; "
        "call lint-iac instead",
        "hyperi-ci deprecated verb",
    )


@app.command(name="lint-manifests")
def lint_manifests_cmd(
    directory: Annotated[
        str,
        typer.Argument(help="Directory to lint (Helm charts / k8s manifests / IaC)"),
    ] = ".",
    sarif: Annotated[str | None, typer.Option("--sarif", help=_SARIF_HELP)] = None,
) -> None:
    """Deprecated: runs lint-iac's helm, kustomize, manifests, kube-linter and checkov."""
    from hyperi_ci.quality import lint_iac

    _deprecated_verb("lint-manifests")
    raise typer.Exit(_lint_iac(directory, sarif, lint_iac.MANIFEST_DIMENSIONS))


@app.command(name="lint-compose")
def lint_compose_cmd(
    directory: Annotated[
        str,
        typer.Argument(help="Directory to lint (docker-compose files)"),
    ] = ".",
    sarif: Annotated[str | None, typer.Option("--sarif", help=_SARIF_HELP)] = None,
) -> None:
    """Deprecated: runs lint-iac's compose dimension."""
    from hyperi_ci.quality import lint_iac

    _deprecated_verb("lint-compose")
    raise typer.Exit(_lint_iac(directory, sarif, lint_iac.COMPOSE_DIMENSIONS))


@app.command(name="lint-docs")
def lint_docs_cmd(
    directory: Annotated[
        str,
        typer.Argument(help="Directory to lint (markdown documentation)"),
    ] = ".",
    sarif: Annotated[str | None, typer.Option("--sarif", help=_SARIF_HELP)] = None,
) -> None:
    """Check the markdown documentation in this repo.

    Runs five checks: doc-paths (a doc naming a file the repo no longer has),
    doc-links (lychee over internal links and anchors, offline), mermaid-parse
    (every fenced block against mermaid's own grammar), markdownlint (mechanical
    syntax) and the docs-untouched nudge.

    All five start at ``warn`` and gate only where a repo has promoted them, so
    adopting this does not turn an existing docs tree red. The same checks run
    inside ``hyperi-ci run quality``; this verb is for a docs repo that has no
    language pipeline to hang them off.
    """
    from hyperi_ci.config import load_config
    from hyperi_ci.quality import lint_docs

    root = Path(directory)
    config = load_config(project_dir=root)
    rc = lint_docs.run(root, config, sarif_path=sarif)
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
            "--release",
            "--publish",  # deprecated spelling, still accepted
            help=(
                "Stamp HEAD with the `Release: true` trailer before pushing -- "
                "the single CI run tags and publishes via the version-first "
                "pipeline. (--publish is the deprecated spelling.)"
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
                "publishes. Implies --release."
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
                "publishes. Implies --release. (Major bumps require a "
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
                "Equivalent to setting HYPERCI_ALLOW_FEAT=1 -- opts in to a "
                "feat: commit (MINOR bump). Required when HEAD is a feat: "
                "commit and you're using --release, since the trailer "
                "amend re-invokes the commit-msg hook gate."
            ),
        ),
    ] = False,
    allow_breaking: Annotated[
        bool,
        typer.Option(
            "--allow-breaking",
            help=(
                "Equivalent to setting HYPERCI_ALLOW_BREAKING=1 -- opts in "
                "to a commit containing the BREAKING-CHANGE marker (MAJOR "
                "bump). Required when HEAD has the marker and you're "
                "using --release."
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

    Without ``--release`` nothing ships: the CI run builds, tags and
    publishes nothing, and runs quality + test only when the pushed
    range is release-worthy.

    With ``--release`` (canonical) or ``--publish`` (deprecated): amends
    the head commit with the ``Release: true`` trailer, then pushes. The
    resulting CI run goes through the version-first pipeline -- predicts
    the next version, stamps it into Cargo.toml/VERSION before build,
    creates the tag, and publishes to all configured registries -- all
    in one workflow.

    With ``--bump-patch`` or ``--bump-minor``: same as ``--release`` but
    adds an empty release-marker commit on top of HEAD. Use this when
    you want to ship a release whose actual commits are no-bump types
    (``docs:``, ``chore:``, etc.) -- saves you from inventing a fake
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

    # CLI flag -> env var: the commit-msg hook (which fires during the
    # trailer amend inside _publish_push) reads HYPERCI_ALLOW_FEAT /
    # HYPERCI_ALLOW_BREAKING. Setting them here means a single
    # `hyperi-ci push --release --allow-feat` works without exporting
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

    Note: `--force` here means "overwrite existing files" -- different from
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
    no_stamp_cmd: Annotated[
        bool,
        typer.Option(
            "--no-stamp-cmd",
            envvar=SKIP_STAMP_CMD_ENV,
            help=(
                "Write VERSION and the manifest but do not run release.stamp_cmd, "
                "for a job holding credentials the repo's code must not reach."
            ),
        ),
    ] = False,
) -> None:
    """Stamp the version into VERSION + the language manifest.

    Central, version-first step run by every language workflow before
    build. Writes the VERSION file (language-agnostic) and delegates the
    manifest stamp (Cargo.toml / pyproject.toml / package.json) to the
    detected language. Go is a no-op (version injected via ldflags).
    """
    from hyperi_ci.stamp import stamp_version

    dir_path = Path(project_dir) if project_dir else None
    raise typer.Exit(
        stamp_version(version, project_dir=dir_path, run_stamp_cmd=not no_stamp_cmd)
    )


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

    An optional input such as `test-tier` is held to the last two only. A
    caller without it is noted, not counted as drifted.

    Never writes. The caller file belongs to the consumer repo.
    """
    from hyperi_ci.caller_audit import (
        OPTIONAL_CALLER_INPUTS,
        RUST_OPTIONAL_CALLER_INPUTS,
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

    for name in (*OPTIONAL_CALLER_INPUTS, *RUST_OPTIONAL_CALLER_INPUTS):
        lacking = [r.repo for r in reports if name in r.optional_absent]
        if lacking:
            info(
                f"{len(lacking)} caller(s) do not declare optional input {name} "
                "(not drift; `hyperi-ci init` adds it)"
            )

    info(f"Audited {len(reports)} caller(s)")
    if not drifted:
        success("Every caller honours the dispatch contract")
        raise typer.Exit(0)

    error(f"{len(drifted)} caller(s) drifted -- a release from HEAD will fail")
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

    A FAILING gate is deliberately not reported -- GitHub already shows a red
    repo as red, and pre-GA repos are expected to be red. Only the invisible
    fault is reported: a gate that never ran.

    Repos declaring a pre-GA `release.channel` are skipped, since a dormant
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
        error("Give --org or --repo -- there is no local equivalent to audit")
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
                f"Skipping {len(prerelease)} pre-GA repo(s) -- a dormant gate is "
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
        typer.Option("--outcome", help="success, failure or commit-back-failed"),
    ] = "success",
    run_url: Annotated[
        str,
        typer.Option("--run-url", help="Link to the run, for an issue"),
    ] = "",
    project_dir: Annotated[
        str | None,
        typer.Option("--project-dir", "-C", help="Project root directory"),
    ] = None,
) -> None:
    """Announce a release, or record that one failed.

    `--outcome success` comments on every issue and PR carried by the release;
    `--outcome failure` opens a tracker issue so a release that dies overnight
    is waiting in the morning; `--outcome commit-back-failed` records a release
    that shipped but could not push VERSION and CHANGELOG.md back to main. All
    are idempotent, and all always exit 0 -- a notification must never be the
    thing that fails a release.

    Slack is off unless `notify.slack.webhook_env` names an env var holding a
    webhook URL.
    """
    from hyperi_ci.release_notify import (
        notify_commit_back_failed,
        notify_failure,
        notify_slack,
        notify_success,
    )

    dir_path = Path(project_dir) if project_dir else None
    cfg = load_config(reload=True, project_dir=dir_path)
    bare = version.removeprefix("v")

    if outcome == "failure":
        rc = notify_failure(version=version, run_url=run_url)
        notify_slack(cfg, text=f"Release of v{bare} FAILED")
    elif outcome == "commit-back-failed":
        rc = notify_commit_back_failed(version=version, run_url=run_url)
        notify_slack(cfg, text=f"Released v{bare}, but its commit-back to main FAILED")
    else:
        rc = notify_success(version=version, cwd=str(dir_path) if dir_path else None)
        notify_slack(cfg, text=f"Released v{bare}")
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
    ones whose handler hard-fails without a token -- a missing
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

    Runs after the tag exists, and only ever adds an UNTAGGED commit -- the
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


@app.command(name="release-prepare")
def release_prepare_cmd(
    version: Annotated[
        str,
        typer.Argument(help="Version being released (with or without leading v)"),
    ],
    out: Annotated[
        str,
        typer.Option("--out", help="Directory to write the prepared artefacts to"),
    ],
    phase: Annotated[
        str,
        typer.Option(
            "--phase",
            help=(
                "stamp: stamp and copy the stamp_paths files to --out. "
                "package: checks and packing, prepared.json to --out. "
                "all: both, stamped files under --out/stamped."
            ),
        ),
    ] = "all",
    project_dir: Annotated[
        str | None,
        typer.Option("--project-dir", "-C", help="Project root directory"),
    ] = None,
) -> None:
    """Stamp the release and run everything in it that executes repo code.

    The half of a release that holds no credentials: the version stamp and
    `release.stamp_cmd`, cargo-semver-checks and `cargo package`, `npm pack`.
    `run release` with HYPERCI_RELEASE_PREPARED naming the package output then
    only uploads (issue #409). The two phases exist so the stamp outputs can
    leave the job before the packaging code runs.
    """
    from hyperi_ci.release_prepare import Phase, prepare_release

    try:
        chosen = Phase(phase)
    except ValueError:
        typer.echo(f"--phase must be one of {', '.join(Phase)}", err=True)
        raise typer.Exit(1) from None
    dir_path = Path(project_dir) if project_dir else None
    raise typer.Exit(
        prepare_release(version, out_dir=Path(out), phase=chosen, project_dir=dir_path)
    )


@app.command(name="release-verify")
def release_verify_cmd(
    project_dir: Annotated[
        str | None,
        typer.Option("--project-dir", "-C", help="Project root directory"),
    ] = None,
) -> None:
    """Check the prepared release against this run before anything is tagged.

    Reads HYPERCI_RELEASE_PREPARED and HYPERCI_VERSION, and fails when the
    directory is unusable or was prepared for another version or language.
    """
    from hyperi_ci.dispatch import check_prepared

    root = Path(project_dir) if project_dir else Path.cwd()
    language = detect_language(root)
    if not language:
        typer.echo("Could not detect project language", err=True)
        raise typer.Exit(1)
    raise typer.Exit(check_prepared(language))


@app.command(name="publish-charts")
def publish_charts_cmd(
    charts: Annotated[
        list[str] | None,
        typer.Option(
            "--charts",
            help="Chart directory or glob, relative to the project root "
            "(repeatable). Overrides release.helm.charts and runs whatever "
            "release.helm.enabled says.",
        ),
    ] = None,
    registry: Annotated[
        str | None,
        typer.Option("--registry", help="oci:// URL. Overrides release.helm.registry"),
    ] = None,
    version: Annotated[
        str | None,
        typer.Option(
            "--version",
            help="Chart version, used verbatim. Default: the release version "
            "(HYPERCI_VERSION, then VERSION, then the latest tag)",
        ),
    ] = None,
    output: Annotated[
        str,
        typer.Option(
            "--output", "-o", help="text, or json for a result list on stdout"
        ),
    ] = "text",
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Package the charts, push nothing"),
    ] = False,
    project_dir: Annotated[
        str | None,
        typer.Option("--project-dir", "-C", help="Project root directory"),
    ] = None,
) -> None:
    """Package committed Helm charts and push them to an OCI registry.

    A version the registry already holds is not pushed again; its existing
    digest is reported. A glob skips library charts, and a library chart named
    by its exact directory is published. Logs go to stderr, so
    `--output json` leaves stdout as one JSON list of
    {chart, version, digest, ref, signed}.
    """
    from hyperi_ci.release.charts import as_json, publish_charts

    if output not in {"text", "json"}:
        typer.echo("--output must be text or json", err=True)
        raise typer.Exit(2)
    root = Path(project_dir) if project_dir else Path.cwd()
    cfg = load_config(project_dir=root, report_removed=output == "text")
    rc, results = publish_charts(
        cfg, root, charts=charts, registry=registry, version=version, dry_run=dry_run
    )
    if output == "json":
        typer.echo(json.dumps(as_json(results), indent=2))
    raise typer.Exit(rc)


chart_app = typer.Typer(help="Build Helm charts from a deployment contract")
app.add_typer(chart_app, name="chart")


@chart_app.command(name="assemble")
def chart_assemble_cmd(
    image: Annotated[
        str,
        typer.Option("--image", help="The pushed image, <repo>:<tag>@sha256:<digest>"),
    ],
    output_dir: Annotated[
        str | None,
        typer.Option(
            "--output-dir", help="Where the chart dir goes. Default: a new temp dir"
        ),
    ] = None,
    library_dir: Annotated[
        str | None,
        typer.Option(
            "--library-dir",
            help="An unpacked scalo-service chart to use instead of pulling one",
        ),
    ] = None,
    binary: Annotated[
        str | None,
        typer.Option(
            "--binary", help="Command whose generate-artefacts emits the contract"
        ),
    ] = None,
    registry: Annotated[
        str | None,
        typer.Option("--registry", help="oci:// URL. Overrides release.helm.registry"),
    ] = None,
    version: Annotated[
        str | None,
        typer.Option("--version", help="Chart version. Default: the release version"),
    ] = None,
    project_dir: Annotated[
        str | None,
        typer.Option("--project-dir", "-C", help="Project root directory"),
    ] = None,
) -> None:
    """Assemble a thin chart on scalo-service from release.helm.contract.

    The contract is validated against the schema the scalo-service chart
    ships, and the chart is written outside the repo. Its directory is the
    only line on stdout, and nothing is printed when the project sets no
    contract.
    """
    from hyperi_ci.release.assemble import assemble_chart

    root = Path(project_dir) if project_dir else Path.cwd()
    rc, chart = assemble_chart(
        load_config(project_dir=root),
        root,
        image=image,
        output_dir=Path(output_dir) if output_dir else None,
        library_dir=Path(library_dir) if library_dir else None,
        binary=binary,
        registry=registry,
        version=version,
    )
    if chart is not None:
        typer.echo(str(chart))
    raise typer.Exit(rc)


vendor_app = typer.Typer(help="Mirror files one way from another repo at a pinned ref")
app.add_typer(vendor_app, name="vendor")


@vendor_app.command(name="sync")
def vendor_sync_cmd(
    project_dir: Annotated[
        str | None,
        typer.Option("--project-dir", "-C", help="Project root directory"),
    ] = None,
) -> None:
    """Fetch every vendor: file at its pinned ref and rewrite the lock file."""
    from hyperi_ci.vendor import run_sync

    root = Path(project_dir) if project_dir else Path.cwd()
    raise typer.Exit(run_sync(load_config(project_dir=root), root))


@vendor_app.command(name="check")
def vendor_check_cmd(
    project_dir: Annotated[
        str | None,
        typer.Option("--project-dir", "-C", help="Project root directory"),
    ] = None,
) -> None:
    """Fail when a vendored file was edited by hand or its pin moved unsynced."""
    from hyperi_ci.vendor import run

    root = Path(project_dir) if project_dir else Path.cwd()
    raise typer.Exit(run(load_config(project_dir=root), root))


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
    Prints the bare version to stdout so it can be captured -- the
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
    tag already exists -- the repo already has its truth. The tag is a
    starting marker, not a release; the first publish bumps from it.
    """
    from hyperi_ci.seed import seed_tag

    dir_path = Path(project_dir) if project_dir else None
    raise typer.Exit(seed_tag(project_dir=dir_path, dry_run=dry_run))


def _apply_org_classification(cfg: CIConfig, project_dir: Path | None) -> None:
    """Fill an undeclared classification from the GitHub org property.

    The org property is the standard's third rung. It is opt-in rather
    than part of the config load because it costs a network round-trip
    and an outside contributor's clone cannot read it at all.

    Args:
        cfg: The loaded config, mutated in place when the org answers.
        project_dir: Repo root, used to resolve the repo slug.

    """
    from hyperi_ci import classification
    from hyperi_ci.description_source import repo_slug

    repo = repo_slug(project_dir)
    if not repo:
        return
    value = classification.from_org_property(repo)
    if not value:
        return
    cfg.classification = value
    cfg.classification_source = classification.SOURCE_ORG
    cfg.classification_effective = value
    cfg._raw["classification"] = value
    cfg._raw["classification_source"] = classification.SOURCE_ORG
    cfg._raw["classification_effective"] = value


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
    org_classification: Annotated[
        bool,
        typer.Option(
            "--org-classification",
            help="Ask the GitHub org for the classification when no in-repo "
            "marker declares one (needs org API access)",
        ),
    ] = False,
) -> None:
    """Show merged configuration (YAML by default, --json for scripts).

    `classification` reports the declared repo category, with
    `classification_source` naming the marker that answered and
    `classification_effective` the category to act on -- `internal` when
    nothing declares one.
    """
    import yaml

    dir_path = Path(project_dir) if project_dir else None
    cfg = load_config(reload=True, project_dir=dir_path, report_removed=not as_json)

    if org_classification and not cfg.classification:
        _apply_org_classification(cfg, dir_path)

    if as_json:
        typer.echo(json.dumps(cfg._raw, indent=2, default=str))
    else:
        typer.echo(yaml.safe_dump(cfg._raw, sort_keys=False, default_flow_style=False))


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
    inputs: Annotated[
        list[str] | None,
        typer.Option(
            "--input",
            help="workflow_dispatch input as key=value (repeatable)",
        ),
    ] = None,
    repo: Annotated[
        str | None,
        typer.Option(
            "--repo",
            "-R",
            help=("Target repo as owner/name. Defaults to the cwd's git remote."),
        ),
    ] = None,
    project_dir: Annotated[
        str | None,
        typer.Option("--project-dir", "-C", help="Project root directory"),
    ] = None,
) -> None:
    """Trigger a GitHub Actions workflow run.

    Dispatches the workflow via `gh workflow run`. Use --watch to block
    until the run completes -- equivalent to running `hyperi-ci trigger`
    then `hyperi-ci watch` as separate commands.

    --workflow takes any workflow the repo carries, not only the ci.yml
    hyperi-ci scaffolds, and accepts a filename, a bare stem or the
    display name. Pass `--input key=value` once per workflow_dispatch
    input; a workflow declaring required inputs cannot be dispatched
    without them.
    """
    from hyperi_ci.trigger import parse_inputs, trigger_workflow

    try:
        dispatch_inputs = parse_inputs(inputs)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    rc = trigger_workflow(
        workflow=workflow,
        ref=ref,
        inputs=dispatch_inputs,
        watch=watch_run,
        timeout=timeout,
        interval=interval,
        repo=repo,
        project_dir=Path(project_dir) if project_dir else None,
    )
    raise typer.Exit(rc)


@app.command()
def watch(
    run_id: Annotated[
        str | None,
        typer.Argument(help="Run ID (resolves HEAD's own run if omitted)"),
    ] = None,
    workflow: Annotated[
        str | None,
        typer.Option(
            "--workflow",
            "-w",
            help=(
                "Workflow name to pin on (e.g. 'Test'). Defaults to the "
                "name this project's .github/workflows/ci.yml declares; "
                "an ambiguous choice is refused, never guessed."
            ),
        ),
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
                "Defaults to the cwd's git remote -- set this when watching "
                "a run in a different repo than your cwd."
            ),
        ),
    ] = None,
    pr: Annotated[
        int | None,
        typer.Option(
            "--pr",
            help=(
                "Pin to this pull request's head commit -- the anchor for a "
                "run that fired on pull_request rather than on HEAD."
            ),
        ),
    ] = None,
    branch: Annotated[
        str | None,
        typer.Option(
            "--branch",
            help="Pin to the newest commit on this branch that has runs.",
        ),
    ] = None,
    commit: Annotated[
        str | None,
        typer.Option("--commit", help="Pin to this commit instead of HEAD."),
    ] = None,
    project_dir: Annotated[
        str | None,
        typer.Option("--project-dir", "-C", help="Project root directory"),
    ] = None,
) -> None:
    """Watch a GitHub Actions run to completion.

    With no run ID, watches the run built from the commit at HEAD, pinned
    to the workflow this project declares in ci.yml. Name another with
    --workflow; an ambiguous choice is refused rather than guessed.

    --pr, --branch and --commit reach a run that is not on the current
    branch head. Where nothing resolves, the refusal lists the runs
    GitHub does hold rather than reporting "no runs found".
    """
    from hyperi_ci.watch import watch_run

    rc = watch_run(
        run_id=run_id,
        workflow=workflow,
        branch=branch,
        commit=commit,
        pr=pr,
        timeout=timeout,
        interval=interval,
        repo=repo,
        project_dir=Path(project_dir) if project_dir else None,
    )
    raise typer.Exit(rc)


@app.command()
def rerun(
    run_id: Annotated[
        str | None,
        typer.Argument(help="Run ID (resolves HEAD's own run if omitted)"),
    ] = None,
    workflow: Annotated[
        str | None,
        typer.Option(
            "--workflow",
            "-w",
            help=(
                "Workflow name to pin on when resolving HEAD's run. "
                "Ignored when a run ID is given."
            ),
        ),
    ] = None,
    all_jobs: Annotated[
        bool,
        typer.Option("--all", help="Re-run every job, not just the failed ones"),
    ] = False,
    repo: Annotated[
        str | None,
        typer.Option(
            "--repo",
            "-R",
            help="Target repo as owner/name. Defaults to the cwd's git remote.",
        ),
    ] = None,
    pr: Annotated[
        int | None,
        typer.Option("--pr", help="Pin to this pull request's head commit."),
    ] = None,
    branch: Annotated[
        str | None,
        typer.Option(
            "--branch",
            help="Pin to the newest commit on this branch that has runs.",
        ),
    ] = None,
    commit: Annotated[
        str | None,
        typer.Option("--commit", help="Pin to this commit instead of HEAD."),
    ] = None,
    project_dir: Annotated[
        str | None,
        typer.Option("--project-dir", "-C", help="Project root directory"),
    ] = None,
) -> None:
    """Re-run a GitHub Actions run, failed jobs only by default.

    For genuine infra incidents -- a GitHub outage, a registry 5xx. A flaky
    test this project owns is a race to fix, not a run to repeat.

    --pr, --branch and --commit reach a run that is not on the current
    branch head.
    """
    from hyperi_ci.rerun import rerun_run

    rc = rerun_run(
        run_id=run_id,
        workflow=workflow,
        branch=branch,
        commit=commit,
        pr=pr,
        repo=repo,
        project_dir=Path(project_dir) if project_dir else None,
        failed_only=not all_jobs,
    )
    raise typer.Exit(rc)


@app.command()
def logs(
    run_id: Annotated[
        str | None,
        typer.Argument(help="Run ID (resolves HEAD's own run if omitted)"),
    ] = None,
    workflow: Annotated[
        str | None,
        typer.Option(
            "--workflow",
            "-w",
            help=(
                "Workflow name to pin on (e.g. 'Test'). Defaults to the "
                "name this project's .github/workflows/ci.yml declares; "
                "an ambiguous choice is refused, never guessed."
            ),
        ),
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
    repo: Annotated[
        str | None,
        typer.Option(
            "--repo",
            "-R",
            help="Target repo as owner/name. Defaults to the cwd's git remote.",
        ),
    ] = None,
    pr: Annotated[
        int | None,
        typer.Option("--pr", help="Pin to this pull request's head commit."),
    ] = None,
    branch: Annotated[
        str | None,
        typer.Option(
            "--branch",
            help="Pin to the newest commit on this branch that has runs.",
        ),
    ] = None,
    commit: Annotated[
        str | None,
        typer.Option("--commit", help="Pin to this commit instead of HEAD."),
    ] = None,
    project_dir: Annotated[
        str | None,
        typer.Option("--project-dir", "-C", help="Project root directory"),
    ] = None,
) -> None:
    """Fetch and filter GitHub Actions run logs.

    With no run ID, reads the run built from the commit at HEAD, pinned
    to the workflow this project declares in ci.yml. Name another with
    --workflow; an ambiguous choice is refused rather than guessed.
    --failed always names the run it read, so an empty result cannot
    pass for a green build.

    --pr, --branch and --commit reach a run that is not on the current
    branch head; --repo reads one in another repo.
    """
    from hyperi_ci.logs import fetch_logs

    rc = fetch_logs(
        run_id=run_id,
        workflow=workflow,
        branch=branch,
        commit=commit,
        pr=pr,
        repo=repo,
        project_dir=Path(project_dir) if project_dir else None,
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
            help="Install every entry unconditionally (bypass manifest matching). "
            "Used for runner image bake. Default is conditional install.",
        ),
    ] = False,
) -> None:
    """Install the apt toolchain families: the default LLVM major, GCC 13/14.

    By default fans out across every family in `config/toolchains/` and
    matches project manifests to decide what to install. Pass `--all` on
    a runner image bake to install every entry of every family regardless
    of manifest. LLVM is always versions.yaml `llvm`, never the designated
    major.

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

    Entries marked `bake: false` are excluded and stay install-on-demand. That
    is for a toolset whose packages declare `Conflicts` across versions, where
    baking one version would lock out a job needing another.

    Covers three things, in order: the language toolchains from
    `config/bootstrap.yaml` (rustup, Go, Node), the apt families from
    `config/toolchains/`, then every language's `config/native-deps/`. The
    toolchain LLVM is always versions.yaml `llvm`. The native-deps entries
    follow the designated major, so HYPERCI_LLVM_VERSION or a `.hyperi-ci.yaml`
    in the working directory moves the bolt, lld and clang they install.

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
        msg = Path(message_file).read_text(encoding="utf-8", errors="replace").strip()
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
    job - so a merge to main is validated even when it is not release-worthy.
    """
    from hyperi_ci.quality import deprecated_files
    from hyperi_ci.quality.commit_validation import run

    # The always-on `commit-check` CI job is the cheapest run that fires on
    # every push/PR, so surface the deprecated-file nudge here too (the
    # run-checks-gated quality job is skipped on non-release-worthy pushes).
    deprecated_files.scan()
    raise typer.Exit(run())


@app.command(name="gate-check")
def gate_check_cmd() -> None:
    """Fail when the checks the gate required did not actually run.

    The terminal job of every language workflow. GitHub counts a SKIPPED
    required check as satisfied, so branch protection naming `ci / Quality` is
    satisfied by Quality not running. This job always runs, so the context it
    publishes cannot be satisfied by a skip (issue #177).

    Reads the plan job's gate and the results of the jobs it governs from the
    environment, because only the workflow knows what the runner decided.
    CI-only; a no-op locally.
    """
    import os

    from hyperi_ci.common import error, info, is_ci, success
    from hyperi_ci.gate_result import evaluate

    if not is_ci():
        info("gate-check is a CI-only job -- nothing to decide locally.")
        return

    def _results(*names: str) -> dict[str, str]:
        found = {n: os.environ.get(f"HYPERCI_GATE_{n.upper()}", "") for n in names}
        return {name: result for name, result in found.items() if result}

    verdict = evaluate(
        run_checks=os.environ.get("HYPERCI_GATE_RUN_CHECKS", "") == "true",
        run_build=os.environ.get("HYPERCI_GATE_RUN_BUILD", "") == "true",
        plan=os.environ.get("HYPERCI_GATE_PLAN", ""),
        checks=_results("quality", "test"),
        build=_results("build"),
    )
    if verdict.ok:
        success(verdict.reason)
        return
    error(verdict.reason)
    print(f"::error title=hyperi-ci gate::{verdict.reason}")
    raise typer.Exit(1)


def _release_impl(
    tag: str | None,
    list_tags: bool,
    dry_run: bool,
    bump: str | None = None,
    version: str | None = None,
) -> None:
    """Shared implementation for the ``release`` command and its ``publish`` alias."""
    from hyperi_ci.common import explicit_version
    from hyperi_ci.release import (
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
                f"Invalid --version '{version}' -- expected an explicit X.Y.Z.",
                err=True,
            )
            raise typer.Exit(1)
        bump = normalised

    if tag and bump:
        typer.echo(
            "Pass either a TAG (re-publish an existing tag) or --bump "
            "(release the current HEAD) -- not both.",
            err=True,
        )
        raise typer.Exit(1)

    if tag:
        # Re-publish an existing tag (idempotent retry of a partial publish).
        rc = dispatch_publish(tag, dry_run=dry_run)
        raise typer.Exit(rc)

    # No tag -> release/retry the current HEAD. The CI resolves the version,
    # creates the tag, and publishes -- no artificial commit, no local tag
    # push (issue #35). `bump` defaults to auto (semantic-release picks the
    # version from commits); --bump patch|minor forces a release; an explicit
    # X.Y.Z (from --version) tags HEAD at exactly that version.
    rc = dispatch_from_head(bump=bump or "auto", dry_run=dry_run)
    raise typer.Exit(rc)


@app.command()
def release(
    tag: Annotated[
        str | None,
        typer.Argument(help="Existing tag to re-release (e.g. v1.3.0 or 'latest')"),
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
            "directly -- use to step past a taken/orphaned tag (issue #37).",
        ),
    ] = None,
    list_tags: Annotated[
        bool,
        typer.Option("--list", help="List unreleased version tags"),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Show what would be dispatched"),
    ] = False,
) -> None:
    """Release or retry a release -- the CI creates the tag (issue #35).

    The primary path is ``hyperi-ci push --release`` (version-first single run,
    gated by the ``Release: true`` trailer). This command is the "I need to
    release/retry that" escape hatch -- no artificial ``fix:`` commit:

    - ``hyperi-ci release`` -- release the current ``main`` HEAD. Dispatches a
      from-head run; the CI resolves the version (semantic-release), tags HEAD,
      and publishes. Also finishes a release that died before the tag was cut.
    - ``hyperi-ci release --bump patch|minor`` -- force a release of HEAD even
      with no release-worthy commit since the last tag.
    - ``hyperi-ci release --version X.Y.Z`` -- release HEAD at an exact version.
      Tags HEAD directly, skipping a taken/orphaned tag the auto tagger would
      otherwise collide with (issue #37).
    - ``hyperi-ci release <tag>`` -- re-dispatch an existing tag (idempotent
      retry of a partial release; fills in registries that were missed).

    The CLI only triggers the workflow; the runner does the tagging and
    publishing, so it works under branch protection and from the Actions UI too.
    """
    _release_impl(
        tag=tag, list_tags=list_tags, dry_run=dry_run, bump=bump, version=version
    )


@app.command()
def publish(
    tag: Annotated[
        str | None,
        typer.Argument(help="Existing tag to re-release (e.g. v1.3.0 or 'latest')"),
    ] = None,
    bump: Annotated[
        str | None,
        typer.Option("--bump", help="Forced bump: patch | minor."),
    ] = None,
    version: Annotated[
        str | None,
        typer.Option("--version", help="Release HEAD at an exact X.Y.Z version."),
    ] = None,
    list_tags: Annotated[
        bool,
        typer.Option("--list", help="List unreleased version tags"),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Show what would be dispatched"),
    ] = False,
) -> None:
    """Run ``release`` under its deprecated name, with every option included."""
    from hyperi_ci.common import warn
    from hyperi_ci.vocabulary import REVERSAL_NOTE

    warn(f"`hyperi-ci publish` is deprecated; use `hyperi-ci release`. {REVERSAL_NOTE}")
    _release_impl(
        tag=tag, list_tags=list_tags, dry_run=dry_run, bump=bump, version=version
    )


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
    `bump` is patch/minor. Not a routine command -- operators use
    `hyperi-ci release` instead.
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


def main() -> int:
    """CLI entry point."""
    # Force UTF-8 with replacement on stdout/stderr so log lines containing
    # arbitrary bytes (gh CLI output, GH Actions log files, container build
    # output) never crash the CLI with UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")

    # A consumer installs the CLI with an unpinned `uvx hyperi-ci`, so the log
    # is the only record of which version actually ran. Without it, telling a
    # stale run from an ineffective fix means arithmetic on the PyPI upload
    # time.
    from hyperi_ci.common import info, is_ci

    if is_ci():
        info(f"hyperi-ci {__version__}")

    # Here rather than in the Typer callback so `--version` warns too -- its
    # eager callback exits before the callback body runs (#163).
    from hyperi_ci.staleness import warn_if_stale

    warn_if_stale()

    app()
    return 0


if __name__ == "__main__":
    sys.exit(main())
