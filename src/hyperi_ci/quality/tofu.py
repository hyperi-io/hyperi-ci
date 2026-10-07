# Project:   HyperI CI
# File:      src/hyperi_ci/quality/tofu.py
# Purpose:   OpenTofu fmt / init / validate over every root module (GATE)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""OpenTofu gate: ``fmt -check`` over every module dir, init and validate per root.

A root is a module dir no other one calls by a local ``source``; a called
module needs its caller's providers and inputs to validate. init writes
``.terraform`` and the lock file beside the module, so each root is copied to
scratch, with the modules and files it reaches, and checked there. Never
``plan`` or ``apply``: they need credentials and state a lint job must not hold.
"""

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from hyperi_ci.common import info, scratch_dir, stage_tree
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_cross_tool_mode
from hyperi_ci.native_tools import ci_binary
from hyperi_ci.quality import findings as fdg
from hyperi_ci.tools import missing_tool
from hyperi_ci.upgrade import CACHE_DIR

_LOCAL_SOURCE = re.compile(
    r"""^\s*source\s*=\s*"(\.{1,2}/[^"]*)"\s*(?:(?:#|//).*)?$""", re.MULTILINE
)

# A "../..." path literal, which validate reads through file() / filemd5().
_PARENT_LITERAL = re.compile(r'"(?:\$\{path\.(?:module|root)\}/)?(\.\./[^"$]*)"')


def _module_text(directory: Path) -> str:
    texts: list[str] = []
    for pattern in ("*.tf", "*.tofu"):
        for tf in sorted(directory.glob(pattern)):
            try:
                texts.append(tf.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
    return "\n".join(texts)


def local_sources(directory: Path) -> list[Path]:
    """Return the module directories ``directory`` calls by a local ``source``."""
    text = _module_text(directory)
    return [(directory / m).resolve() for m in _LOCAL_SOURCE.findall(text)]


def _reaches(directory: Path) -> list[Path]:
    """Return the local modules and the existing ``../`` paths a module reaches."""
    parents = [
        (directory / m).resolve()
        for m in _PARENT_LITERAL.findall(_module_text(directory))
    ]
    return local_sources(directory) + [p for p in parents if p.exists()]


def roots(dirs: list[Path]) -> list[Path]:
    """Return the module directories in ``dirs`` that no other one calls locally."""
    called = set().union(*map(local_sources, dirs))
    return [d for d in dirs if d.resolve() not in called]


def _ignore_hidden(_directory: str, names: list[str]) -> set[str]:
    """Skip ``.terraform`` and other hidden entries, but keep the lock file."""
    return {n for n in names if n.startswith(".") and n != ".terraform.lock.hcl"}


def _finding(
    path: str, rule: str, message: str, *, line: int | None = None, level: str = "error"
) -> fdg.Finding:
    return fdg.Finding("tofu", path, line, level, rule, message)


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _step(
    exe: str, args: list[str], where: str, **kwargs: Any
) -> subprocess.CompletedProcess[str] | fdg.Finding:
    """Run ``tofu <args>``; return its result, or a finding when it never finished."""
    return fdg.run_tool(
        [exe, *args],
        lambda kind, why: _finding(where, f"tofu/{kind}", f"tofu {args[0]}: {why}"),
        **kwargs,
    )


def _fmt(exe: str, dirs: list[Path], timeout: float | None) -> list[fdg.Finding]:
    """Return one finding per file ``tofu fmt -check`` would rewrite."""
    args = ["fmt", "-check", "-list=true", "-no-color", *[str(d) for d in dirs]]
    result = _step(exe, args, "", timeout=timeout)
    if isinstance(result, fdg.Finding):
        return [result]
    files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0 and not files:
        detail = _first_line(result.stderr) or f"exited {result.returncode}"
        return [_finding("", "tofu/fmt-error", f"tofu fmt: {detail}")]
    return [
        _finding(f, "tofu/fmt", "not in canonical format - run `tofu fmt`")
        for f in files
    ]


def _diagnostics(stdout: str, root: Path) -> list[fdg.Finding]:
    """Parse ``tofu validate -json`` diagnostics into findings against ``root``."""
    try:
        doc = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return []
    out: list[fdg.Finding] = []
    for diag in doc.get("diagnostics", []) if isinstance(doc, dict) else []:
        if not isinstance(diag, dict):
            continue
        rng = diag.get("range") or {}
        filename = rng.get("filename")
        path = fdg.relpath(root / filename if filename else root)
        line = (rng.get("start") or {}).get("line")
        summary = str(diag.get("summary", "")).strip()
        detail = _first_line(str(diag.get("detail", "")))
        message = f"{summary}: {detail}" if detail else summary
        level = "error" if diag.get("severity") == "error" else "warning"
        out.append(_finding(path, "tofu/validate", message, line=line, level=level))
    return out


def _first_error(stderr: str) -> str:
    """Return the ``Error:`` line of tofu's human output, else its first line."""
    errors = [
        line.strip()
        for line in stderr.splitlines()
        if line.strip().startswith("Error:")
    ]
    return errors[0] if errors else _first_line(stderr)


def _validate_root(
    exe: str, root: Path, stage: Path, env: dict[str, str], timeout: float | None
) -> list[fdg.Finding]:
    """Init and validate a scratch copy of one root module; return its findings."""
    where = fdg.relpath(root)
    with scratch_dir(stage):
        try:
            staged = stage_tree(root, Path.cwd(), stage, _reaches, _ignore_hidden)
        except OSError as exc:
            return [_finding(where, "tofu/stage", f"could not copy to scratch ({exc})")]
        run = {
            "cwd": staged,
            "env": {**env, "TF_DATA_DIR": str(staged / ".terraform")},
            "timeout": timeout,
        }
        init = _step(
            exe, ["init", "-backend=false", "-input=false", "-no-color"], where, **run
        )
        if isinstance(init, fdg.Finding):
            return [init]
        if init.returncode != 0:
            detail = _first_error(init.stderr) or f"exited {init.returncode}"
            return [_finding(where, "tofu/init", f"tofu init: {detail}")]
        result = _step(exe, ["validate", "-json", "-no-color"], where, **run)
        if isinstance(result, fdg.Finding):
            return [result]
        found = _diagnostics(result.stdout, root)
        if result.returncode != 0 and not any(f.level == "error" for f in found):
            detail = _first_line(result.stderr) or f"exited {result.returncode}"
            found.append(_finding(where, "tofu/validate", detail))
        return found


def plugin_cache() -> Path:
    """Return the provider cache dir: ``TF_PLUGIN_CACHE_DIR`` or the hyperi-ci cache."""
    configured = os.environ.get("TF_PLUGIN_CACHE_DIR")
    path = Path(configured) if configured else CACHE_DIR / "tofu-plugins"
    path.mkdir(parents=True, exist_ok=True)
    return path


def run(
    dirs: list[Path],
    config: CIConfig,
    *,
    scratch: Path,
    sarif_path: str | Path | None = None,
    timeout: float | None = None,
) -> int:
    """Format-check every module dir and validate every root; return the exit code.

    Scratch copies keep each path's place relative to the cwd.
    """
    mode = resolve_cross_tool_mode(config, "tofu", "blocking")
    if mode == "disabled":
        info("  tofu: disabled")
        return 0
    if not dirs:
        info("  tofu: no .tf files - skipping")
        return 0
    exe = ci_binary("tofu")
    if exe is None:
        return missing_tool("tofu", mode)

    root_dirs = roots(dirs)
    info(f"  tofu: {len(dirs)} module dir(s), {len(root_dirs)} root(s) to validate")
    found = _fmt(exe, dirs, timeout)
    env = {"TF_PLUGIN_CACHE_DIR": str(plugin_cache()), "TF_IN_AUTOMATION": "1"}
    for index, root in enumerate(root_dirs):
        found += _validate_root(exe, root, scratch / f"tofu-{index}", env, timeout)
    fdg.report("tofu", found, mode, sarif_path=sarif_path)
    errors = sum(f.level == "error" for f in found)
    ok = f"{len(dirs)} module dir(s) formatted, {len(root_dirs)} valid"
    return fdg.verdict("tofu", errors, mode, ok)
