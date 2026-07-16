# Project:   HyperI CI
# File:      src/hyperi_ci/tools.py
# Purpose:   External-tool presence checks with actionable, Rust-style guidance
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""External-tool presence checks with actionable, Rust-style guidance.

When hyperi-ci shells out to an external tool that is missing, don't just say
"not found" - name what hyperi-ci needs it for and give the exact way to
install it (command(s) + docs URL), then let the CALLER decide whether that is
fatal (a required tool in CI) or a skip (an optional advisory). Like a Rust
compiler error, the message helps you FIX the problem, it doesn't only report
it.

One SSoT for the per-tool install hints (``_REGISTRY``). An unknown tool still
gets a sane generic notice. Callers pick the emit level:

    exe = find_tool("alint")                    # optional -> info-skip
    exe = find_tool("gitleaks", recommended=True)  # nice-to-have -> warn-skip
    if not shutil.which("gh"):                   # required -> caller fails
        error(missing_tool_notice("gh")); return False
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass

from hyperi_ci.common import info, warn


@dataclass(frozen=True)
class ToolInfo:
    """How to install one external tool, and what hyperi-ci needs it for."""

    name: str
    purpose: str
    # One or more copy-pasteable install command lines (first = preferred).
    install: tuple[str, ...] = ()
    url: str = ""


# Known external tools hyperi-ci shells out to. Keep install lines current and
# copy-pasteable - they are what a developer will actually run.
_REGISTRY: dict[str, ToolInfo] = {
    "alint": ToolInfo(
        name="alint",
        purpose="profile-aware repo-hygiene advice (.gitignore / .editorconfig / lockfiles / ...)",
        install=(
            "brew tap asamarts/alint && brew install alint",
            "cargo install alint",
        ),
        url="https://github.com/asamarts/alint",
    ),
    "gitleaks": ToolInfo(
        name="gitleaks",
        purpose="secret scanning",
        install=("brew install gitleaks",),
        url="https://github.com/gitleaks/gitleaks#installing",
    ),
    "semgrep": ToolInfo(
        name="semgrep",
        purpose="SAST scanning",
        install=("uvx semgrep --help", "pipx install semgrep", "brew install semgrep"),
        url="https://semgrep.dev/docs/getting-started/",
    ),
    "osv-scanner": ToolInfo(
        name="osv-scanner",
        purpose="dependency vulnerability scanning (OSV)",
        install=("brew install osv-scanner",),
        url="https://google.github.io/osv-scanner/installation/",
    ),
    "gh": ToolInfo(
        name="gh",
        purpose="GitHub operations (releases, workflow dispatch, run status)",
        install=("brew install gh",),
        url="https://cli.github.com/",
    ),
    "helm": ToolInfo(
        name="helm",
        purpose="Helm chart packaging / topology stitching",
        install=("brew install helm",),
        url="https://helm.sh/docs/intro/install/",
    ),
    "aws": ToolInfo(
        name="aws",
        purpose="S3-compatible upload to Cloudflare R2",
        install=("brew install awscli",),
        url="https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html",
    ),
}


def tool_info(name: str) -> ToolInfo | None:
    """Return the registry entry for ``name`` (or None if unknown)."""
    return _REGISTRY.get(name)


def missing_tool_notice(
    name: str,
    *,
    purpose: str | None = None,
    install: tuple[str, ...] | list[str] | None = None,
    url: str | None = None,
) -> str:
    """Render the actionable 'here is how to fix it' notice for a missing tool.

    Uses the registry as the default and lets a caller override any field
    (a one-off tool, or a context-specific purpose). Multi-line, safe to pass
    straight to :func:`info` / :func:`warn` / :func:`error`.
    """
    reg = _REGISTRY.get(name)
    purpose = purpose if purpose is not None else (reg.purpose if reg else None)
    installs = tuple(install) if install is not None else (reg.install if reg else ())
    url = url if url is not None else (reg.url if reg else "")

    head = f"`{name}` is not installed"
    if purpose:
        head += f" - hyperi-ci needs it for {purpose}"
    lines = [head + "."]
    if installs:
        lines.append("  help: install it with one of:")
        lines.extend(f"    {cmd}" for cmd in installs)
    if url:
        lines.append(f"  docs: {url}")
    return "\n".join(lines)


def find_tool(
    name: str,
    *,
    recommended: bool = False,
    purpose: str | None = None,
    install: tuple[str, ...] | list[str] | None = None,
    url: str | None = None,
) -> str | None:
    """Return the resolved tool path, or None after emitting a helpful notice.

    Never raises and never exits - the CALLER owns fatality (e.g. a required
    tool blocks in CI). ``recommended=True`` emits at warn level (the tool adds
    real value), otherwise info (a nice-to-have). The notice is Rust-style: it
    tells you exactly how to install the thing.
    """
    exe = shutil.which(name)
    if exe:
        return exe
    notice = missing_tool_notice(name, purpose=purpose, install=install, url=url)
    (warn if recommended else info)(notice)
    return None
