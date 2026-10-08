# Project:   HyperI CI
# File:      src/hyperi_ci/quality/mermaid_parse.py
# Purpose:   Parse-check every fenced mermaid block in the repo's markdown
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Parse-check every fenced mermaid block with mermaid's own parser.

``mmdc`` is not used: it needs headless Chrome and has exited 0 on syntax it
could not draw. Two layers:

1. Structural, pure Python, always: an unclosed or empty fence, neither of
   which the parser can report.
2. Grammar, when Node resolves ``mermaid`` and ``linkedom``, from the repo's
   ``node_modules`` or on CI from :mod:`hyperi_ci.quality.node_tools`.
   ``mermaid_runner.mjs`` supplies the browser globals the bundle needs. When
   the packages are missing the layer skips, which fails a blocking check in CI.

Without ``linkedom``, mermaid 12 throws ``DOMPurify.addHook is not a function``
on a valid flowchart.
"""

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from hyperi_ci.common import error, info, run_cmd, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_tool_mode
from hyperi_ci.quality import findings as fdg
from hyperi_ci.quality import node_tools
from hyperi_ci.tools import missing_tool

RUNNER = Path(__file__).with_name("mermaid_runner.mjs")

# The npm packages the runner imports.
NODE_PACKAGES = ("mermaid", "linkedom")

_INSTALL_LINE = f"npm install --no-save {' '.join(NODE_PACKAGES)}"

# A CommonMark fence: up to three spaces of indent, then 3+ backticks or
# tildes, then the info string whose first word names the language.
_FENCE = re.compile(r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})(?P<info>.*)$")


@dataclass(frozen=True)
class Block:
    """One fenced mermaid block.

    ``line`` is the 1-indexed line of the opening fence. ``text`` excludes both
    fence lines.
    """

    path: Path
    line: int
    text: str
    closed: bool


def extract_blocks(path: Path) -> list[Block]:
    """Return every fenced ``mermaid`` block in the markdown file at ``path``.

    Only a fence of the same character and at least the same length closes a
    block. A block still open at end of file is returned with ``closed=False``.
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
    """Return an unclosed- or empty-fence finding for ``block``, else None."""
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


def _module_dirs(root: Path) -> list[Path]:
    """Return the ``node_modules`` directories the runner may import from.

    The repo's own comes first. hyperi-ci's pinned install is added only when
    the repo lacks one of the packages.
    """
    own = root / "node_modules"
    dirs = [own]
    if not all((own / name / "package.json").is_file() for name in NODE_PACKAGES):
        installed = node_tools.install()
        if installed is not None:
            dirs.append(installed)
    return dirs


def _node_env(root: Path) -> dict[str, str]:
    """Return the environment naming the runner's ``node_modules`` directories.

    NODE_PATH does not work: node's ESM loader ignores it, and the runner's own
    search starts in site-packages.
    """
    dirs = os.pathsep.join(str(d) for d in _module_dirs(root))
    return {"HYPERCI_NODE_MODULES": dirs}


def _run_parser(blocks: list[Block], root: Path) -> tuple[dict[int, str], str | None]:
    """Parse ``blocks`` with mermaid.

    Returns:
        The parse error per block index, and a skip reason when the layer did
        not run (no node, packages absent, the runner broke), else None.
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
    """Parse-check every mermaid block in ``files``; return the exit code.

    Returns 1 when a blocking check finds a broken block, or cannot run the
    parser in CI.
    """
    mode = resolve_tool_mode("mermaid_parse", config, default="warn")
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

    dropped = fdg.surface(
        "mermaid-parse", fdg.at_mode(found, mode), sarif_path=sarif_path
    )
    if dropped:
        info(f"  mermaid-parse: +{dropped} more finding(s) in the job summary")

    # The structural layer alone says nothing about grammar.
    if skipped and missing_tool(
        "mermaid",
        mode,
        head=f"the mermaid grammar check did not run ({skipped})",
        install=(_INSTALL_LINE,),
    ):
        error("  mermaid-parse: the grammar check is blocking and could not run")
        return 1

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
