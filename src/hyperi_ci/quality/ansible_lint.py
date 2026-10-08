# Project:   HyperI CI
# File:      src/hyperi_ci/quality/ansible_lint.py
# Purpose:   ansible-lint and yamllint over every ansible project (warn by default)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Ansible linting: galaxy install, one ansible-lint run, yamllint under a repo config.

Galaxy requirements install into scratch with every ansible path pointed there,
so an ``ansible.cfg`` naming an in-repo ``collections_path`` cannot pull the
install into the tree. A project under the repo's ``.ansible-lint``
``exclude_paths`` is not linted. Both tools run pinned through ``uvx``.
"""

import configparser
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from hyperi_ci.common import info, scratch_dir
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_tool_cmd, resolve_tool_mode
from hyperi_ci.quality import findings as fdg
from hyperi_ci.quality.targets import first_file, yaml_mapping
from hyperi_ci.tools import missing_tool
from hyperi_ci.versions import tool_version

_LINT_CONFIGS = (
    ".ansible-lint",
    ".ansible-lint.yml",
    ".ansible-lint.yaml",
    ".config/ansible-lint.yml",
    ".config/ansible-lint.yaml",
)
_YAMLLINT_CONFIGS = (".yamllint", ".yamllint.yaml", ".yamllint.yml")
_REQUIREMENTS = (
    "requirements.yml",
    "collections/requirements.yml",
    "roles/requirements.yml",
)

# yamllint `-f parsable`: `path:line:col: [level] message (rule)`.
_YAMLLINT_LINE = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+):\d+: \[(?P<level>\w+)\] (?P<msg>.*?)"
    r"(?: \((?P<rule>[\w-]+)\))?$"
)


def lintable_projects(projects: list[Path], root: Path) -> list[Path]:
    """Drop the projects at or under an ``exclude_paths`` entry of the repo config."""
    entries = yaml_mapping(first_file(root, _LINT_CONFIGS)).get("exclude_paths")
    if not isinstance(entries, list):
        entries = []
    excluded = [(root / str(e)).resolve() for e in entries if str(e).strip()]
    return [
        p
        for p in projects
        if not any(p.resolve() == e or e in p.resolve().parents for e in excluded)
    ]


def roles_path(project: Path) -> list[Path]:
    """Return the roles directories a project's ``ansible.cfg`` names, else its ``roles/``."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(project / "ansible.cfg", encoding="utf-8")
    except configparser.Error:
        parser = configparser.ConfigParser(interpolation=None)
    raw = parser.get("defaults", "roles_path", fallback="")
    if raw.strip():
        return [
            (project / Path(os.path.expanduser(p.strip()))).resolve()
            for p in raw.split(":")
            if p.strip()
        ]
    roles = project / "roles"
    return [roles.resolve()] if roles.is_dir() else []


def requirement_files(project: Path) -> list[Path]:
    """Return the galaxy requirements files a project carries, resolved."""
    return [(project / r).resolve() for r in _REQUIREMENTS if (project / r).is_file()]


def _scratch_env(home: Path) -> dict[str, str]:
    return {
        "ANSIBLE_COLLECTIONS_PATH": str(home / "collections"),
        "ANSIBLE_ROLES_PATH": str(home / "roles"),
        "ANSIBLE_HOME": str(home / "home"),
        "ANSIBLE_LOCAL_TEMP": str(home / "tmp"),
    }


def _pinned(command: str, package: str) -> list[str]:
    """Return the pinned uvx invocation of ``command`` from PyPI ``package``."""
    spec = f"{package}=={tool_version(package)}"
    return resolve_tool_cmd([command], via="uvx", spec=spec)


def _available(cmd: list[str]) -> bool:
    return shutil.which(cmd[0]) is not None


def _failer(
    tool: str, path: str, rule: str | None = None
) -> Callable[[str, str], fdg.Finding]:
    """Return a ``run_tool`` callback naming ``rule`` (default ``<prefix>/<kind>``)."""
    prefix = "yamllint" if tool == "yamllint" else "ansible"
    return lambda kind, why: fdg.Finding(
        tool, path, None, "error", rule or f"{prefix}/{kind}", why
    )


def _last_line(result: subprocess.CompletedProcess[str]) -> str:
    detail = (result.stderr or result.stdout or "").strip().splitlines()
    return detail[-1] if detail else f"exited {result.returncode}"


def _galaxy(
    projects: list[Path], root: Path, *, home: Path, timeout: float | None
) -> list[fdg.Finding]:
    """Install every project's galaxy requirements under ``home``; return failures."""
    reqs: list[Path] = []
    for project in projects:
        reqs += requirement_files(project)
    out: list[fdg.Finding] = []
    for req in reqs:
        where = fdg.relpath(req, root)
        info(f"  ansible-galaxy: installing {where}")
        fail = _failer("ansible-galaxy", where, "ansible/galaxy")
        result = fdg.run_tool(
            [*_pinned("ansible-galaxy", "ansible-lint"), "install", "-r", str(req)],
            fail,
            timeout=timeout,
            cwd=home,
            env=_scratch_env(home),
            stdin_text="",
        )
        if isinstance(result, fdg.Finding):
            out.append(result)
        elif result.returncode != 0:
            out.append(fail("", _last_line(result)))
    return out


def _ansible_lint(
    projects: list[Path],
    root: Path,
    *,
    home: Path,
    timeout: float | None,
    memory_limit_bytes: int | None,
) -> list[fdg.Finding]:
    """Run ansible-lint once over every project; return its findings."""
    sarif = home / "ansible-lint.sarif"
    base = root.resolve()
    roles = [str(home / "roles"), *(str(r) for p in projects for r in roles_path(p))]
    cmd = [
        *_pinned("ansible-lint", "ansible-lint"),
        "--offline",
        "--nocolor",
        "-q",
        "--sarif-file",
        str(sarif),
        "--project-dir",
        str(base),
        *[fdg.relpath(p, base) for p in projects],
    ]
    info(f"  ansible-lint: linting {len(projects)} project(s)...")
    result = fdg.run_tool(
        cmd,
        _failer("ansible-lint", "."),
        timeout=timeout,
        cwd=base,
        env={**_scratch_env(home), "ANSIBLE_ROLES_PATH": os.pathsep.join(roles)},
        stdin_text="",
        memory_limit_bytes=memory_limit_bytes,
    )
    if isinstance(result, fdg.Finding):
        return [result]
    text = sarif.read_text(encoding="utf-8") if sarif.is_file() else ""
    found = fdg.parse_sarif(text, "ansible-lint")
    # 0 is clean and 2 is "violations found"; anything else is the tool failing.
    if result.returncode not in (0, 2) or (result.returncode == 2 and not found):
        message = f"ansible-lint failed: {_last_line(result)}"
        found.append(_failer("ansible-lint", ".")("tool-error", message))
    return found


def parse_yamllint(stdout: str) -> list[fdg.Finding]:
    """Parse ``yamllint -f parsable`` output into findings."""
    out: list[fdg.Finding] = []
    for line in stdout.splitlines():
        match = _YAMLLINT_LINE.match(line.strip())
        if match is None:
            continue
        out.append(
            fdg.Finding(
                tool="yamllint",
                path=match.group("path"),
                line=int(match.group("line")),
                level=fdg.normalise_level(match.group("level")),
                rule=match.group("rule") or "yamllint",
                message=match.group("msg"),
            )
        )
    return out


def _yamllint(
    root: Path,
    config_file: Path,
    *,
    timeout: float | None,
    memory_limit_bytes: int | None,
) -> list[fdg.Finding]:
    """Run yamllint over ``root`` under the repo's own config; return its findings."""
    cmd = [
        *_pinned("yamllint", "yamllint"),
        "-f",
        "parsable",
        "-c",
        str(config_file.resolve()),
        ".",
    ]
    info(f"  yamllint: linting under {config_file.name}...")
    fail = _failer("yamllint", ".")
    result = fdg.run_tool(
        cmd,
        fail,
        timeout=timeout,
        cwd=root.resolve(),
        memory_limit_bytes=memory_limit_bytes,
    )
    if isinstance(result, fdg.Finding):
        return [result]
    found = parse_yamllint(result.stdout)
    if result.returncode not in (0, 1):
        found.append(fail("tool-error", _last_line(result)))
    return found


def run(
    projects: list[Path],
    config: CIConfig,
    *,
    root: Path,
    scratch: Path,
    sarif_path: str | Path | None = None,
    timeout: float | None = None,
    memory_limit_bytes: int | None = None,
) -> int:
    """Install requirements, then run ansible-lint and yamllint; return the exit code."""
    mode = resolve_tool_mode("ansible_lint", config, default="warn")
    if mode == "disabled":
        info("  ansible: disabled")
        return 0
    kept = lintable_projects(projects, root)
    for dropped in sorted(set(projects) - set(kept)):
        info(f"  ansible: {dropped} is under an exclude_paths entry - not linted")
    if not kept:
        info("  ansible: no ansible project to lint - skipping")
        return 0
    if not _available(_pinned("ansible-lint", "ansible-lint")):
        return missing_tool("ansible-lint", mode)

    with scratch_dir(scratch / "ansible") as home:
        found = _galaxy(kept, root, home=home, timeout=timeout)
        found += _ansible_lint(
            kept,
            root,
            home=home,
            timeout=timeout,
            memory_limit_bytes=memory_limit_bytes,
        )
    yamllint_config = first_file(root, _YAMLLINT_CONFIGS)
    if yamllint_config is not None:
        found += _yamllint(
            root,
            yamllint_config,
            timeout=timeout,
            memory_limit_bytes=memory_limit_bytes,
        )

    for tool in ("ansible-galaxy", "ansible-lint", "yamllint"):
        mine = [f for f in found if f.tool == tool]
        if mine:
            fdg.report(tool, mine, mode, sarif_path=sarif_path)
    errors = sum(f.level == "error" for f in found)
    return fdg.verdict(
        "ansible", errors, mode, f"{len(kept)} project(s), no error-level finding"
    )
