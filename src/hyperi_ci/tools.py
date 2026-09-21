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
        # Prebuilt binaries first. `cargo install` compiles alint from source
        # (minutes); every option above it fetches a release artefact (seconds).
        # upstream publishes musl-static + darwin tarballs with SHA256SUMS.
        install=(
            "brew install asamarts/alint/alint",
            "download a release binary (musl-static/darwin + SHA256SUMS): https://github.com/asamarts/alint/releases/latest",
            "cargo binstall alint",
            "cargo install alint  # from source - slowest, last resort",
        ),
        url="https://github.com/asamarts/alint",
    ),
    "gitleaks": ToolInfo(
        name="gitleaks",
        purpose="secret scanning",
        # brew is macOS-only, so give Linux a real answer too: upstream ships
        # prebuilt binaries. Never `go install ...@latest` - unpinned and built
        # from source.
        install=(
            "brew install gitleaks",
            "download a release binary: https://github.com/gitleaks/gitleaks/releases/latest",
        ),
        url="https://github.com/gitleaks/gitleaks#installing",
    ),
    "semgrep": ToolInfo(
        name="semgrep",
        purpose="SAST scanning",
        # astral first: uv is already a hard dependency of every hyperi-ci
        # project, so `uvx` / `uv tool` needs nothing new installed. pipx would
        # be a second, redundant Python tool manager.
        install=(
            "uvx semgrep --help",
            "uv tool install semgrep",
            "brew install semgrep",
        ),
        url="https://semgrep.dev/docs/getting-started/",
    ),
    "hadolint": ToolInfo(
        name="hadolint",
        purpose="Dockerfile linting gate (lints the shell inside RUN via ShellCheck)",
        # Single static binary. brew on macOS; a release binary elsewhere. On
        # Linux CI hyperi-ci auto-installs the pinned release, so this notice is
        # for local dev.
        install=(
            "brew install hadolint",
            "download a release binary: https://github.com/hadolint/hadolint/releases/latest",
        ),
        url="https://github.com/hadolint/hadolint#install",
    ),
    "droast": ToolInfo(
        name="droast",
        purpose="Dockerfile advisory - cache ordering / .dockerignore / npm ci (DF070/DF033/DF031)",
        # Advisory-only, never auto-installed. cargo binstall fetches a prebuilt
        # binary; `cargo install` builds from source (slower, last resort).
        install=(
            "cargo binstall dockerfile-roast",
            "cargo install dockerfile-roast  # from source - slowest",
            "download a release binary: https://github.com/immanuwell/dockerfile-roast/releases/latest",
        ),
        url="https://github.com/immanuwell/dockerfile-roast",
    ),
    "kubeconform": ToolInfo(
        name="kubeconform",
        purpose="Kubernetes manifest schema validation gate",
        # Single static Go binary. On Linux CI hyperi-ci auto-installs the
        # pinned release; this notice is for local dev.
        install=(
            "brew install kubeconform",
            "go install github.com/yannh/kubeconform/cmd/kubeconform@latest",
        ),
        url="https://github.com/yannh/kubeconform#installation",
    ),
    "kube-linter": ToolInfo(
        name="kube-linter",
        purpose="Kubernetes best-practice advisory (production-readiness / security)",
        install=(
            "brew install kube-linter",
            "go install golang.stackrox.io/kube-linter/cmd/kube-linter@latest",
        ),
        url="https://docs.kubelinter.io/#/configuring-kubelinter",
    ),
    "checkov": ToolInfo(
        name="checkov",
        purpose="IaC security scanning (k8s / helm / kustomize / terraform / opentofu)",
        # astral first: uv is already a hard dependency, so `uvx` needs nothing
        # new (same rationale as semgrep).
        install=(
            "uvx checkov --version",
            "uv tool install checkov",
            "pip install checkov",
        ),
        url="https://www.checkov.io/2.Basics/Installing%20Checkov.html",
    ),
    "lychee": ToolInfo(
        name="lychee",
        purpose="repo-internal doc link + anchor checking (offline, no network)",
        # Prebuilt binaries first: `cargo install` compiles it from source
        # (minutes), everything above fetches a release artefact (seconds).
        install=(
            "brew install lychee",
            "cargo binstall lychee",
            "download a release binary: https://github.com/lycheeverse/lychee/releases/latest",
            "cargo install lychee  # from source - slowest, last resort",
        ),
        url="https://github.com/lycheeverse/lychee#installation",
    ),
    "markdownlint-cli2": ToolInfo(
        name="markdownlint-cli2",
        purpose="mechanical markdown syntax linting",
        install=(
            "npm install -g markdownlint-cli2",
            "brew install markdownlint-cli2",
        ),
        url="https://github.com/DavidAnson/markdownlint-cli2#install",
    ),
    "mermaid": ToolInfo(
        name="mermaid",
        purpose="mermaid diagram parse checking (the grammar, not a render)",
        # Node packages, resolved from the repo's own node_modules. linkedom
        # supplies the browser globals mermaid's bundle reaches for - without it
        # a VALID flowchart throws, so both are needed or neither works.
        install=(
            "npm install --no-save mermaid linkedom",
            "add mermaid + linkedom to the project's devDependencies",
        ),
        url="https://mermaid.js.org/config/usage.html",
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
    "docker compose": ToolInfo(
        name="docker compose",
        purpose="compose file resolution (`docker compose config`) - no daemon needed",
        # The v2 compose plugin ships inside Docker Desktop and inside the
        # `docker-compose-plugin` package; the standalone v1 `docker-compose`
        # binary is end-of-life and is NOT what this calls.
        install=(
            "brew install docker docker-compose",
            "apt-get install docker-compose-plugin",
        ),
        url="https://docs.docker.com/compose/install/",
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
    head: str | None = None,
) -> str:
    """Render the actionable 'here is how to fix it' notice for a missing tool.

    Uses the registry as the default and lets a caller override any field
    (a one-off tool, or a context-specific purpose). Multi-line, safe to pass
    straight to :func:`info` / :func:`warn` / :func:`error`.

    ``head`` replaces the "is not installed" opening for a tool that IS present
    but unusable - an unsupported version, say. The install guidance is the same
    either way, and this keeps a caller from restating it to avoid saying
    something false.
    """
    reg = _REGISTRY.get(name)
    purpose = purpose if purpose is not None else (reg.purpose if reg else None)
    installs = tuple(install) if install is not None else (reg.install if reg else ())
    url = url if url is not None else (reg.url if reg else "")

    opening = head if head is not None else f"`{name}` is not installed"
    if purpose:
        opening += f" - hyperi-ci needs it for {purpose}"
    lines = [opening + "."]
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
