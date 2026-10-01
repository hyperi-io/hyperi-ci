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
removed input/output/secret, a newly-required input, or optional->required. Run
in hyperi-ci's own CI so a break is caught before it ever reaches a consumer.

Comparing interface to interface cannot express "nothing consumes this", so a
deliberate retirement reads as a regression too. `config/retired-interfaces.yaml`
carries the evidence for one, and this gate honours and reports it.

It also reads every call to the PyPI CLI in the workflows and composites, and
fails on a subcommand or option the latest release does not have. Consumers
take the workflow from @main at once and the CLI only when it is released.

Usage:  uv run scripts/check-workflow-interfaces.py
Exit 1 if any interface regressed; 0 otherwise.
"""

import json
import re
import shlex
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
    return _cli_gate()


def _cli_gate() -> int:
    """Fail when a workflow calls a subcommand or option PyPI does not ship."""
    scan = workflow_cli_invocations(_ROOT)
    for note in scan.unchecked:
        print(f"  not checked: {note}")
    try:
        published = published_cli_options()
    except CliDumpError as exc:
        print(f"\nThe published CLI's option dump failed: {exc}")
        print("That is a typer/click API break in the dump itself, not a PyPI outage.")
        return 1
    if published is None:
        print("\nPublished CLI unreachable -- skipping the subcommand gate.")
    else:
        gaps = cli_invocation_gaps(scan.invocations, published)
        if gaps:
            print("\nWorkflow calls a subcommand or option the PUBLISHED CLI lacks:")
            for gap in gaps:
                print(f"  - {gap}")
            print(
                "\nConsumers take the workflow from @main instantly and the CLI "
                "from PyPI, so both halves of one commit do not arrive together. "
                "Release the CLI first, then land the caller."
            )
            return 1
        print(
            f"\nEvery invoked subcommand and option exists in the published CLI "
            f"({len(scan.invocations)} calls checked)."
        )

    print("\nAll interfaces backward-compatible.")
    return 0


@dataclass(frozen=True, slots=True)
class Invocation:
    """One call to the published CLI, as the shell would pass its arguments."""

    file: str
    line: int
    args: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InvocationScan:
    """Every call found, plus the lines that name the CLI but could not be read."""

    invocations: tuple[Invocation, ...]
    unchecked: tuple[str, ...]


# GitHub substitutes `${{ }}` before the shell runs, so each one is swapped for
# a placeholder word before the line is split.
_GH_EXPR = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
_EXPR_REF = re.compile(r"__GHEXPR(\d+)__")
_EXPR_LITERAL = re.compile(r"'((?:[^']|'')*)'")
_INSTALL_EXPR = "env.HYPERCI_INSTALL"
_INSTALL_VARS = frozenset({"$HYPERCI_INSTALL", "${HYPERCI_INSTALL}"})
_OPAQUE = "<expr>"

# Words that may precede the command itself in a simple command.
_COMMAND_PREFIXES = frozenset(
    {"if", "then", "else", "elif", "do", "while", "until", "!", "time", "exec"}
)
_SHELL_SEPARATORS = frozenset({";", "&", "&&", "|", "||", "(", ")", ";;"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# uvx options that consume the next word, so it is not read as the package.
_UVX_VALUE_OPTIONS = frozenset(
    {
        "--python",
        "-p",
        "--with",
        "--with-editable",
        "--with-requirements",
        "--index",
        "--default-index",
        "--index-url",
        "--extra-index-url",
        "--find-links",
        "-f",
        "--cache-dir",
        "--python-preference",
        "--exclude-newer",
        "--config-file",
        "--directory",
        "--project",
        "--color",
        "--constraints",
        "-c",
        "--overrides",
    }
)


def _expression_words(expr: str) -> list[str]:
    """Words an expression can expand to that the CLI would read as options.

    `${{ cond && '--tier full' || '' }}` passes `--tier` on one branch, so a
    literal that opens with an option counts. Anything else is an opaque value.
    """
    words: list[str] = []
    for literal in _EXPR_LITERAL.findall(expr):
        split = literal.replace("''", "'").split()
        if split and split[0].startswith("-"):
            words.extend(split)
    return words or [_OPAQUE]


def _logical_lines(script: str) -> list[tuple[int, str]]:
    """Split shell text into (line offset, command line) pairs.

    A newline inside quotes or after a backslash does not end a command, and a
    `#` comment is dropped so an apostrophe in it cannot open a quote.
    """
    out: list[tuple[int, str]] = []
    buf: list[str] = []
    quote: str | None = None
    line = start = 0
    i = 0
    while i < len(script):
        ch = script[i]
        nxt = script[i + 1] if i + 1 < len(script) else ""
        if ch == "\\" and quote != "'" and nxt:
            if nxt == "\n":
                buf.append(" ")
                line += 1
            else:
                buf.append(ch + nxt)
            i += 2
            continue
        if ch == "#" and quote is None and (not buf or buf[-1] in " \t;&|("):
            while i < len(script) and script[i] != "\n":
                i += 1
            continue
        if ch in "'\"" and quote in (None, ch):
            quote = None if quote else ch
        if ch == "\n":
            line += 1
            if quote is None:
                out.append((start, "".join(buf)))
                buf, start = [], line
                i += 1
                continue
        buf.append(ch)
        i += 1
    out.append((start, "".join(buf)))
    return out


def _simple_commands(words: list[str]) -> list[list[str]]:
    """Split shell words into simple commands at `;`, `&&`, `|` and friends."""
    commands: list[list[str]] = [[]]
    for word in words:
        if word in _SHELL_SEPARATORS:
            commands.append([])
        else:
            commands[-1].append(word)
    return [c for c in commands if c]


def _uvx_cli_args(words: list[str]) -> list[str] | None:
    """Arguments after `uvx ... hyperi-ci`, or None unless it runs latest PyPI.

    `uvx hyperi-ci==X` and `uvx --from <spec>` run something other than the
    latest release, so the gate has nothing to compare them with.
    """
    i = 1
    while i < len(words):
        word = words[i]
        if word == "--from" or word.startswith("--from="):
            return None
        if not word.startswith("-"):
            return words[i + 1 :] if word == "hyperi-ci" else None
        i += 2 if word in _UVX_VALUE_OPTIONS else 1
    return None


def _cli_args(command: list[str], exprs: list[str]) -> list[str] | None:
    """The CLI's arguments when this simple command runs the published CLI."""
    i = 0
    while i < len(command) and (
        command[i] in _COMMAND_PREFIXES or _ASSIGNMENT.match(command[i])
    ):
        i += 1
    if i == len(command):
        return None
    head = command[i]
    rest = command[i + 1 :]
    match = _EXPR_REF.fullmatch(head)
    if match and exprs[int(match.group(1))].strip() == _INSTALL_EXPR:
        args = rest
    elif head in _INSTALL_VARS:
        args = rest
    elif head == "uvx":
        uvx_args = _uvx_cli_args(command[i:])
        if uvx_args is None:
            return None
        args = uvx_args
    else:
        return None
    expanded: list[str] = []
    for word in args:
        match = _EXPR_REF.fullmatch(word)
        if match:
            expanded.extend(_expression_words(exprs[int(match.group(1))]))
        elif "__GHEXPR" in word:
            expanded.append(_OPAQUE)
        else:
            expanded.append(word)
    return expanded


def _shell_scripts(yaml_text: str) -> list[tuple[int, str]]:
    """Every shell `run:` value in a workflow or action, with its first line.

    Line numbers are 1-based. A block scalar's text starts on the line after
    its `|` or `>` indicator. A step with a non-shell `shell:` is skipped.
    """
    root = yaml.compose(yaml_text, Loader=yaml.SafeLoader)
    out: list[tuple[int, str]] = []
    stack = [root] if root is not None else []
    while stack:
        node = stack.pop()
        if isinstance(node, yaml.MappingNode):
            pairs = {
                k.value: v for k, v in node.value if isinstance(k, yaml.ScalarNode)
            }
            shell = pairs.get("shell")
            program = str(shell.value).split()[:1] if shell is not None else []
            is_shell = program in ([], ["bash"], ["sh"])
            run = pairs.get("run")
            if isinstance(run, yaml.ScalarNode) and is_shell:
                block = run.style in ("|", ">")
                out.append((run.start_mark.line + 1 + block, run.value))
            stack.extend(v for _, v in node.value)
        elif isinstance(node, yaml.SequenceNode):
            stack.extend(node.value)
    return sorted(out)


def cli_invocations(rel: str, yaml_text: str) -> InvocationScan:
    """Calls to the latest published CLI in one workflow or action file.

    A call is `${{ env.HYPERCI_INSTALL }}`, `$HYPERCI_INSTALL` or an unpinned
    `uvx ... hyperi-ci` in command position. `uv run hyperi-ci` is not one:
    it runs the checkout's own CLI, which is never behind the workflow.

    Args:
        rel: Repo-relative path, used in every location reported.
        yaml_text: Contents of the file.

    Returns:
        The calls found, and the lines that name the CLI but could not be split.
    """
    invocations: list[Invocation] = []
    unchecked: list[str] = []
    for first_line, script in _shell_scripts(yaml_text):
        exprs = _GH_EXPR.findall(script)
        counter = iter(range(len(exprs)))
        text = _GH_EXPR.sub(lambda _: f"__GHEXPR{next(counter)}__", script)
        for offset, line in _logical_lines(text):
            where = first_line + offset
            lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            try:
                words = list(lexer)
            except ValueError as exc:
                if (
                    "HYPERCI_INSTALL" in line
                    or "hyperi-ci" in line
                    or any(
                        exprs[int(n)].strip() == _INSTALL_EXPR
                        for n in _EXPR_REF.findall(line)
                    )
                ):
                    unchecked.append(f"{rel}:{where}: {exc}")
                continue
            for command in _simple_commands(words):
                args = _cli_args(command, exprs)
                if args is not None:
                    invocations.append(Invocation(rel, where, tuple(args)))
    return InvocationScan(tuple(invocations), tuple(unchecked))


def workflow_cli_invocations(root: Path) -> InvocationScan:
    """Calls to the published CLI across every workflow and composite action."""
    invocations: list[Invocation] = []
    unchecked: list[str] = []
    files = sorted((root / ".github" / "workflows").glob("*.yml"))
    files += sorted((root / ".github" / "actions").glob("*/action.yml"))
    for path in files:
        scan = cli_invocations(
            path.relative_to(root).as_posix(), path.read_text(encoding="utf-8")
        )
        invocations.extend(scan.invocations)
        unchecked.extend(scan.unchecked)
    return InvocationScan(tuple(invocations), tuple(unchecked))


@dataclass(frozen=True, slots=True)
class CommandOptions:
    """Every option name one published command accepts, and which take a value.

    A value-taking option given as `--opt value` consumes the next word too;
    `--opt=value` and a flag do not. `names` holds every spelling (`--opt` and
    a short `-o`), `value_taking` the subset that eats the next word.
    """

    names: frozenset[str]
    value_taking: frozenset[str]


_NO_OPTIONS = CommandOptions(names=frozenset(), value_taking=frozenset())


def _option_name(word: str) -> str:
    """The option a word names: `--x=1` is `--x`, `-xVALUE` is `-x`."""
    if word.startswith("--"):
        return word.split("=", 1)[0]
    return word[:2]


def _is_group(path: str, published: dict[str, CommandOptions]) -> bool:
    """Whether the command at `path` has subcommands of its own."""
    prefix = f"{path} " if path else ""
    return any(key != path and key.startswith(prefix) for key in published)


def _call_gaps(call: Invocation, published: dict[str, CommandOptions]) -> list[str]:
    """What one call uses that the published CLI lacks."""
    where = f"{call.file}:{call.line}"
    gaps: list[str] = []
    path = ""
    skip_value = False
    for word in call.args:
        if skip_value:
            skip_value = False
            continue
        shown = f"hyperi-ci {path}".strip()
        if word == "--":
            break
        if word.startswith("-") and word != "-":
            option = _option_name(word)
            opts = published.get(path, _NO_OPTIONS)
            if option not in opts.names:
                gaps.append(
                    f"{where}: `{shown}` passes `{option}`, which the published "
                    "CLI does not accept"
                )
            elif word == option and option in opts.value_taking:
                skip_value = True
            continue
        child = f"{path} {word}".strip()
        if child in published:
            path = child
        elif not _is_group(path, published):
            continue
        elif word == _OPAQUE:
            gaps.append(
                f"{where}: the subcommand after `{shown}` comes from an "
                "expression, so the gate cannot check it"
            )
            break
        else:
            gaps.append(
                f"{where}: calls `hyperi-ci {child}`, absent from the published CLI"
            )
            break
    return gaps


def cli_invocation_gaps(
    invocations: tuple[Invocation, ...] | list[Invocation],
    published: dict[str, CommandOptions],
) -> list[str]:
    """Subcommands and options a workflow uses that the published CLI lacks.

    Args:
        invocations: Calls found in the workflows.
        published: Option names per command path, `""` for the root and
            `"publish binaries"` for a nested command.

    Returns:
        One message per gap, naming the file, line, command and option.
    """
    return sorted({gap for call in invocations for gap in _call_gaps(call, published)})


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


# Walks the click model typer builds rather than scraping `--help`, which HIDES
# some commands (`tag-head` is one) and wraps option names across lines. Any
# failure inside the try is a typer/click API break, not a missing wheel --
# it prints the marker `_DUMP_FAILURE_MARKER` and exits `_DUMP_FAILURE_CODE`
# so the caller can tell that apart from uvx itself failing to fetch the wheel.
_DUMP_FAILURE_MARKER = "DUMP_OPTIONS_FAILED"
_DUMP_FAILURE_CODE = 3
_DUMP_OPTIONS = f"""
import json
import sys

try:
    import typer.main
    from hyperi_ci.cli import app

    def walk(cmd, path, out):
        ctx = cmd.context_class(cmd, info_name=path[-1] if path else "hyperi-ci")
        specs = {{}}
        for param in cmd.get_params(ctx):
            if param.param_type_name == "option":
                takes_value = not param.is_flag
                for name in (*param.opts, *param.secondary_opts):
                    specs[name] = takes_value
        out[" ".join(path)] = specs
        for name, sub in getattr(cmd, "commands", {{}}).items():
            walk(sub, [*path, name], out)

    table = {{}}
    walk(typer.main.get_command(app), [], table)
    print(json.dumps(table))
except Exception as exc:
    print(f"{_DUMP_FAILURE_MARKER}: {{exc!r}}", file=sys.stderr)
    sys.exit({_DUMP_FAILURE_CODE})
"""


class CliDumpError(RuntimeError):
    """The published CLI's own introspection snippet ran and failed -- a fail, not a skip."""


def parse_option_table(text: str) -> dict[str, CommandOptions]:
    """Command paths to their options, from the JSON `_DUMP_OPTIONS` prints.

    Raises:
        ValueError: On anything but a mapping of path to {name: takes_value}.
    """
    data = json.loads(text)
    if not isinstance(data, dict) or not all(
        isinstance(v, dict) for v in data.values()
    ):
        raise ValueError("option table is not a mapping of path to option specs")
    table: dict[str, CommandOptions] = {}
    for path, specs in data.items():
        if not all(
            isinstance(k, str) and isinstance(v, bool) for k, v in specs.items()
        ):
            raise ValueError(
                f"option spec for '{path}' is not a mapping of name to bool"
            )
        table[str(path)] = CommandOptions(
            names=frozenset(specs),
            value_taking=frozenset(
                name for name, takes_value in specs.items() if takes_value
            ),
        )
    return table


def published_cli_options() -> dict[str, CommandOptions] | None:
    """Every command and its options in the LATEST PUBLISHED CLI, or None.

    Reads the published wheel, not the working tree, and that inversion is the
    whole point. Workflows float ``@main`` and reach a consumer instantly; the
    CLI arrives only on a release. A subcommand or option added in the same
    commit as its caller is therefore missing on every runner until the next
    publish, which is how a Gate job went red across the fleet (issue #181).

    Returns:
        Options keyed by command path (`""` is the root), or None when PyPI
        or the wheel cannot be reached.

    Raises:
        CliDumpError: The wheel installed and ran, but the dump snippet itself
            raised (a typer/click API change) or printed something the gate
            cannot parse. A broken dump must fail the gate, not skip it.
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
            _DUMP_OPTIONS,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if (
        result.returncode == _DUMP_FAILURE_CODE
        and _DUMP_FAILURE_MARKER in result.stderr
    ):
        raise CliDumpError(f"the option dump raised: {result.stderr.strip()}")
    if result.returncode != 0:
        return None
    try:
        return parse_option_table(result.stdout)
    except ValueError as exc:
        raise CliDumpError(
            f"the option dump printed unparseable output: {exc}"
        ) from exc


if __name__ == "__main__":
    sys.exit(main())
