# Project:   HyperI CI
# File:      src/hyperi_ci/deps/render.py
# Purpose:   Human-first rendering: actionable at the top, inventory below
# Origin:    Derek's deps automation scripts, merged into hyperi-ci now they are
#            mature enough for people (and hyperi-ai's /deps) to use directly
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Default (non-JSON) output, written for a person at a terminal.

Ordering is the point. Drift, inert surfaces and Renovate blind spots come
FIRST, because they are the only things here that need a decision. The full
inventory, the extracted pins and the per-group tables come after, so nobody
scrolls past thirty green rows to find the one problem.

ASCII only -- no colour codes, no box-drawing. Columns are sized to content so
it stays readable in a normal-width terminal. Any list that gets capped SAYS it
was capped, with the real total: a silently truncated report is worse than no
report, because it reads as complete.
"""

from __future__ import annotations

from hyperi_ci.deps.surfaces import ABSENT, INERT

# Rows printed per surface / per group before the human view says "and N more".
# Lifted entirely by --full; `deps show <id>` never caps.
DETAIL_CAP = 20


def table(headers: list[str], rows: list[list[str]], indent: str = "") -> list[str]:
    """Compact ASCII table lines, columns sized to content."""
    if not rows:
        return []
    widths = [len(head) for head in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    out = [
        indent
        + "  ".join(head.ljust(widths[i]) for i, head in enumerate(headers)).rstrip()
    ]
    for row in rows:
        out.append(
            indent
            + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
        )
    return out


def _rule(title: str) -> list[str]:
    return ["", f"== {title} " + "=" * max(3, 70 - len(title)), ""]


def _cap(rows: list, full: bool) -> tuple[list, int]:
    """Apply the display cap. Returns (shown, hidden)."""
    if full or len(rows) <= DETAIL_CAP:
        return rows, 0
    return rows[:DETAIL_CAP], len(rows) - DETAIL_CAP


def _group_rows(entries: list[dict]) -> list[list[str]]:
    return [
        [
            entry["dep"],
            entry["constraint"] or "-",
            entry["floor"] or "-",
            entry["locked"] or "(not locked)",
            entry["drift"] or "",
            entry["source"] or "",
        ]
        for entry in entries
    ]


_GROUP_HEADERS = ["DEP", "DECLARED", "FLOOR", "LOCKED", "DRIFT", "SRC"]


# ---------------------------------------------------------------------------
# ACTION blocks
# ---------------------------------------------------------------------------


def drift_block(drift_result: dict, full: bool) -> list[str]:
    """Floor drift first: the only thing here that is always a defect."""
    out = ["FLOOR DRIFT -- the lock has moved past what the manifest admits to."]
    if not drift_result["drift"]:
        out.append(
            f"  none. {drift_result['compared']} of {drift_result['declared']} "
            "declared dependencies had both a floor and a locked version."
        )
        out += [f"  note: {note}" for note in drift_result["notes"]]
        return out
    out.append("  Nothing bumps a floor on its own, so this only ever grows. A dev or")
    out.append("  test group here means the test stack has aged out underneath you.")
    shown, hidden = _cap(drift_result["drift"], full)
    # MANIFEST is not decoration: a monorepo has several pyproject.toml files
    # with the same group name, so without it four different repos' stale
    # `dev` extras render as four identical rows.
    rows = [
        [
            item["ecosystem"],
            item["manifest"],
            item["group"],
            item["dep"],
            item["constraint"] or "-",
            item["floor"],
            item["locked"],
            item["drift"],
            item["source"],
        ]
        for item in shown
    ]
    out += table(
        [
            "ECO",
            "MANIFEST",
            "GROUP",
            "DEP",
            "DECLARED",
            "FLOOR",
            "LOCKED",
            "DRIFT",
            "SRC",
        ],
        rows,
        indent="  ",
    )
    if hidden:
        out.append(f"  ... and {hidden} more drifted (capped; --full shows all)")
    out.append(
        f"  {len(drift_result['drift'])} drifted of "
        f"{drift_result['compared']} compared."
    )
    out += [f"  note: {note}" for note in drift_result["notes"]]
    return out


def inert_block(scan_result: dict) -> list[str]:
    """Spell out the false-assurance case -- a human needs the definition."""
    inert = [r for r in scan_result["surfaces"] if r["state"] == INERT]
    out = [
        "INERT SURFACES -- files matched but nothing was extractable, or the",
        "manager has no file patterns at all. NOT clean: go and look.",
    ]
    if not inert:
        out.append("  none.")
        return out
    for record in inert:
        out.append(f"  {record['id']}  ({len(record['files'])} file(s) matched)")
        note = record["caveat"] or record["gap"] or record["notes"]
        if note:
            out.append(f"    {note}")
    return out


def gaps_block(gaps_result: dict) -> list[str]:
    """Renovate coverage boundary -- what no bot will ever raise a PR for."""
    out = ["RENOVATE BLIND SPOTS -- present surfaces no enabled manager sees."]
    if gaps_result["config"] is None:
        out.append("  no renovate config in this repo, so every surface below.")
    else:
        managers = gaps_result["enabled_managers"]
        shown = ", ".join(managers) if managers else "(unset: Renovate defaults)"
        out.append(f"  config: {gaps_result['config']}  enabledManagers: {shown}")
    if not gaps_result["uncovered"]:
        out.append("  none -- every present surface has an enabled manager.")
        return out
    rows = [
        [item["id"], item["state"], item["renovate_manager"] or "NONE", item["reason"]]
        for item in gaps_result["uncovered"]
    ]
    out += table(["SURFACE", "STATE", "MANAGER", "WHY IT IS MISSED"], rows, indent="  ")
    return out


def unclassified_block(scan_result: dict) -> list[str]:
    """Version-bearing files no surface claimed -- the next catalogue entry."""
    unclassified = scan_result["unclassified"]
    out = ["UNCLASSIFIED -- look version-bearing, no surface claimed them."]
    if not unclassified["total"]:
        out.append("  none.")
        return out
    out.append(f"  {unclassified['total']} file(s):")
    out += [f"    {rel}" for rel in unclassified["shown"]]
    if unclassified["capped"]:
        remainder = unclassified["total"] - unclassified["cap"]
        out.append(
            f"    ... {remainder} more not shown (list capped at "
            f"{unclassified['cap']}, not silently truncated)"
        )
    return out


# ---------------------------------------------------------------------------
# INVENTORY / PINS / GROUPS
# ---------------------------------------------------------------------------


def inventory(scan_result: dict) -> list[str]:
    """Present surfaces in a table; absent ones collapsed to one line."""
    present = [r for r in scan_result["surfaces"] if r["state"] != ABSENT]
    absent = [r["id"] for r in scan_result["surfaces"] if r["state"] == ABSENT]
    rows = []
    for record in present:
        if record["pins"]:
            detail = f"{len(record['pins'])} pins"
        elif record["groups"] and record["files"]:
            detail = f"{len(record['groups'])} groups"
        else:
            detail = "-"
        rows.append(
            [
                record["state"],
                record["id"],
                record["kind"],
                "yes" if record["resolver"] else "no",
                str(len(record["files"])),
                detail,
                record["renovate_manager"] or "NONE",
            ]
        )
    out = table(
        ["STATE", "SURFACE", "KIND", "RESOLVER", "FILES", "EXTRACTED", "RENOVATE"],
        rows,
    ) or ["No surface matched anything in this repo."]
    out.append("")
    out.append("RESOLVER=no means no lockfile ever moves that pin -- only a human or a")
    out.append("bot does, which is how a surface rots without anyone noticing.")
    if absent:
        out.append("")
        out.append(f"absent ({len(absent)}): " + ", ".join(absent))
    return out


def pins(scan_result: dict, full: bool) -> list[str]:
    """Every extracted pin and its current value, grouped by surface."""
    out: list[str] = []
    for record in scan_result["surfaces"]:
        if not record["pins"]:
            continue
        out.append(f"{record['id']} -- {record['label']}")
        shown, hidden = _cap(record["pins"], full)
        rows = [
            [f"{pin['file']}:{pin['line']}", pin["dep"] or "-", pin["version"]]
            for pin in shown
        ]
        out += table(["FILE:LINE", "DEP", "VERSION"], rows, indent="  ")
        if hidden:
            out.append(
                f"  ... and {hidden} more (capped; `hyperi-ci deps show "
                f"{record['id']}` lists every one)"
            )
        out.append("")
    return out or ["No embedded pins extracted."]


def groups(drift_result: dict, full: bool) -> list[str]:
    """Show the declared constraint beside the locked version, per group."""
    out: list[str] = []
    for eco in drift_result["ecosystems"]:
        lock = eco["lock"] or "(no lock found)"
        out.append(f"{eco['name']}  {eco['manifest']} -> {lock}")
        for group in eco["groups"]:
            out.append(f"  {group['group']}  ({len(group['entries'])})")
            shown, hidden = _cap(group["entries"], full)
            out += table(_GROUP_HEADERS, _group_rows(shown), indent="    ")
            if hidden:
                out.append(f"    ... and {hidden} more (capped; --full shows all)")
        out.append("")
    return out or ["No pyproject.toml / Cargo.toml / package.json in this repo."]


# ---------------------------------------------------------------------------
# Whole-report renderers
# ---------------------------------------------------------------------------


def report(payload: dict, full: bool = False) -> str:
    """Render the default view: what needs attention, then the full picture."""
    scan_result = payload["scan"]
    drift_result = payload["drift"]
    gaps_result = payload["gaps"]
    out = [
        f"deps: {payload['root']}",
        f"{scan_result['files_scanned']} file(s) via {scan_result['file_source']}"
        + (f"  [kind={payload['kind_filter']}]" if payload["kind_filter"] else ""),
    ]

    out += _rule("ACTION")
    out += drift_block(drift_result, full)
    out += [""] + inert_block(scan_result)
    out += [""] + gaps_block(gaps_result)
    out += [""] + unclassified_block(scan_result)

    out += _rule("INVENTORY")
    out += inventory(scan_result)

    out += _rule("PINS -- versions embedded in a file, by surface")
    out += pins(scan_result, full)

    out += _rule("GROUPS -- declared constraint vs locked, by ecosystem")
    out += groups(drift_result, full)

    if not full:
        out += [
            "",
            f"Detail lists are capped at {DETAIL_CAP} rows. --full lifts the cap;",
            "`hyperi-ci deps show <surface>` dumps one surface uncapped.",
        ]
    return "\n".join(out)


def drift_only(drift_result: dict, full: bool = False) -> str:
    """Render the drift slice alone, for scripting or a focused look."""
    return "\n".join(
        [f"deps drift: {drift_result['root']}", ""] + drift_block(drift_result, full)
    )


def gaps_only(gaps_result: dict) -> str:
    """Render the Renovate-gap slice alone."""
    return "\n".join(
        [f"deps gaps: {gaps_result['root']}", ""] + gaps_block(gaps_result)
    )


def show(detail: dict) -> str:
    """Full detail for one surface. Never capped -- that is the whole point."""
    if "error" in detail:
        return f"deps show: {detail['error']}\nknown: " + ", ".join(detail["known"])
    record = detail["surface"]
    registry = detail["registry"]
    out = [
        f"deps show: {record['id']} -- {record['label']}",
        f"root: {detail['root']}",
        f"state: {record['state']}   kind: {record['kind']}   "
        f"resolver: {'yes' if record['resolver'] else 'no'}",
        f"renovate manager: {registry['renovate_manager'] or 'NONE'}",
    ]
    if detail["renovate"]:
        out.append(f"renovate: UNCOVERED -- {detail['renovate']['reason']}")
    for key in ("gap", "caveat", "notes"):
        if registry[key]:
            out.append(f"{key}: {registry[key]}")

    out += ["", "patterns:"]
    out += [f"  {p}" for p in registry["patterns"]] or [
        "  (none -- this surface can never match a file)"
    ]
    if registry["lock"]:
        out.append("lockfiles: " + ", ".join(registry["lock"]))

    out += ["", f"files matched ({len(record['files'])}):"]
    out += [f"  {rel}" for rel in record["files"]] or ["  none"]

    out += ["", f"pins ({len(record['pins'])}):"]
    if record["pins"]:
        rows = [
            [f"{pin['file']}:{pin['line']}", pin["dep"] or "-", pin["version"]]
            for pin in record["pins"]
        ]
        out += table(["FILE:LINE", "DEP", "VERSION"], rows, indent="  ")
    else:
        out.append("  none extracted")

    for eco in detail["ecosystems"]:
        out += ["", f"{eco['manifest']} -> {eco['lock'] or '(no lock found)'}"]
        for group in eco["groups"]:
            out.append(f"  {group['group']}  ({len(group['entries'])})")
            out += table(_GROUP_HEADERS, _group_rows(group["entries"]), indent="    ")
    return "\n".join(out)
