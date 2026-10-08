# Project:   HyperI CI
# File:      src/hyperi_ci/tools.py
# Purpose:   External-tool presence checks with actionable, Rust-style guidance
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""External-tool presence checks with actionable, Rust-style guidance.

A missing tool gets a notice naming what hyperi-ci needs it for and the exact
way to install it (command(s) + docs URL). The CALLER decides whether that is
fatal (a required tool in CI) or a skip (an optional advisory).

``_REGISTRY`` is the SSoT for the install hints, and an unknown tool still gets
a generic notice. Callers pick the emit level:

    exe = find_tool("alint")                    # optional -> info-skip
    exe = find_tool("gitleaks", recommended=True)  # nice-to-have -> warn-skip
    if not shutil.which("gh"):                   # required -> caller fails
        error(missing_tool_notice("gh")); return False

:func:`warn_on_pin_drift` names a PATH copy whose version differs from
``versions.yaml``, for tools CI installs pinned but a dev box carries at any
version.
"""

import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass

from hyperi_ci.common import error, info, is_ci, run_cmd, warn
from hyperi_ci.versions import tool_version


@dataclass(frozen=True)
class ToolInfo:
    """How to install one external tool, and what hyperi-ci needs it for."""

    name: str
    purpose: str
    # One or more copy-pasteable install command lines (first = preferred).
    install: tuple[str, ...] = ()
    url: str = ""


# External tools hyperi-ci shells out to, with copy-pasteable install lines.
_REGISTRY: dict[str, ToolInfo] = {
    "alint": ToolInfo(
        name="alint",
        purpose="profile-aware repo-hygiene advice (.gitignore / .editorconfig / lockfiles / ...)",
        # Prebuilt binaries first: `cargo install` compiles from source (minutes).
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
        # Never `go install ...@latest`: unpinned and built from source.
        install=(
            "brew install gitleaks",
            "download a release binary: https://github.com/gitleaks/gitleaks/releases/latest",
        ),
        url="https://github.com/gitleaks/gitleaks#installing",
    ),
    "semgrep": ToolInfo(
        name="semgrep",
        purpose="SAST scanning",
        # uv first: it is already a hard dependency, so `uvx` needs nothing new.
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
        # Linux CI auto-installs the pinned release, so this is for local dev.
        install=(
            "brew install hadolint",
            "download a release binary: https://github.com/hadolint/hadolint/releases/latest",
        ),
        url="https://github.com/hadolint/hadolint#install",
    ),
    "droast": ToolInfo(
        name="droast",
        purpose="Dockerfile advisory - cache ordering / .dockerignore / npm ci (DF070/DF033/DF031)",
        # Advisory-only, never auto-installed.
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
        # Linux CI auto-installs the pinned release, so this is for local dev.
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
        # Prebuilt binaries first: `cargo install` compiles from source (minutes).
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
        # linkedom supplies the browser globals mermaid's bundle needs, or a
        # valid flowchart throws. CI installs the pinned set via
        # quality/node_tools.py.
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
        # The v2 plugin, not the end-of-life standalone `docker-compose` v1.
        install=(
            "brew install docker docker-compose",
            "apt-get install docker-compose-plugin",
        ),
        url="https://docs.docker.com/compose/install/",
    ),
    "helm": ToolInfo(
        name="helm",
        purpose="Helm chart rendering for manifest linting",
        install=("brew install helm",),
        url="https://helm.sh/docs/intro/install/",
    ),
    "tofu": ToolInfo(
        name="tofu",
        purpose="OpenTofu fmt and validate (lint-iac)",
        # Linux CI installs the pinned release, so this is for local dev.
        install=(
            "brew install opentofu",
            "download a release binary: https://github.com/opentofu/opentofu/releases/latest",
        ),
        url="https://opentofu.org/docs/intro/install/",
    ),
    "kustomize": ToolInfo(
        name="kustomize",
        purpose="rendering kustomizations for schema validation (lint-iac)",
        install=(
            "brew install kustomize",
            "download a release binary: https://github.com/kubernetes-sigs/kustomize/releases",
        ),
        url="https://kubectl.docs.kubernetes.io/installation/kustomize/",
    ),
    "ansible-lint": ToolInfo(
        name="ansible-lint",
        purpose="ansible playbook and role linting (lint-iac)",
        install=("uvx ansible-lint --version", "uv tool install ansible-lint"),
        url="https://ansible.readthedocs.io/projects/lint/installing/",
    ),
    "aws": ToolInfo(
        name="aws",
        purpose="S3-compatible upload to Cloudflare R2",
        install=("brew install awscli",),
        url="https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html",
    ),
}


def missing_tool_notice(
    name: str,
    *,
    purpose: str | None = None,
    install: tuple[str, ...] | list[str] | None = None,
    url: str | None = None,
    head: str | None = None,
) -> str:
    """Render the actionable 'here is how to fix it' notice for a missing tool.

    The registry supplies the defaults and a caller can override any field.
    Multi-line, safe to pass to :func:`info` / :func:`warn` / :func:`error`.

    ``head`` replaces the "is not installed" opening for a tool that IS present
    but unusable, such as an unsupported version.
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


def missing_tool(
    name: str,
    mode: str,
    *,
    purpose: str | None = None,
    head: str | None = None,
    install: tuple[str, ...] | None = None,
) -> int:
    """Report a missing tool: fail a blocking gate in CI (returns 1), else warn-skip.

    ``purpose``, ``head`` and ``install`` override the notice as for
    :func:`missing_tool_notice`.
    """
    notice = missing_tool_notice(name, purpose=purpose, head=head, install=install)
    if mode == "blocking" and is_ci():
        error(notice)
        return 1
    warn(notice)
    return 0


def find_tool(
    name: str,
    *,
    recommended: bool = False,
    purpose: str | None = None,
    install: tuple[str, ...] | list[str] | None = None,
    url: str | None = None,
) -> str | None:
    """Return the resolved tool path, or None after emitting a helpful notice.

    Never raises or exits: the CALLER owns fatality. ``recommended=True`` emits
    the notice at warn level, otherwise info.
    """
    exe = shutil.which(name)
    if exe:
        return exe
    notice = missing_tool_notice(name, purpose=purpose, install=install, url=url)
    (warn if recommended else info)(notice)
    return None


# Tools whose version flag is not ``--version``.
_VERSION_ARGS: dict[str, tuple[str, ...]] = {"govulncheck": ("-version",)}


def version_output(argv: Sequence[str]) -> str | None:
    """Return what a version probe printed, stdout first, or None if it printed nothing.

    A probe that cannot run only thins the caller's message and never blocks it.
    """
    try:
        result = run_cmd(list(argv), check=False, capture=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = "\n".join(s.strip() for s in (result.stdout, result.stderr) if s).strip()
    return output or None


def installed_version(binary: str) -> str | None:
    """First line of ``binary --version``, or None when it prints nothing."""
    output = version_output([binary, "--version"])
    return output.splitlines()[0] if output else None


def matches_pin(pin: str, output: str) -> bool:
    """Return True when ``output`` names the pinned version as a whole number.

    The leading ``v`` is dropped because tools print ``2.6.0`` for tag
    ``v2.6.0``, and digit boundaries keep ``0.20.2`` from matching ``0.20.21``.
    """
    bare = re.escape(pin.removeprefix("v"))
    return re.search(rf"(?<![\d.]){bare}(?!\.?\d)", output) is not None


def warn_on_pin_drift(name: str) -> None:
    """Warn when the ``name`` on PATH is not the version ``versions.yaml`` pins.

    CI installs and asserts the pin, but a local run uses whatever is on PATH.
    An absent tool is the missing-tool path's business and stays quiet here.
    """
    exe = shutil.which(name)
    if exe is None:
        return
    pin = tool_version(name)
    output = version_output([exe, *_VERSION_ARGS.get(name, ("--version",))])
    if output is None:
        warn(f"  {name}: could not read the version of {exe} -- hyperi-ci pins {pin}")
        return
    if not matches_pin(pin, output):
        warn(
            f"  {name}: {exe} reports '{output.splitlines()[0]}' but hyperi-ci "
            f"pins {pin}, so this result can differ from CI's"
        )
