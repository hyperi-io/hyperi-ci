# Project:   HyperI CI
# File:      src/hyperi_ci/quality/doc_links.py
# Purpose:   Check repo-internal doc links and anchors with lychee
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Repo-internal link + anchor checking via lychee.

``--offline`` is the whole design. It blocks every network request, so the
check is about THIS repo: a relative link whose target moved, and an anchor
naming a heading that was renamed. Those are deterministic - the same commit
gives the same answer on every run, on a runner with no egress - which is what
makes them safe to promote past advisory.

External links are the opposite: they fail for rate limits, Cloudflare
challenges and outages that have nothing to do with the commit under test, so
checking them on a PR converts someone else's downtime into your red build.
That check belongs on a schedule with retries and an issue, and is NOT built
here - ``--offline`` is passed unconditionally rather than exposed as a knob.

Anchors come from ``--include-fragments``, which resolves ``#heading-name``
against the target document's own headings. That catches the half of link rot
that a file-existence check cannot see.

A repo silences a known-bad link with a committed ``.lycheeignore``, which
lychee reads from the directory it runs in.
"""

from __future__ import annotations

import json
import platform
import shutil
from pathlib import Path

from hyperi_ci.common import error, info, is_ci, run_cmd, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_cross_tool_mode
from hyperi_ci.quality import findings as fdg
from hyperi_ci.quality.install import install_ci_binary
from hyperi_ci.tools import missing_tool_notice
from hyperi_ci.versions import tool_sha256, tool_version


def _install_lychee() -> str | None:
    """Install the pinned lychee release on Linux CI (else None).

    Without this the check warned about a missing binary on every consumer run
    and nothing could act on it, because no runner image or install path
    supplied one (issue #230).
    """
    target = (
        "x86_64-unknown-linux-musl"
        if platform.machine() in ("x86_64", "AMD64")
        else "aarch64-unknown-linux-musl"
    )
    arch = "amd64" if target.startswith("x86_64") else "arm64"
    url = (
        f"https://github.com/lycheeverse/lychee/releases/download/"
        f"lychee-v{tool_version('lychee')}/lychee-{target}.tar.gz"
    )
    return install_ci_binary(
        "lychee",
        url,
        tar_member="lychee",
        expected_sha256=tool_sha256("lychee", arch),
    )


def resolve_mode(config: CIConfig) -> str:
    """Resolve lychee's mode: ``warn`` (default) / ``blocking`` / ``disabled``."""
    return resolve_cross_tool_mode(config, "doc_links", "warn")


def will_run(config: CIConfig) -> bool:
    """Return True when lychee is enabled AND present, so it will do the work.

    The orchestrator asks before running :mod:`doc_paths`, which otherwise
    reports the same broken link a second time. Installs lychee to answer,
    because the answer has to match what :func:`run` does a moment later.
    """
    if resolve_mode(config) == "disabled":
        return False
    return (shutil.which("lychee") or _install_lychee()) is not None


def parse(stdout: str) -> list[fdg.Finding]:
    """Parse lychee ``--format json`` into normalised findings.

    Reads ``error_map`` only. ``excluded_map`` holds the external links
    ``--offline`` declined to check, which is the intended outcome rather than
    a finding.
    """
    try:
        doc = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return []
    if not isinstance(doc, dict):
        return []
    out: list[fdg.Finding] = []
    for path, entries in (doc.get("error_map") or {}).items():
        for entry in entries or []:
            status = entry.get("status") or {}
            span = entry.get("span") or {}
            out.append(
                fdg.Finding(
                    tool="doc-links",
                    path=str(path),
                    line=span.get("line"),
                    level="error",
                    rule="docs/broken-link",
                    message=f"{entry.get('url', '')} - {status.get('text', 'failed')}",
                )
            )
    return out


def run(
    files: list[Path],
    config: CIConfig,
    *,
    root: Path | None = None,
    sarif_path: str | Path | None = None,
) -> int:
    """Check every internal link and anchor in ``files``. Returns exit code.

    0 = every link resolves / advisory mode / disabled / no files; 1 = a
    blocking check found a broken link, or lychee is required-but-missing in CI.
    """
    mode = resolve_mode(config)
    if mode == "disabled":
        info("  doc-links: disabled")
        return 0
    if not files:
        info("  doc-links: no markdown to check - skipping")
        return 0

    root = Path(root or Path.cwd())
    exe = shutil.which("lychee") or _install_lychee()
    if not exe:
        # A gate that cannot run has not passed - fail it in CI, warn locally
        # where a missing linker is an ordinary state of a dev box.
        if mode == "blocking" and is_ci():
            error(missing_tool_notice("lychee"))
            return 1
        warn(missing_tool_notice("lychee"))
        return 0

    info(f"  doc-links: checking {len(files)} markdown file(s)...")
    try:
        result = run_cmd(
            [
                exe,
                "--offline",
                "--include-fragments",
                "--no-progress",
                "--format",
                "json",
                *[str(f) for f in files],
            ],
            check=False,
            capture=True,
            cwd=root,
        )
    except OSError as exc:
        warn(f"  lychee could not be run ({exc})")
        if mode == "blocking" and is_ci():
            error("  doc-links: the check is blocking and could not run")
            return 1
        return 0

    found = parse(result.stdout)

    # lychee exits 2 for broken links and 1 for its own failure. A non-zero exit
    # with nothing parsed is the TOOL erroring, not a clean docs tree - scoring
    # that green is the failure mode a gate exists to prevent.
    if result.returncode not in (0, 2) and not found:
        warn(
            f"  lychee exited {result.returncode} with no parseable output - "
            "tool error, not a clean pass"
        )
        if mode == "blocking" and is_ci():
            error("  doc-links: the check is blocking and could not complete")
            return 1
        return 0

    dropped = fdg.surface("doc-links", found, sarif_path=sarif_path)
    if dropped:
        info(f"  doc-links: +{dropped} more finding(s) in the job summary")

    if not found:
        success(f"  doc-links: every internal link in {len(files)} file(s) resolves")
        return 0
    if mode == "blocking":
        error(f"  doc-links: {len(found)} broken internal link(s) or anchor(s)")
        return 1
    warn(f"  doc-links: {len(found)} broken internal link(s) (non-blocking)")
    return 0
