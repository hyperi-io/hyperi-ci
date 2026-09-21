# Project:   HyperI CI
# File:      src/hyperi_ci/quality/mermaid_parse.py
# Purpose:   Parse-check every fenced mermaid block in the repo's markdown
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Mermaid diagram parse checking - the docs check with a real grammar behind it.

A broken mermaid block renders as an error box on GitHub and in the docs site,
and nothing in the repo notices. The check that catches it must use mermaid's
own PARSER (``mermaid.parse``), not ``mmdc``: the render path needs headless
Chrome, and has exited 0 on syntax it could not draw.

Two layers, because the parser needs a Node toolchain and most repos do not
have one:

1. **Structural, always.** Fence extraction is pure Python, so an unclosed or
   empty block is caught with nothing installed. These are also the two faults
   the parser CANNOT report - an unclosed fence never becomes a block to hand
   it, and an empty one is a fence problem rather than a grammar problem.
2. **Grammar, when Node resolves ``mermaid`` + ``linkedom``.** The vendored
   ``mermaid_runner.mjs`` supplies the browser globals mermaid's bundle reaches
   for and parses each block. Absent the packages the layer skips with the
   install line; whether that skip is fatal is the mode's call, per the gate
   contract - a blocking check that cannot run has not passed.

``linkedom`` is not decoration. Without it a VALID flowchart throws
``DOMPurify.addHook is not a function`` on mermaid 12, so the layer would fail
correct diagrams.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from hyperi_ci.common import error, info, is_ci, run_cmd, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_cross_tool_mode
from hyperi_ci.quality import findings as fdg
from hyperi_ci.tools import missing_tool_notice

RUNNER = Path(__file__).with_name("mermaid_runner.mjs")

# The npm packages the runner imports. Named here so the skip message and the
# docs quote one list.
NODE_PACKAGES = ("mermaid", "linkedom")

_INSTALL_LINE = f"npm install --no-save {' '.join(NODE_PACKAGES)}"

# A CommonMark fence: up to three spaces of indent, then 3+ backticks or
# tildes, then the info string whose first word names the language.
_FENCE = re.compile(r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})(?P<info>.*)$")


@dataclass(frozen=True)
class Block:
    """One fenced mermaid block: where it starts, what is in it, and if it closed.

    ``line`` is the 1-indexed line of the OPENING fence, which is what a reader
    needs to find the block. ``text`` excludes both fence lines.
    """

    path: Path
    line: int
    text: str
    closed: bool


def extract_blocks(path: Path) -> list[Block]:
    """Return every fenced ``mermaid`` block in the markdown file at ``path``.

    Tracks the fence character and length so a longer outer fence quoting a
    shorter one (a markdown file documenting mermaid syntax) does not close
    early. A block still open at end of file is returned with
    ``closed=False`` - CommonMark closes it implicitly, so it renders, but it
    almost always means a fence was forgotten.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    out: list[Block] = []
    open_at: int | None = None
    open_fence = ""
    body: list[str] = []

    for number, line in enumerate(text.splitlines(), start=1):
        match = _FENCE.match(line)
        if open_at is None:
            if match and match.group("info").strip().split(" ")[0].lower() == "mermaid":
                open_at = number
                open_fence = match.group("fence")
                body = []
            continue
        # A closing fence is the same character, at least as long, info-free.
        if (
            match
            and match.group("fence")[0] == open_fence[0]
            and len(match.group("fence")) >= len(open_fence)
            and not match.group("info").strip()
        ):
            out.append(Block(path, open_at, "\n".join(body), closed=True))
            open_at = None
            continue
        body.append(line)

    if open_at is not None:
        out.append(Block(path, open_at, "\n".join(body), closed=False))
    return out


def screen(block: Block) -> fdg.Finding | None:
    """Return the structural fault in ``block``, or None when it is well-formed.

    Structural only - an unclosed or empty fence. Grammar is the parser's job,
    so nothing here guesses at diagram syntax and nothing here carries a list of
    diagram keywords that would go stale the next time mermaid adds a type.
    """
    if not block.closed:
        return fdg.Finding(
            tool="mermaid-parse",
            path=str(block.path),
            line=block.line,
            level="error",
            rule="mermaid/unclosed-fence",
            message=(
                "mermaid block is never closed - the rest of the file is "
                "swallowed into the diagram"
            ),
        )
    if not block.text.strip():
        return fdg.Finding(
            tool="mermaid-parse",
            path=str(block.path),
            line=block.line,
            level="error",
            rule="mermaid/empty-block",
            message="mermaid block is empty, so it renders as an error box",
        )
    return None


def _node_env(root: Path) -> dict[str, str]:
    """NODE_PATH pointing at the repo's own ``node_modules``.

    The runner lives inside the installed wheel, so node's upward search for
    ``node_modules`` starts in site-packages and never reaches the repo. Naming
    the repo's directory is what lets a project supply the two packages as its
    own dev dependencies.
    """
    parts = [str(root / "node_modules")]
    existing = os.environ.get("NODE_PATH")
    if existing:
        parts.append(existing)
    return {"NODE_PATH": os.pathsep.join(parts)}


def _run_parser(blocks: list[Block], root: Path) -> tuple[dict[int, str], str | None]:
    """Parse ``blocks`` with mermaid. Returns (index -> error message, skip reason).

    A skip reason means the layer did not run at all (no node, packages absent,
    the runner itself broke); the caller decides whether that is fatal. An empty
    reason with an empty map means every block parsed.
    """
    node = shutil.which("node")
    if not node:
        return {}, "node is not installed"

    payload = {"blocks": [{"id": str(i), "text": b.text} for i, b in enumerate(blocks)]}
    with tempfile.TemporaryDirectory(prefix="hyperi-mermaid-") as tmp:
        request = Path(tmp) / "blocks.json"
        request.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
        try:
            result = run_cmd(
                [node, str(RUNNER), str(request)],
                check=False,
                capture=True,
                cwd=root,
                env=_node_env(root),
            )
        except OSError as exc:
            return {}, f"the mermaid runner could not be started ({exc})"

    try:
        verdict = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {}, f"the mermaid runner emitted no verdict (exit {result.returncode})"

    if not verdict.get("ok"):
        reason = str(verdict.get("reason", "unknown"))
        if reason == "missing-dependency":
            return {}, f"node cannot resolve {' + '.join(NODE_PACKAGES)}"
        return {}, f"the mermaid runner failed ({reason}: {verdict.get('detail', '')})"

    errors: dict[int, str] = {}
    for item in verdict.get("results", []):
        if item.get("ok"):
            continue
        try:
            index = int(item.get("id", -1))
        except (TypeError, ValueError):
            continue
        errors[index] = _one_line(str(item.get("error", "parse failed")))
    return errors, None


def _one_line(message: str) -> str:
    """Flatten mermaid's multi-line parse error into one annotation-sized line."""
    flat = " | ".join(part.strip() for part in message.splitlines() if part.strip())
    return flat[:400]


def run(
    files: list[Path],
    config: CIConfig,
    *,
    root: Path | None = None,
    sarif_path: str | Path | None = None,
) -> int:
    """Parse-check every mermaid block in ``files``. Returns exit code.

    0 = every block parses / advisory mode / disabled / no blocks; 1 = a
    blocking check found a broken block, or could not run the parser in CI.
    """
    mode = resolve_cross_tool_mode(config, "mermaid_parse", "warn")
    if mode == "disabled":
        info("  mermaid-parse: disabled")
        return 0

    root = Path(root or Path.cwd())
    blocks: list[Block] = []
    for path in files:
        blocks.extend(extract_blocks(path))
    if not blocks:
        info("  mermaid-parse: no mermaid blocks found - skipping")
        return 0

    found: list[fdg.Finding] = []
    parseable: list[Block] = []
    for block in blocks:
        fault = screen(block)
        if fault is not None:
            found.append(fault)
            continue
        parseable.append(block)

    info(f"  mermaid-parse: {len(blocks)} block(s) in {len(files)} file(s)...")
    errors, skipped = _run_parser(parseable, root)
    for index, message in errors.items():
        block = parseable[index]
        found.append(
            fdg.Finding(
                tool="mermaid-parse",
                path=str(block.path),
                line=block.line,
                level="error",
                rule="mermaid/parse-error",
                message=message,
                url="https://mermaid.js.org/intro/syntax-reference.html",
            )
        )

    dropped = fdg.surface("mermaid-parse", found, sarif_path=sarif_path)
    if dropped:
        info(f"  mermaid-parse: +{dropped} more finding(s) in the job summary")

    if skipped:
        notice = missing_tool_notice(
            "mermaid",
            head=f"the mermaid grammar check did not run ({skipped})",
            install=(_INSTALL_LINE,),
        )
        # A blocking check that could not run has NOT passed: the structural
        # layer alone says nothing about grammar. Locally it stays a warning,
        # because a missing Node toolchain is normal on a Rust or Python box.
        if mode == "blocking" and is_ci():
            error(notice)
            error("  mermaid-parse: the grammar check is blocking and could not run")
            return 1
        warn(notice)

    if not found:
        if skipped:
            info(f"  mermaid-parse: {len(blocks)} block(s) structurally sound")
        else:
            success(f"  mermaid-parse: {len(blocks)} block(s) parse")
        return 0
    if mode == "blocking":
        error(f"  mermaid-parse: {len(found)} broken mermaid block(s)")
        return 1
    warn(f"  mermaid-parse: {len(found)} broken mermaid block(s) (non-blocking)")
    return 0
