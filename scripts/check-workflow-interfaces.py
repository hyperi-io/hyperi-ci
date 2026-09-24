#!/usr/bin/env python3
# Project:   HyperI CI
# File:      scripts/check-workflow-interfaces.py
# Purpose:   Gate -- reusable-workflow/composite interfaces stay backward-compatible
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Interface backward-compatibility gate (issue #31).

Consumers pin a caller (`python-ci.yml@<sha>`), but its siblings are written
`@main`, so the transitive graph floats live. If a sibling's `workflow_call`
or composite interface regresses, the pinned caller's graph fails to compile
at startup -- 0 jobs, no logs, and it breaks consumers RETROACTIVELY.

This gate compares each reusable workflow + composite interface in the working
tree against the LAST RELEASE TAG and fails on a backward-incompatible delta:
removed input/output/secret, a newly-required input, or optional→required. Run
in hyperi-ci's own CI so a break is caught before it ever reaches a consumer.

Comparing interface to interface cannot express "nothing consumes this", so a
deliberate retirement reads as a regression too. `config/retired-interfaces.yaml`
carries the evidence for one, and this gate honours and reports it.

Usage:  uv run scripts/check-workflow-interfaces.py
Exit 1 if any interface regressed; 0 otherwise.
"""

import json
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import yaml

from hyperi_ci.common import URL_ERRORS, url_read

_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOWS = _ROOT / ".github" / "workflows"
_ACTIONS = _ROOT / ".github" / "actions"
_RETIREMENTS = _ROOT / "config" / "retired-interfaces.yaml"


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
    # PyYAML parses the bare key `on:` as the boolean True (YAML 1.1) -- accept both.
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
            out[str(name)] = {
                "required": bool(spec.get("required", False)),
                "has_default": "default" in spec,
            }
    return out


def _secrets(raw: object) -> dict:
    out: dict[str, dict] = {}
    if isinstance(raw, dict):
        for name, spec in raw.items():
            spec = spec if isinstance(spec, dict) else {}
            out[str(name)] = {"required": bool(spec.get("required", False))}
    return out


_REMOVAL_MESSAGE = {
    "input": "input '{name}' removed (a pinned caller may still pass it)",
    "output": "output '{name}' removed (a pinned caller may read it)",
    "secret": "secret '{name}' removed",
}


def removed_members(old: dict, new: dict) -> set[tuple[str, str]]:
    """(kind, name) pairs `old` declares and `new` does not.

    One definition of "removed", shared by the delta report and the retirement
    bookkeeping, so the two cannot drift apart.
    """
    gone = {("input", n) for n in old["inputs"] if n not in new["inputs"]}
    gone |= {("output", n) for n in old["outputs"] if n not in new["outputs"]}
    gone |= {("secret", n) for n in old["secrets"] if n not in new["secrets"]}
    return gone


def breaking_deltas(
    old: dict, new: dict, retired: frozenset[tuple[str, str]] = frozenset()
) -> list[str]:
    """Backward-incompatible changes from `old` to `new` (empty == safe).

    `retired` holds the (kind, name) pairs a retirement record has cleared for
    this file; a removal listed there is not reported.
    """
    deltas: list[str] = []

    for kind, name in sorted(removed_members(old, new)):
        if (kind, name) not in retired:
            deltas.append(_REMOVAL_MESSAGE[kind].format(name=name))

    old_in, new_in = old["inputs"], new["inputs"]
    for name, spec in new_in.items():
        was = old_in.get(name)
        # New required input with no default → old callers don't pass it.
        if was is None and spec["required"] and not spec["has_default"]:
            deltas.append(f"input '{name}' added as required (no default)")
        # Existing input tightened to required.
        elif was is not None and spec["required"] and not was["required"]:
            deltas.append(f"input '{name}' changed optional → required")

    old_sec, new_sec = old["secrets"], new["secrets"]
    for name, spec in new_sec.items():
        was = old_sec.get(name)
        if was is None and spec["required"]:
            deltas.append(f"secret '{name}' added as required")
        elif was is not None and spec["required"] and not was["required"]:
            deltas.append(f"secret '{name}' changed optional → required")

    return deltas


_KINDS = ("input", "secret", "output")
_ENTRY_FIELDS = frozenset({"file", "kind", "name", "reason", "checked"})


@dataclass(frozen=True, slots=True)
class Retirement:
    """One interface member a release may stop declaring, and its evidence."""

    file: str
    kind: str
    name: str
    reason: str
    checked: str

    @property
    def member(self) -> tuple[str, str]:
        """The (kind, name) pair this record clears."""
        return (self.kind, self.name)


def parse_retirements(yaml_text: str) -> list[Retirement]:
    """Validated records from `config/retired-interfaces.yaml`.

    Args:
        yaml_text: Contents of the retirement file.

    Returns:
        Every declared retirement, in file order.

    Raises:
        ValueError: On a malformed entry. An entry missing its reason or date
            asserts that nothing consumes the interface without saying how that
            was established, which is a bypass rather than a retirement.
    """
    data = yaml.safe_load(yaml_text) or {}
    # An absent or null key is an empty list; any other shape is a malformed
    # file, which must not read as "no retirements declared".
    raw = [] if data.get("retired") is None else data["retired"]
    if not isinstance(raw, list):
        raise ValueError("`retired` must be a list")

    records: list[Retirement] = []
    for index, entry in enumerate(raw):
        where = f"entry {index}"
        if not isinstance(entry, dict):
            raise ValueError(f"{where} is not a mapping")
        unknown = sorted(set(entry) - _ENTRY_FIELDS)
        if unknown:
            raise ValueError(f"{where} has unknown field(s): {', '.join(unknown)}")
        missing = sorted(f for f in _ENTRY_FIELDS if not str(entry.get(f, "")).strip())
        if missing:
            raise ValueError(f"{where} is missing: {', '.join(missing)}")
        if entry["kind"] not in _KINDS:
            raise ValueError(
                f"{where} has kind '{entry['kind']}', not one of {', '.join(_KINDS)}"
            )
        records.append(
            Retirement(
                file=str(entry["file"]),
                kind=str(entry["kind"]),
                name=str(entry["name"]),
                reason=" ".join(str(entry["reason"]).split()),
                checked=str(entry["checked"]),
            )
        )
    return records


def load_retirements(path: Path) -> list[Retirement]:
    """Retirements declared at `path`; none when the file is absent.

    A missing file means the gate checks everything, which is the safe
    direction to fail in.
    """
    if not path.is_file():
        return []
    return parse_retirements(path.read_text(encoding="utf-8"))


def _declares(iface: dict | None, kind: str, name: str) -> bool:
    """Whether a parsed interface declares this member."""
    return iface is not None and name in iface[f"{kind}s"]


def retirement_state(
    record: Retirement, tree: dict | None, baseline: dict | None
) -> str:
    """Where a retirement stands: pending, retired or prunable.

    Args:
        record: The retirement to classify.
        tree: Parsed interface of its file now, or None if it declares none.
        baseline: The same at the last release tag.

    Returns:
        "pending" while the interface is still declared, "retired" for the
        removal this run clears, "prunable" once a release has shipped without
        it and the entry no longer does anything.
    """
    if _declares(tree, record.kind, record.name):
        return "pending"
    if _declares(baseline, record.kind, record.name):
        return "retired"
    return "prunable"


def removed_pipeline_files(old: set[str], current: set[str]) -> list[str]:
    """Pipeline files present at the last release but gone now.

    A pinned caller references its siblings `@main`; if a referenced composite/
    workflow is deleted or renamed, that `@main` ref 404s and the graph fails
    at startup. Flag any last-release pipeline file missing from the tree.
    """
    return sorted(p for p in old if p not in current)


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
        encoding="utf-8",
        errors="replace",
        cwd=_ROOT,
    )
    return result.stdout.strip() or None if result.returncode == 0 else None


def _file_at(tag: str, rel_path: str) -> str | None:
    result = subprocess.run(
        ["git", "show", f"{tag}:{rel_path}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=_ROOT,
    )
    return result.stdout if result.returncode == 0 else None


def _pipeline_files_at(tag: str) -> set[str]:
    """Pipeline file paths (workflows + composites) present at `tag`."""
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", tag, ".github/"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=_ROOT,
    )
    if result.returncode != 0:
        return set()
    return {
        p
        for p in result.stdout.splitlines()
        if (p.startswith(".github/workflows/") and p.endswith((".yml", ".yaml")))
        or (p.startswith(".github/actions/") and p.endswith("action.yml"))
    }


def _report_retirements(
    retirements: list[Retirement],
    tree_ifaces: dict[str, dict],
    tag_ifaces: dict[str, dict],
    tag: str,
) -> None:
    """Print every retirement and its state, so none of them is silent."""
    if not retirements:
        return
    print(f"\nRetirements honoured ({_RETIREMENTS.relative_to(_ROOT).as_posix()}):")
    prunable: list[Retirement] = []
    for record in retirements:
        state = retirement_state(
            record, tree_ifaces.get(record.file), tag_ifaces.get(record.file)
        )
        print(f"  {state:8} {record.file}: {record.kind} '{record.name}'")
        print(f"           checked {record.checked} — {record.reason}")
        if state == "prunable":
            prunable.append(record)
    if prunable:
        noun = "entry" if len(prunable) == 1 else "entries"
        print(
            f"\n  {len(prunable)} {noun} no longer clear anything — absent from "
            f"{tag} as well as the tree. Delete them from "
            f"{_RETIREMENTS.relative_to(_ROOT).as_posix()}."
        )


def main() -> int:
    """Fail on a backward-incompatible change to a published call interface."""
    tag = _last_release_tag()
    if not tag:
        print("No release tag to compare against — skipping interface gate.")
        return 0

    try:
        retirements = load_retirements(_RETIREMENTS)
    except ValueError as exc:
        print(f"config/retired-interfaces.yaml is invalid: {exc}")
        return 1
    retired_by_file: dict[str, set[tuple[str, str]]] = {}
    for record in retirements:
        retired_by_file.setdefault(record.file, set()).add(record.member)

    print(f"Interface compat gate — working tree vs {tag}\n")
    regressions = 0
    tree_ifaces: dict[str, dict] = {}
    tag_ifaces: dict[str, dict] = {}
    for path in _tracked_files():
        rel = path.relative_to(_ROOT).as_posix()
        new_iface = parse_interface(path.read_text(encoding="utf-8"))
        if new_iface["kind"] == "other":
            continue
        tree_ifaces[rel] = new_iface
        old_text = _file_at(tag, rel)
        if old_text is None:
            print(f"  {rel}: new since {tag} — no baseline, OK")
            continue
        old_iface = parse_interface(old_text)
        if old_iface["kind"] == "other":
            continue
        tag_ifaces[rel] = old_iface
        deltas = breaking_deltas(
            old_iface, new_iface, frozenset(retired_by_file.get(rel, ()))
        )
        if deltas:
            regressions += len(deltas)
            print(f"  ✗ {rel}:")
            for d in deltas:
                print(f"      - {d}")
        else:
            print(f"  ✓ {rel}")

    removed = removed_pipeline_files(
        _pipeline_files_at(tag),
        {p.relative_to(_ROOT).as_posix() for p in _tracked_files()},
    )
    for rel in removed:
        regressions += 1
        print(f"  ✗ {rel}: removed since {tag} (a pinned caller's @main ref 404s)")

    _report_retirements(retirements, tree_ifaces, tag_ifaces, tag)

    if regressions:
        print(
            f"\n{regressions} interface regression(s). A consumer pinned to an "
            f"older caller would fail at startup (issue #31).\n"
            "Make the change additive (optional inputs, keep outputs/secrets), "
            "or cut a deliberate major break."
        )
        return 1
    invoked = workflow_cli_commands(_ROOT)
    published = published_cli_commands()
    if published is None:
        print("\nPublished CLI unreachable -- skipping the subcommand gate.")
    else:
        gaps = cli_command_gaps(invoked, published)
        if gaps:
            print("\nWorkflow calls a subcommand the PUBLISHED CLI lacks:")
            for gap in gaps:
                print(f"  - {gap}")
            print(
                "\nConsumers take the workflow from @main instantly and the CLI "
                "from PyPI, so both halves of one commit do not arrive together. "
                "Release the CLI first, then land the caller."
            )
            return 1
        print(
            f"\nEvery invoked subcommand exists in the published CLI "
            f"({len(published)})."
        )

    print("\nAll interfaces backward-compatible.")
    return 0


# A workflow line invoking the CLI: `${{ env.HYPERCI_INSTALL }} <subcommand>`.
_CLI_CALL = re.compile(r"HYPERCI_INSTALL\s*\}\}\s+([a-z][a-z0-9-]*)")

# Introspects typer's registry rather than scraping `--help`, which HIDES some
# commands (`tag-head` is one) and would report them as missing.
_LIST_COMMANDS = (
    "from hyperi_ci.cli import app; "
    "print(chr(10).join(sorted((c.name or c.callback.__name__).replace('_','-') "
    "for c in app.registered_commands)))"
)


def workflow_cli_commands(root: Path) -> dict[str, set[str]]:
    """Return each workflow's set of invoked ``hyperi-ci`` subcommands."""
    out: dict[str, set[str]] = {}
    workflows = root / ".github" / "workflows"
    if not workflows.is_dir():
        return out
    for path in sorted(workflows.glob("*.yml")):
        found = set(_CLI_CALL.findall(path.read_text(encoding="utf-8")))
        if found:
            out[path.relative_to(root).as_posix()] = found
    return out


_PYPI_JSON = "https://pypi.org/pypi/hyperi-ci/json"


def latest_published_version() -> str | None:
    """The version PyPI serves, or None when it cannot be reached.

    Asked of PyPI rather than left to uv's resolver, which answers from a
    cached index: for some time after a release an unpinned `uvx --from
    hyperi-ci` still runs the PREVIOUS version, even under `--refresh`. This
    gate would then report a shipped subcommand as missing and block every PR.
    """
    try:
        body = url_read(urllib.request.Request(_PYPI_JSON), timeout=15)
        return json.loads(body)["info"]["version"]
    except (*URL_ERRORS, ValueError, KeyError):
        return None


def published_cli_commands() -> set[str] | None:
    """Subcommands the LATEST PUBLISHED CLI exposes, or None when unreachable.

    Reads the published wheel, not the working tree, and that inversion is the
    whole point. Workflows float ``@main`` and reach a consumer instantly; the
    CLI arrives only on a release. A subcommand added in the same commit as its
    caller is therefore missing on every runner until the next publish, which
    is how a Gate job went red across the fleet (issue #181).
    """
    version = latest_published_version()
    if version is None:
        return None
    result = subprocess.run(
        [
            "uvx",
            "--from",
            f"hyperi-ci=={version}",
            "--python",
            "3.14",
            "python",
            "-c",
            _LIST_COMMANDS,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        return None
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def cli_command_gaps(invoked: dict[str, set[str]], published: set[str]) -> list[str]:
    """Subcommands a workflow calls that the published CLI does not have."""
    return sorted(
        f"{workflow}: calls `hyperi-ci {command}`, absent from the published CLI"
        for workflow, commands in invoked.items()
        for command in commands - published
    )


if __name__ == "__main__":
    sys.exit(main())
