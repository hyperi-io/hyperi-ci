# Project:   HyperI CI
# File:      src/hyperi_ci/quality/compose_pins.py
# Purpose:   Assert every compose service image is pinned (GATE, Path C)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Compose image-pin assertion - the compose GATE that needs no tools.

Compose is a supported production deploy target, so "which image am I running?"
must not have a silent answer. An ``image:`` with no tag, or one whose tag
resolves to ``latest`` when nothing is set, gives the registry the final say
over what a deploy runs.

Every reference is resolved the way compose would resolve it with NOTHING set,
which is the state a fresh checkout and a CI runner are both in:

* a mandatory ``${VAR:?message}`` anywhere in the reference means compose aborts
  rather than resolving anything - the operator has to supply the pin, so it is
  clean here;
* ``${VAR:-default}`` resolves to its default, and ``${VAR}`` to nothing, so
  both are judged on what that leaves behind;
* a ``@sha256:`` digest, literal or carried in a default, is a full pin.

Findings split by how much is actually at stake. An untagged reference or one
resolving to ``latest`` is an ERROR and gates: the running image can change
under a deploy nobody made. A reference that resolves to a real tag is a NOTICE:
a tag is mutable at the registry, so it is weaker than a digest, but it is a
deliberate pin rather than a silent one.

Static by construction - it reads the file rather than asking the daemon - so it
covers a repo with no docker installed, and it covers every compose file
including the overlay fragments, which ``docker compose config`` can only reach
through a file set nobody outside the repo can name.
"""

from __future__ import annotations

import re
from pathlib import Path

from hyperi_ci.common import error, info, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.quality_common import resolve_cross_tool_mode
from hyperi_ci.quality import findings as fdg

_IMAGE_LINE = re.compile(r"^\s*image:\s*(?P<ref>\S.*?)\s*$")

# `${NAME}`, `${NAME:-default}`, `${NAME-default}`, `${NAME:?msg}`, `${NAME?msg}`
# - compose's whole interpolation surface. Defaults carry no braces of their own.
_INTERPOLATION = re.compile(
    r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P<form>:?[-?])?(?P<rest>[^}]*)\}"
)

_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}")

_FLOATING_TAG = "latest"


def _strip_comment(value: str) -> str:
    """Drop a trailing YAML comment and surrounding quotes from a scalar."""
    out: list[str] = []
    quote = ""
    for index, char in enumerate(value):
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "#" and (index == 0 or value[index - 1].isspace()):
            break
        out.append(char)
    return "".join(out).strip().strip("\"'")


def _split_outside_braces(value: str, char: str) -> tuple[str, str] | None:
    """Split ``value`` at the first ``char`` that is not inside a ``${...}``."""
    depth = 0
    index = 0
    while index < len(value):
        if value.startswith("${", index):
            depth += 1
            index += 2
            continue
        if value[index] == "}" and depth:
            depth -= 1
        elif value[index] == char and not depth:
            return value[:index], value[index + 1 :]
        index += 1
    return None


def resolve_unset(reference: str) -> str | None:
    """Return ``reference`` as compose resolves it with nothing set.

    None means compose aborts instead: a mandatory ``${VAR:?}`` is present, so
    the reference cannot silently become anything at all.
    """
    if any("?" in (m.group("form") or "") for m in _INTERPOLATION.finditer(reference)):
        return None
    return _INTERPOLATION.sub(
        lambda m: m.group("rest") if "-" in (m.group("form") or "") else "", reference
    )


def _tag(reference: str) -> str | None:
    """Return the tag of a fully literal image reference, or None when untagged."""
    name = reference.rsplit("/", 1)[-1]
    split = _split_outside_braces(name, ":")
    return None if split is None else split[1]


def classify(reference: str) -> tuple[str, str] | None:
    """Return the ``(level, message)`` for one image reference, or None when pinned."""
    resolved = resolve_unset(reference)
    if resolved is None:
        return None
    if _DIGEST.search(resolved):
        return None
    tag = _tag(resolved)
    if tag is None:
        return (
            "error",
            f"`{reference}` carries no tag, so it resolves to `{_FLOATING_TAG}` "
            "and the registry decides what a deploy runs",
        )
    if not tag or tag == _FLOATING_TAG:
        return (
            "error",
            f"`{reference}` resolves to `{_FLOATING_TAG}` with nothing set - pin it "
            "with a digest, or make the tag a mandatory `${VAR:?...}`",
        )
    return (
        "notice",
        f"`{reference}` resolves to the tag `{tag}`, which the registry can move - "
        "a `@sha256:` digest is the stronger pin",
    )


def scan(path: Path) -> list[fdg.Finding]:
    """Return one finding per unpinned ``image:`` in the compose file at ``path``."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [
            fdg.Finding(
                tool="compose-pins",
                path=str(path),
                line=None,
                level="error",
                rule="compose/unreadable",
                message=f"could not be read ({exc})",
            )
        ]
    out: list[fdg.Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = _IMAGE_LINE.match(line)
        if match is None:
            continue
        reference = _strip_comment(match.group("ref"))
        if not reference:
            continue
        verdict = classify(reference)
        if verdict is None:
            continue
        level, message = verdict
        out.append(
            fdg.Finding(
                tool="compose-pins",
                path=str(path),
                line=number,
                level=level,
                rule="compose/unpinned-image"
                if level == "error"
                else "compose/undigested-image",
                message=message,
            )
        )
    return out


def run(
    files: list[Path],
    config: CIConfig,
    *,
    sarif_path: str | Path | None = None,
) -> int:
    """Assert every image in ``files`` is pinned. Returns exit code.

    0 = every reference pinned or mandatory / notices only / disabled / no files;
    1 = a blocking gate found a reference that resolves to ``latest``.
    """
    mode = resolve_cross_tool_mode(config, "compose_pins", "blocking")
    if mode == "disabled":
        info("  compose-pins: disabled")
        return 0
    if not files:
        info("  compose-pins: no compose files to check - skipping")
        return 0

    found: list[fdg.Finding] = []
    for path in files:
        found.extend(scan(path))

    dropped = fdg.surface("compose-pins", found, sarif_path=sarif_path)
    if dropped:
        info(f"  compose-pins: +{dropped} more finding(s) in the job summary")

    floating = [f for f in found if f.level == "error"]
    if not floating:
        success(
            f"  compose-pins: no image in {len(files)} file(s) resolves to "
            f"`{_FLOATING_TAG}`"
        )
        return 0
    if mode == "blocking":
        error(
            f"  compose-pins: {len(floating)} image(s) resolve to `{_FLOATING_TAG}` "
            "and must be pinned"
        )
        return 1
    warn(
        f"  compose-pins: {len(floating)} unpinned image(s) (non-blocking)",
    )
    return 0
