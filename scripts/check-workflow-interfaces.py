#!/usr/bin/env python3
# Project:   HyperI CI
# File:      scripts/check-workflow-interfaces.py
# Purpose:   Gate — reusable-workflow/composite interfaces stay backward-compatible
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Interface backward-compatibility gate (issue #31).

Consumers pin a caller (`python-ci.yml@<sha>`), but its siblings are written
`@main`, so the transitive graph floats live. If a sibling's `workflow_call`
or composite interface regresses, the pinned caller's graph fails to compile
at startup — 0 jobs, no logs, and it breaks consumers RETROACTIVELY.

This gate compares each reusable workflow + composite interface in the working
tree against the LAST RELEASE TAG and fails on a backward-incompatible delta:
removed input/output/secret, a newly-required input, or optional→required. Run
in hyperi-ci's own CI so a break is caught before it ever reaches a consumer.

Usage:  uv run scripts/check-workflow-interfaces.py
Exit 1 if any interface regressed; 0 otherwise.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOWS = _ROOT / ".github" / "workflows"
_ACTIONS = _ROOT / ".github" / "actions"


def parse_interface(yaml_text: str) -> dict:
    """Extract the call interface from a workflow or composite-action file.

    Returns ``{kind, inputs, secrets, outputs}`` where
      inputs:  {name: {required: bool, has_default: bool}}
      secrets: {name: {required: bool}}
      outputs: set[str]
    kind is "workflow" (has on.workflow_call), "composite" (runs.using ==
    composite), or "other" (skip).
    """
    data = yaml.safe_load(yaml_text) or {}
    # PyYAML parses the bare key `on:` as the boolean True (YAML 1.1) — accept both.
    on = data.get("on")
    if on is None:
        on = data.get(True, {})
    if not isinstance(on, dict):
        on = {}

    wc = on.get("workflow_call")
    if isinstance(wc, dict):
        return {
            "kind": "workflow",
            "inputs": _inputs(wc.get("inputs")),
            "secrets": _secrets(wc.get("secrets")),
            "outputs": set((wc.get("outputs") or {}).keys()),
        }

    runs = data.get("runs")
    if isinstance(runs, dict) and runs.get("using") == "composite":
        return {
            "kind": "composite",
            "inputs": _inputs(data.get("inputs")),
            "secrets": {},
            "outputs": set((data.get("outputs") or {}).keys()),
        }

    return {"kind": "other", "inputs": {}, "secrets": {}, "outputs": set()}


def _inputs(raw: object) -> dict:
    out: dict[str, dict] = {}
    if isinstance(raw, dict):
        for name, spec in raw.items():
            spec = spec if isinstance(spec, dict) else {}
            out[name] = {
                "required": bool(spec.get("required", False)),
                "has_default": "default" in spec,
            }
    return out


def _secrets(raw: object) -> dict:
    out: dict[str, dict] = {}
    if isinstance(raw, dict):
        for name, spec in raw.items():
            spec = spec if isinstance(spec, dict) else {}
            out[name] = {"required": bool(spec.get("required", False))}
    return out


def breaking_deltas(old: dict, new: dict) -> list[str]:
    """Backward-incompatible changes from `old` to `new` (empty == safe)."""
    deltas: list[str] = []

    old_in, new_in = old["inputs"], new["inputs"]
    for name in old_in:
        if name not in new_in:
            deltas.append(f"input '{name}' removed (a pinned caller may still pass it)")
    for name, spec in new_in.items():
        was = old_in.get(name)
        # New required input with no default → old callers don't pass it.
        if was is None and spec["required"] and not spec["has_default"]:
            deltas.append(f"input '{name}' added as required (no default)")
        # Existing input tightened to required.
        elif was is not None and spec["required"] and not was["required"]:
            deltas.append(f"input '{name}' changed optional → required")

    for name in old["outputs"]:
        if name not in new["outputs"]:
            deltas.append(f"output '{name}' removed (a pinned caller may read it)")

    old_sec, new_sec = old["secrets"], new["secrets"]
    for name in old_sec:
        if name not in new_sec:
            deltas.append(f"secret '{name}' removed")
    for name, spec in new_sec.items():
        was = old_sec.get(name)
        if was is None and spec["required"]:
            deltas.append(f"secret '{name}' added as required")
        elif was is not None and spec["required"] and not was["required"]:
            deltas.append(f"secret '{name}' changed optional → required")

    return deltas


def _tracked_files() -> list[Path]:
    files = [p for p in sorted(_WORKFLOWS.glob("*.yml")) if _WORKFLOWS.exists()]
    if _ACTIONS.is_dir():
        files += sorted(_ACTIONS.glob("*/action.yml"))
    return files


def _last_release_tag() -> str | None:
    result = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0"],
        capture_output=True,
        text=True,
        cwd=_ROOT,
    )
    return result.stdout.strip() or None if result.returncode == 0 else None


def _file_at(tag: str, rel_path: str) -> str | None:
    result = subprocess.run(
        ["git", "show", f"{tag}:{rel_path}"],
        capture_output=True,
        text=True,
        cwd=_ROOT,
    )
    return result.stdout if result.returncode == 0 else None


def main() -> int:
    tag = _last_release_tag()
    if not tag:
        print("No release tag to compare against — skipping interface gate.")
        return 0

    print(f"Interface compat gate — working tree vs {tag}\n")
    regressions = 0
    for path in _tracked_files():
        rel = path.relative_to(_ROOT).as_posix()
        new_iface = parse_interface(path.read_text())
        if new_iface["kind"] == "other":
            continue
        old_text = _file_at(tag, rel)
        if old_text is None:
            print(f"  {rel}: new since {tag} — no baseline, OK")
            continue
        old_iface = parse_interface(old_text)
        if old_iface["kind"] == "other":
            continue
        deltas = breaking_deltas(old_iface, new_iface)
        if deltas:
            regressions += len(deltas)
            print(f"  ✗ {rel}:")
            for d in deltas:
                print(f"      - {d}")
        else:
            print(f"  ✓ {rel}")

    if regressions:
        print(
            f"\n{regressions} interface regression(s). A consumer pinned to an "
            f"older caller would fail at startup (issue #31).\n"
            "Make the change additive (optional inputs, keep outputs/secrets), "
            "or cut a deliberate major break."
        )
        return 1
    print("\nAll interfaces backward-compatible.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
