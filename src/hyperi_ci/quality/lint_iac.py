# Project:   HyperI CI
# File:      src/hyperi_ci/quality/lint_iac.py
# Purpose:   Orchestrate every IaC linting dimension behind `hyperi-ci lint-iac`
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Orchestrate IaC linting: charts, manifests, kustomize, OpenTofu, ansible, compose.

Each dimension switches on by what the tree holds and runs in its own log
group, one at a time, with ``iac.timeout_seconds`` on every external call. A
dimension that crashes fails the run without hiding the rest. Nothing plans,
applies, installs a chart or starts a cluster, and nothing is written into the
tree: every build, install and regeneration runs on a copy in scratch.
"""

import contextlib
import itertools
import shlex
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from hyperi_ci.common import (
    error,
    get_exclude_dirs,
    group,
    info,
    run_cmd,
    scratch_dir,
    warn,
)
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_tool_mode
from hyperi_ci.native_tools import ci_binary
from hyperi_ci.quality import (
    ansible_lint,
    checkov,
    compose_config,
    compose_pins,
    hadolint,
    kube_linter,
    kubeconform,
    render,
    tofu,
)
from hyperi_ci.quality import findings as fdg
from hyperi_ci.quality.targets import (
    discover_ansible_projects,
    discover_compose_files,
    discover_dockerfiles,
    discover_helm_charts,
    discover_kustomizations,
    discover_manifests,
    discover_tofu_dirs,
    git_ignored_dirs,
)
from hyperi_ci.tools import missing_tool

DIMENSIONS = (
    "dockerfile",
    "compose",
    "helm",
    "kustomize",
    "manifests",
    "kube-linter",
    "checkov",
    "tofu",
    "ansible",
    "generated",
)

# What the deprecated `lint-manifests` and `lint-compose` verbs ran.
MANIFEST_DIMENSIONS = ("helm", "kustomize", "manifests", "kube-linter", "checkov")
COMPOSE_DIMENSIONS = ("compose",)

_DEFAULT_TIMEOUT = 600
_DEFAULT_MEMORY_MB = 4096


@dataclass(slots=True)
class _Context:
    """What every dimension reads, plus the renders one hands to another."""

    root: Path
    config: CIConfig
    scratch: Path
    sarif_path: str | Path | None
    timeout: float
    memory_limit_bytes: int
    exclude: list[str]
    kustomize_outputs: list[Path] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Outcome:
    """How one dimension went.

    Attributes:
        dimension: The dimension's name.
        targets: How many targets it found (charts, roots, files, ...).
        findings: Findings it surfaced.
        rc: 0 passed or advisory, 1 a gate failed.
        seconds: Elapsed time.

    """

    dimension: str
    targets: int
    findings: int
    rc: int
    seconds: float


def _positive(config: CIConfig, key: str, default: int) -> int:
    """Return a positive integer setting, warning and defaulting on anything else."""
    raw = config.get(key, default)
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return raw
    warn(f"{key}: expected a positive whole number, got {raw!r} - using {default}")
    return default


def _render_gate(
    tool: str, renders: list[render.Rendered], mode: str, ctx: _Context
) -> int:
    """Surface failed renders at ``mode`` and unstable ones at ``quality.render_stable``.

    Returns 1 when either gate fails.
    """
    stable_mode = resolve_tool_mode("render_stable", ctx.config)
    failed = [r.finding for r in renders if r.finding is not None and not r.unstable]
    unstable = [r.finding for r in renders if r.finding is not None and r.unstable]
    if stable_mode == "disabled":
        unstable = []
    surfaced = fdg.at_mode(failed, mode) + fdg.at_mode(unstable, stable_mode)
    if surfaced:
        fdg.surface(tool, surfaced, sarif_path=ctx.sarif_path)
    produced = sum(1 for r in renders if r.output is not None)
    info(f"  {tool}: {produced}/{len(renders)} render(s) produced")
    rc = 0
    for problems, problem_mode, what in (
        (failed, mode, "failed"),
        (unstable, stable_mode, "differ between two runs"),
    ):
        if not problems:
            continue
        message = f"  {tool}: {len(problems)} render(s) {what}"
        if problem_mode == "blocking":
            error(message)
            rc = 1
        else:
            warn(message)
    return rc


def _rendered(
    ctx: _Context,
    tool: str,
    targets: list[Path],
    render_one: Callable[[str, Path], list[render.Rendered]],
) -> tuple[int, int, list[Path]]:
    """Render ``targets`` with ``tool``, gate the renders and schema-check the output.

    Returns the target count, the exit code and the rendered manifests.
    """
    if not targets:
        info(f"  {tool}: nothing to render - skipping")
        return 0, 0, []
    mode = resolve_tool_mode("kubeconform", ctx.config)
    if mode == "disabled":
        info(f"  {tool}: kubeconform is disabled, so nothing is rendered")
        return len(targets), 0, []
    exe = ci_binary(tool)
    if exe is None:
        return len(targets), missing_tool(tool, mode), []
    renders = list(itertools.chain.from_iterable(render_one(exe, t) for t in targets))
    render_rc = _render_gate(tool, renders, mode, ctx)
    outputs = [r.output for r in renders if r.output is not None]
    schema_rc = kubeconform.run(
        outputs, ctx.config, sarif_path=ctx.sarif_path, timeout=ctx.timeout
    )
    return len(targets), render_rc or schema_rc, outputs


def _helm(ctx: _Context) -> tuple[int, int]:
    extra = render.helm_value_args(ctx.config, ctx.root)
    count, rc, _ = _rendered(
        ctx,
        "helm",
        discover_helm_charts(ctx.root, exclude_dirs=ctx.exclude),
        lambda exe, chart: render.render_chart(
            exe,
            chart,
            root=ctx.root,
            extra_args=extra,
            out_dir=ctx.scratch / "helm",
            stage_dir=ctx.scratch / "stage",
            timeout=ctx.timeout,
        ),
    )
    return count, rc


def _kustomize(ctx: _Context) -> tuple[int, int]:
    count, rc, ctx.kustomize_outputs = _rendered(
        ctx,
        "kustomize",
        discover_kustomizations(ctx.root, exclude_dirs=ctx.exclude),
        lambda exe, directory: [
            render.render_kustomization(
                exe,
                directory,
                root=ctx.root,
                out_dir=ctx.scratch / "kustomize",
                stage_dir=ctx.scratch / "stage",
                timeout=ctx.timeout,
            )
        ],
    )
    return count, rc


def _manifests(ctx: _Context) -> tuple[int, int]:
    manifests = discover_manifests(ctx.root, exclude_dirs=ctx.exclude)
    if not manifests:
        info("  manifests: no plain k8s manifest - skipping")
        return 0, 0
    rc = kubeconform.run(
        manifests, ctx.config, sarif_path=ctx.sarif_path, timeout=ctx.timeout
    )
    return len(manifests), rc


def _kube_linter(ctx: _Context) -> tuple[int, int]:
    targets = [
        *discover_helm_charts(ctx.root, exclude_dirs=ctx.exclude),
        *discover_manifests(ctx.root, exclude_dirs=ctx.exclude),
        *ctx.kustomize_outputs,
    ]
    kube_linter.run(
        targets,
        ctx.config,
        root=ctx.root,
        scratch=ctx.scratch,
        sarif_path=ctx.sarif_path,
        timeout=ctx.timeout,
    )
    return len(targets), 0


def _checkov(ctx: _Context) -> tuple[int, int]:
    rc = checkov.run(
        ctx.root,
        ctx.config,
        sarif_path=ctx.sarif_path,
        timeout=ctx.timeout,
        memory_limit_bytes=ctx.memory_limit_bytes,
    )
    return 1, rc


def _tofu(ctx: _Context) -> tuple[int, int]:
    dirs = discover_tofu_dirs(ctx.root, exclude_dirs=ctx.exclude)
    rc = tofu.run(
        dirs,
        ctx.config,
        scratch=ctx.scratch,
        sarif_path=ctx.sarif_path,
        timeout=ctx.timeout,
    )
    return len(dirs), rc


def _ansible(ctx: _Context) -> tuple[int, int]:
    projects = discover_ansible_projects(ctx.root, exclude_dirs=ctx.exclude)
    rc = ansible_lint.run(
        projects,
        ctx.config,
        root=ctx.root,
        scratch=ctx.scratch,
        sarif_path=ctx.sarif_path,
        timeout=ctx.timeout,
        memory_limit_bytes=ctx.memory_limit_bytes,
    )
    return len(projects), rc


def _compose(ctx: _Context) -> tuple[int, int]:
    files = discover_compose_files(ctx.root, exclude_dirs=ctx.exclude)
    if not files:
        info("  compose: no compose file - skipping")
        return 0, 0
    config_rc = compose_config.run(
        files, ctx.config, sarif_path=ctx.sarif_path, timeout=ctx.timeout
    )
    pins_rc = compose_pins.run(files, ctx.config, sarif_path=ctx.sarif_path)
    return len(files), config_rc or pins_rc


def _dockerfile(ctx: _Context) -> tuple[int, int]:
    count = len(discover_dockerfiles(ctx.root, exclude_dirs=ctx.exclude))
    rc = hadolint.run(
        ctx.config,
        sarif_path=ctx.sarif_path,
        root=ctx.root,
        timeout=ctx.timeout,
        exclude_dirs=ctx.exclude,
    )
    return count, rc


@dataclass(frozen=True, slots=True)
class GeneratedEntry:
    """One ``iac.generated`` entry: a command and the paths it must not change.

    Attributes:
        paths: Repo-relative paths the command writes.
        command: The command, already split into arguments.

    """

    paths: tuple[str, ...]
    command: tuple[str, ...]


def generated_entries(raw: object) -> tuple[list[GeneratedEntry], list[str]]:
    """Parse ``iac.generated`` into entries, and the problems that stop one.

    A string ``command`` is split as a shell would split it, never run in one.
    """
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], [f"iac.generated must be a list of entries, got {raw!r}"]
    entries: list[GeneratedEntry] = []
    problems: list[str] = []
    for index, item in enumerate(raw):
        where = f"iac.generated[{index}]"
        if not isinstance(item, dict):
            problems.append(f"{where} must be a mapping with paths and command")
            continue
        paths = item.get("paths")
        command = item.get("command")
        if (
            not isinstance(paths, list)
            or not paths
            or not all(isinstance(p, str) and p.strip() for p in paths)
        ):
            problems.append(f"{where}.paths must be a non-empty list of paths")
            continue
        if isinstance(command, str) and command.strip():
            argv = shlex.split(command)
        elif (
            isinstance(command, list)
            and command
            and all(isinstance(a, str) for a in command)
        ):
            argv = list(command)
        else:
            problems.append(f"{where}.command must be a string or a list of strings")
            continue
        entries.append(GeneratedEntry(tuple(paths), tuple(argv)))
    return entries, problems


def _copy_checkout(root: Path, dest: Path, timeout: float) -> None:
    """Copy the files git tracks or would track under ``root`` into ``dest``."""
    listing = run_cmd(
        [
            "git",
            "-C",
            str(root),
            *"ls-files -z --cached --others --exclude-standard".split(),
        ],
        check=True,
        capture=True,
        timeout=timeout,
    ).stdout
    for rel in filter(None, listing.split("\0")):
        src = root / rel
        if src.is_file() or src.is_symlink():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / rel, follow_symlinks=False)


def _contents(base: Path, paths: Sequence[str]) -> dict[str, bytes]:
    """Return every file under ``paths`` (relative to ``base``) and its bytes."""
    out: dict[str, bytes] = {}
    for entry in paths:
        start = base / entry
        files = [start] if start.is_file() else sorted(start.rglob("*"))
        for f in files:
            if f.is_file():
                out[f.relative_to(base).as_posix()] = f.read_bytes()
    return out


def _regenerate(
    root: Path, entry: GeneratedEntry, scratch: Path, timeout: float
) -> fdg.Finding | None:
    """Run one entry in a scratch copy; return a finding when its paths change."""
    label = " ".join(entry.command)

    def finding(where: str, message: str) -> fdg.Finding:
        return fdg.Finding("generated", where, None, "error", "iac/generated", message)

    try:
        _copy_checkout(root, scratch, timeout)
        result = run_cmd(
            list(entry.command),
            check=False,
            capture=True,
            cwd=scratch,
            timeout=timeout,
            own_group=True,
        )
    except subprocess.TimeoutExpired:
        return finding(label, f"no result within {timeout}s")
    except (OSError, subprocess.CalledProcessError) as exc:
        return finding(label, f"could not run ({exc})")
    if result.returncode != 0:
        return finding(label, f"exited {result.returncode}")
    committed = _contents(root, entry.paths)
    regenerated = _contents(scratch, entry.paths)
    changed = sorted(
        k
        for k in committed.keys() | regenerated.keys()
        if committed.get(k) != regenerated.get(k)
    )
    if not changed:
        return None
    shown = ", ".join(changed[:5]) + (" ..." if len(changed) > 5 else "")
    return finding(
        ", ".join(entry.paths),
        f"`{label}` regenerates different content ({shown}) - commit what it generates",
    )


def _generated(ctx: _Context) -> tuple[int, int]:
    entries, problems = generated_entries(ctx.config.get("iac.generated"))
    if not entries and not problems:
        info("  generated: no iac.generated entry - skipping")
        return 0, 0
    mode = resolve_tool_mode("iac_generated", ctx.config)
    if mode == "disabled":
        info("  generated: disabled")
        return len(entries), 0
    for problem in problems:
        error(f"  generated: {problem}")
    found: list[fdg.Finding] = []
    for index, entry in enumerate(entries):
        with scratch_dir(ctx.scratch / f"generated-{index}") as copy:
            finding = _regenerate(ctx.root.resolve(), entry, copy, ctx.timeout)
        if finding is not None:
            found.append(finding)
    fdg.surface("generated", fdg.at_mode(found, mode), sarif_path=ctx.sarif_path)
    if problems:
        return len(entries), 1
    ok = f"{len(entries)} entry(ies) up to date"
    return len(entries), fdg.verdict("generated", len(found), mode, ok)


_RUNNERS: dict[str, Callable[[_Context], tuple[int, int]]] = {
    "dockerfile": _dockerfile,
    "compose": _compose,
    "helm": _helm,
    "kustomize": _kustomize,
    "manifests": _manifests,
    "kube-linter": _kube_linter,
    "checkov": _checkov,
    "tofu": _tofu,
    "ansible": _ansible,
    "generated": _generated,
}


def _report(outcomes: list[Outcome]) -> None:
    info("lint-iac summary:")
    for o in outcomes:
        verdict = "FAIL" if o.rc else "ok"
        info(
            f"  {o.dimension:<12} targets={o.targets:<4} findings={o.findings:<5} "
            f"{o.seconds:6.1f}s {verdict}"
        )


def run(
    root: Path | str,
    config: CIConfig,
    *,
    sarif_path: str | Path | None = None,
    dimensions: Sequence[str] = DIMENSIONS,
) -> int:
    """Run each dimension in ``dimensions`` over ``root``, in order.

    Returns 1 when any dimension's gate failed, else 0.

    Raises:
        ValueError: ``dimensions`` names one that does not exist.

    """
    unknown = [d for d in dimensions if d not in _RUNNERS]
    if unknown:
        raise ValueError(f"unknown lint-iac dimension(s): {', '.join(unknown)}")
    root = Path(root).resolve()
    if sarif_path is not None:
        sarif_path = Path(sarif_path).resolve()
    timeout = _positive(config, "iac.timeout_seconds", _DEFAULT_TIMEOUT)
    memory_mb = _positive(config, "iac.memory_limit_mb", _DEFAULT_MEMORY_MB)
    outcomes: list[Outcome] = []
    # Discovery runs from "." inside the root: the exclude list reads
    # .gitmodules from the cwd, and annotations attach to repo-relative paths.
    with (
        contextlib.chdir(root),
        tempfile.TemporaryDirectory(prefix="hyperi-lint-iac-") as tmp,
    ):
        ctx = _Context(
            root=Path("."),
            config=config,
            scratch=Path(tmp),
            sarif_path=sarif_path,
            timeout=float(timeout),
            memory_limit_bytes=memory_mb * 1024 * 1024,
            exclude=[*get_exclude_dirs(config._raw), *git_ignored_dirs(root)],
        )
        for name in dimensions:
            before = fdg.surfaced_count()
            started = time.monotonic()
            with group(f"lint-iac: {name}"):
                try:
                    targets, rc = _RUNNERS[name](ctx)
                except Exception as exc:  # noqa: BLE001 - one dimension's crash must not hide the rest
                    error(f"  {name}: failed with {type(exc).__name__}: {exc}")
                    targets, rc = 0, 1
            outcomes.append(
                Outcome(
                    dimension=name,
                    targets=targets,
                    findings=fdg.surfaced_count() - before,
                    rc=rc,
                    seconds=time.monotonic() - started,
                )
            )
    _report(outcomes)
    return 1 if any(o.rc for o in outcomes) else 0
