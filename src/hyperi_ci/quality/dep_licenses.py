# Project:   HyperI CI
# File:      src/hyperi_ci/quality/dep_licenses.py
# Purpose:   Judge each shipped dependency's licence against the packaged policy
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Judge each shipped dependency's licence against the packaged policy.

The policy (``config/license-policy.yaml``) maps an SPDX id and a consumption
mode to a verdict and its obligations. Detection belongs to each language;
this module turns what a tool reported into a :class:`Finding` an agent can
cite, so a licence verdict never comes from anyone's memory.
"""

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from packaging.licenses import InvalidLicenseExpression, canonicalize_license_expression

from hyperi_ci import __version__
from hyperi_ci.common import error, info, warn
from hyperi_ci.config import CIConfig

_POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "license-policy.yaml"

REPORT_PATH = Path(".hyperi-ci") / "licenses.json"

# A repo opts in to attribution by committing this file. The generated copy is
# written beside the report every run, so it can be copied over.
NOTICES_PATH = Path("THIRD-PARTY-NOTICES")
NOTICES_OUTPUT = REPORT_PATH.parent / "THIRD-PARTY-NOTICES"

# Licence file lines quoted when metadata declares nothing.
_QUOTE_LINES = 3

_SEPARATORS = re.compile(r"[-_.]+")

# Metadata values that mean "no licence declared".
_UNDECLARED = frozenset({"", "UNKNOWN", "NONE", "NOASSERTION"})

SCHEMA_VERSION = 1


class Mode(StrEnum):
    """How a dependency reaches what ships."""

    IMPORT = "import"
    STATIC = "static"
    VENDORED = "vendored"
    MODIFIED = "modified"


class Verdict(StrEnum):
    """The policy's answer, in order from best to worst."""

    ALLOW = "allow"
    OBLIGATIONS = "allow-with-obligations"
    ASK = "ask"
    DENY = "deny"


_RANK = {verdict: rank for rank, verdict in enumerate(Verdict)}


class PolicyError(ValueError):
    """The policy table or a repo's override of it is malformed."""


@dataclass(frozen=True, slots=True)
class Rule:
    """One cell of the policy: a verdict and the obligations it carries."""

    verdict: Verdict
    obligations: tuple[str, ...] = ()
    group: str = ""


@dataclass(frozen=True, slots=True)
class Policy:
    """The loaded policy table.

    Attributes:
        groups: Group name to its rule per mode.
        group_of: Canonical SPDX id or alias to its group name.
        canonical: Alias to the SPDX id it stands for.
        obligations: Obligation key to its description.
        exceptions: ``WITH`` exceptions judged as their base licence.
        digest: sha256 of the effective policy, so a citation names the table it used.
        override_only: Groups whose verdict holds only when an override named them.

    """

    groups: Mapping[str, Mapping[Mode, Rule]]
    group_of: Mapping[str, str]
    canonical: Mapping[str, str]
    obligations: Mapping[str, str]
    exceptions: frozenset[str]
    digest: str
    override_only: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class Override:
    """A repo's answer for one dependency the tools could not settle."""

    name: str
    licence: str
    reason: str
    version: str | None = None
    # A repo file holding the licence text, for a package that ships none.
    licence_file: str | None = None


@dataclass(frozen=True, slots=True)
class Finding:
    """One dependency's licence verdict and where it came from."""

    ecosystem: str
    name: str
    version: str
    spdx: str | None
    source: str
    evidence: str
    mode: Mode
    verdict: Verdict
    obligations: tuple[str, ...] = field(default_factory=tuple)
    rule: str = ""

    def line(self) -> str:
        """Return the one-line form a citation quotes verbatim."""
        obligations = f" [{', '.join(self.obligations)}]" if self.obligations else ""
        return (
            f"licence: {self.ecosystem} {self.name} {self.version} "
            f"{self.spdx or 'unknown'} {self.mode} -> {self.verdict}{obligations} "
            f"(rule {self.rule or 'none'}, source {self.source})"
        )


def _parse_rule(raw: Any, group: str, obligations: Mapping[str, str]) -> Rule:
    if not isinstance(raw, Mapping) or "verdict" not in raw:
        raise PolicyError(f"group {group!r}: each mode needs a `verdict`")
    try:
        verdict = Verdict(raw["verdict"])
    except ValueError as exc:
        raise PolicyError(
            f"group {group!r}: unknown verdict {raw['verdict']!r}"
        ) from exc
    keys = tuple(raw.get("obligations") or ())
    unknown = [key for key in keys if key not in obligations]
    if unknown:
        raise PolicyError(f"group {group!r}: unknown obligations {unknown}")
    if verdict is Verdict.OBLIGATIONS and not keys:
        raise PolicyError(f"group {group!r}: {verdict} names no obligations")
    return Rule(verdict=verdict, obligations=keys, group=group)


def _parse_modes(
    raw: Any, group: str, obligations: Mapping[str, str]
) -> dict[Mode, Rule]:
    if not isinstance(raw, Mapping):
        raise PolicyError(f"group {group!r}: `modes` must be a mapping")
    missing = [mode.value for mode in Mode if mode.value not in raw]
    if missing:
        raise PolicyError(f"group {group!r}: missing modes {missing}")
    return {mode: _parse_rule(raw[mode.value], group, obligations) for mode in Mode}


def load_policy(
    path: Path | None = None, repo_policy: Mapping[str, Any] | None = None
) -> Policy:
    """Load the policy table, with a repo's per-group mode overrides applied.

    Args:
        path: The policy YAML, default the packaged one.
        repo_policy: ``quality.dep_licenses.policy`` from .hyperi-ci.yaml: group
            name to mode to ``{verdict, obligations, reason}``. Each entry needs
            a ``reason``.

    Returns:
        The effective policy.

    Raises:
        PolicyError: The table or the override is malformed.

    """
    raw = yaml.safe_load((path or _POLICY_PATH).read_text(encoding="utf-8"))
    obligations: dict[str, str] = dict(raw.get("obligations") or {})
    groups: dict[str, dict[Mode, Rule]] = {}
    group_of: dict[str, str] = {}
    canonical: dict[str, str] = {}
    override_only: set[str] = set()
    for name, body in (raw.get("groups") or {}).items():
        ids = list(body.get("ids") or [])
        if not ids:
            raise PolicyError(f"group {name!r}: no `ids`")
        groups[name] = _parse_modes(body.get("modes"), name, obligations)
        if body.get("override_only") is True:
            override_only.add(name)
        aliases = body.get("aliases") or {}
        if not isinstance(aliases, Mapping):
            raise PolicyError(f"group {name!r}: `aliases` maps a name to its SPDX id")
        for key in [*ids, *aliases]:
            if key in group_of:
                raise PolicyError(f"{key!r} is in both {group_of[key]!r} and {name!r}")
            group_of[key] = name
        canonical.update({alias: str(spdx) for alias, spdx in aliases.items()})

    for name, modes in (repo_policy or {}).items():
        if name not in groups:
            raise PolicyError(f"dep_licenses.policy: unknown group {name!r}")
        for mode_name, cell in (modes or {}).items():
            if (
                not isinstance(cell, Mapping)
                or not str(cell.get("reason") or "").strip()
            ):
                raise PolicyError(
                    f"dep_licenses.policy.{name}.{mode_name}: a `reason` is required"
                )
            try:
                mode = Mode(mode_name)
            except ValueError as exc:
                raise PolicyError(
                    f"dep_licenses.policy: unknown mode {mode_name!r}"
                ) from exc
            groups[name][mode] = _parse_rule(cell, name, obligations)

    effective = {
        name: {
            mode.value: [rule.verdict.value, *rule.obligations]
            for mode, rule in modes.items()
        }
        for name, modes in groups.items()
    }
    exceptions = frozenset(str(e) for e in raw.get("exceptions") or ())
    digest = hashlib.sha256(
        json.dumps(
            [effective, group_of, sorted(exceptions), sorted(override_only)],
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return Policy(
        groups=groups,
        group_of=group_of,
        canonical=canonical,
        obligations=obligations,
        exceptions=exceptions,
        digest=digest,
        override_only=frozenset(override_only),
    )


def parse_overrides(raw: Iterable[Any] | None) -> list[Override]:
    """Parse ``quality.dep_licenses.overrides`` from .hyperi-ci.yaml.

    Args:
        raw: A list of ``{name, licence, reason, version?, licence_file?}``
            mappings.

    Returns:
        The overrides.

    Raises:
        PolicyError: An entry lacks a name, licence or reason.

    """
    overrides = []
    for entry in raw or []:
        if not isinstance(entry, Mapping):
            raise PolicyError("dep_licenses.overrides: each entry must be a mapping")
        name, licence, reason = (
            str(entry.get(key) or "").strip() for key in ("name", "licence", "reason")
        )
        if not (name and licence and reason):
            raise PolicyError(
                f"dep_licenses.overrides: {entry!r} needs name, licence and reason"
            )
        version = entry.get("version")
        licence_file = str(entry.get("licence_file") or "").strip() or None
        overrides.append(
            Override(
                name, licence, reason, str(version) if version else None, licence_file
            )
        )
    return overrides


def _package_key(ecosystem: str, name: str) -> str:
    """Return the name as the ecosystem's registry compares it."""
    if ecosystem == "pypi":
        # PEP 503: `-`, `_` and `.` runs are one separator, any case.
        return _SEPARATORS.sub("-", name).lower()
    if ecosystem == "crates":
        # crates.io refuses a name differing from another only by `-` or `_`.
        return name.replace("_", "-").lower()
    # npm and copied-in paths: `foo-bar` and `foo_bar` are different packages.
    return name


def find_override(
    overrides: Sequence[Override], ecosystem: str, name: str, version: str
) -> Override | None:
    """Return the override for this dependency, a version-pinned one first."""
    key = _package_key(ecosystem, name)
    named = [o for o in overrides if _package_key(ecosystem, o.name) == key]
    pinned = [o for o in named if o.version == version]
    if pinned:
        return pinned[0]
    return next((o for o in named if o.version is None), None)


def _alias_expression(text: str, policy: Policy) -> str | None:
    """Return a flat OR or AND expression with each alias swapped for its SPDX id.

    None when the text mixes operators, has brackets, or any term is neither
    an alias nor a valid SPDX id.
    """
    if "(" in text or ")" in text:
        return None
    operators = [op for op in (" OR ", " AND ") if op in text]
    if len(operators) != 1:
        return None
    resolved = []
    for term in text.split(operators[0]):
        term = term.strip()
        spdx = policy.canonical.get(term)
        if spdx is None:
            try:
                spdx = canonicalize_license_expression(term)
            except InvalidLicenseExpression:
                return None
        resolved.append(spdx)
    return operators[0].join(resolved)


def _term_rule(term: str, policy: Policy, mode: Mode) -> tuple[str, Rule | None]:
    """Return the SPDX id and policy rule for one licence id or alias.

    ``A WITH exception`` is judged as ``A`` when the policy lists the exception,
    and is unrecognised otherwise.
    """
    base, _, exception = term.partition(" WITH ")
    if exception:
        if exception.strip() not in policy.exceptions:
            return term, None
        _, rule = _term_rule(base.strip(), policy, mode)
        return term, rule
    spdx = policy.canonical.get(term)
    if spdx is None:
        try:
            spdx = canonicalize_license_expression(term)
        except InvalidLicenseExpression:
            spdx = term
    group = policy.group_of.get(term) or policy.group_of.get(spdx)
    if group is None:
        return spdx, None
    return spdx, policy.groups[group][mode]


def judge(
    raw_licence: str | None, mode: Mode, policy: Policy
) -> tuple[str | None, Verdict, tuple[str, ...], str]:
    """Return the SPDX id, verdict, obligations and deciding rule for a licence.

    ``A OR B`` takes the best of its terms and ``A AND B`` the worst. A
    parenthesised or mixed expression, an undeclared licence, an id no group
    lists and an exception the policy does not list are all ask.

    Args:
        raw_licence: The licence as metadata gave it: an SPDX expression, a
            trove classifier name, or several joined with ``"; "``.
        mode: How the dependency reaches what ships.
        policy: The effective policy.

    Returns:
        ``(spdx, verdict, obligations, rule)``.

    """
    text = (raw_licence or "").strip()
    if text.upper() in _UNDECLARED:
        return None, Verdict.ASK, (), "undeclared"

    # Several classifiers on one package mean the user may pick any of them.
    terms = [term.strip() for term in text.split(";") if term.strip()]
    operator = "OR"
    if len(terms) == 1 and terms[0] not in policy.group_of:
        try:
            expression = canonicalize_license_expression(terms[0])
        except InvalidLicenseExpression:
            # A flat expression naming a term by alias: `BSD 3-Clause OR Apache-2.0`.
            expression = _alias_expression(terms[0], policy)
            if expression is None:
                return text, Verdict.ASK, (), "unrecognised"
        # npm writes a lone expression in brackets: `(MIT OR GPL-3.0-or-later)`.
        inner = expression[1:-1]
        if expression.startswith("(") and expression.endswith(")"):
            if "(" not in inner and ")" not in inner:
                expression = inner
        if "(" in expression:
            return expression, Verdict.ASK, (), "complex-expression"
        if " AND " in expression and " OR " in expression:
            return expression, Verdict.ASK, (), "complex-expression"
        operator = "AND" if " AND " in expression else "OR"
        terms = expression.split(f" {operator} ")

    judged = [_term_rule(term, policy, mode) for term in terms]
    spdx = f" {operator} ".join(spdx for spdx, _ in judged)
    rules = [rule for _, rule in judged if rule is not None]
    unknown = len(rules) < len(judged)
    if unknown and (operator == "AND" or not rules):
        return spdx, Verdict.ASK, (), "unrecognised"

    if operator == "OR":
        chosen = min(
            rules, key=lambda rule: (_RANK[rule.verdict], len(rule.obligations))
        )
        # The unknown term may be the one that permits it.
        if unknown and _RANK[chosen.verdict] >= _RANK[Verdict.ASK]:
            return spdx, Verdict.ASK, (), "unrecognised"
        return spdx, chosen.verdict, chosen.obligations, chosen.group
    worst = max(rules, key=lambda rule: _RANK[rule.verdict])
    obligations = tuple(dict.fromkeys(o for rule in rules for o in rule.obligations))
    groups = "+".join(dict.fromkeys(rule.group for rule in rules))
    return spdx, worst.verdict, obligations, groups


def restrict_override_only(finding: Finding, policy: Policy) -> Finding:
    """Return ``finding`` as ask when an override-only group decided it without one."""
    groups = set(finding.rule.split("+"))
    if finding.source == "override" or not groups & policy.override_only:
        return finding
    return replace(finding, verdict=Verdict.ASK, obligations=(), rule="override-only")


def blocking(findings: Iterable[Finding]) -> list[Finding]:
    """Return the findings a blocking gate fails on: every deny and ask."""
    return [f for f in findings if f.verdict in (Verdict.DENY, Verdict.ASK)]


def report(
    findings: Sequence[Finding], policy: Policy, tool_versions: Mapping[str, str]
) -> dict[str, Any]:
    """Return the machine-readable report: one record per dependency."""
    deps = []
    for finding in sorted(findings, key=lambda f: (f.ecosystem, f.name.lower())):
        record = asdict(finding)
        record["obligations"] = list(finding.obligations)
        deps.append(record)
    return {
        "schema": SCHEMA_VERSION,
        # The writing version: a hyperi-ci upgrade can change the packaged policy.
        "hyperi_ci": __version__,
        "policy": policy.digest,
        "tool_versions": dict(tool_versions),
        "deps": deps,
    }


_RULE = "-" * 78

# GNU texts an `lgpl-texts` dependency ships with: its LGPL, and the GPL beside it.
_GNU_TEXTS_DIR = _POLICY_PATH.parent / "licence-texts"
_GNU_TEXTS = (
    ("LGPL-3.0", ("LGPL-3.0-only", "GPL-3.0-only")),
    ("LGPL-2.1", ("LGPL-2.1-only", "GPL-2.0-only")),
    ("LGPL-2.0", ("LGPL-2.0-only", "GPL-2.0-only")),
)
_ALL_GNU_TEXTS = tuple(sorted({n for _, names in _GNU_TEXTS for n in names}))


def _gnu_texts(findings: Iterable[Finding]) -> list[str]:
    """Return the GNU licence texts the ``lgpl-texts`` findings need, sorted."""
    needed: set[str] = set()
    for finding in findings:
        if "lgpl-texts" not in finding.obligations:
            continue
        spdx = finding.spdx or ""
        matched = [names for prefix, names in _GNU_TEXTS if prefix in spdx]
        # An unversioned LGPL could be any of them.
        needed.update(*matched or [_ALL_GNU_TEXTS])
    return sorted(needed)


@dataclass(frozen=True, slots=True)
class NoticeEntry:
    """One dependency's attribution: its finding and the texts it shipped."""

    finding: Finding
    licence_text: str | None
    notice_text: str | None


def render_notices(entries: Iterable[NoticeEntry]) -> str:
    """Return THIRD-PARTY-NOTICES text, byte-identical for the same lock.

    Every shipped dependency is listed, an unanswered ask included, because
    its licence text ships whatever its verdict. Its licence file follows its
    name, then its NOTICE file where the policy says to carry it. The LGPL and
    GPL texts an ``lgpl-texts`` dependency needs follow once, at the end.

    Args:
        entries: Each dependency's finding and the texts its package carries.

    Returns:
        The file's full text.

    """
    listed = sorted(
        entries,
        key=lambda e: (e.finding.ecosystem, e.finding.name.lower(), e.finding.version),
    )
    parts = [
        "THIRD-PARTY-NOTICES",
        "",
        "Generated by hyperi-ci from the lock. Do not edit by hand.",
        "",
    ]
    for entry in listed:
        finding = entry.finding
        parts += [
            _RULE,
            f"{finding.name} {finding.version} ({finding.spdx or 'unknown'})",
            _RULE,
            "",
            _text_or_missing(entry.licence_text, "licence"),
        ]
        if "carry-notice" in finding.obligations and _shipped(entry.notice_text):
            parts += ["", "NOTICE:", "", str(entry.notice_text).strip()]
    for name in _gnu_texts(entry.finding for entry in listed):
        text = (_GNU_TEXTS_DIR / f"{name}.txt").read_text(encoding="utf-8")
        parts += [_RULE, f"{name}, for the LGPL dependencies above", _RULE, ""]
        parts.append(text.strip())
    return "\n".join(parts).rstrip() + "\n"


def _shipped(text: str | None) -> bool:
    return bool(text) and str(text).strip().upper() not in _UNDECLARED


def _text_or_missing(text: str | None, what: str) -> str:
    if _shipped(text):
        return str(text).strip()
    return f"[no {what} file in the package]"


def quote(text: str | None) -> str:
    """Return the first lines of a licence file, for a citation's evidence."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return " / ".join(lines[:_QUOTE_LINES])


def project_path(raw: object) -> str:
    """Return ``raw`` relative to the project, so the report is the same anywhere."""
    path = Path(str(raw))
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(raw)


def load_settings(config: CIConfig) -> tuple[Policy, list[Override]]:
    """Return the effective policy and the repo's overrides.

    Raises:
        PolicyError: The repo's ``quality.dep_licenses`` is malformed.

    """
    # A bare mode string carries no overrides or policy.
    settings = config.setting("quality.dep_licenses")
    settings = settings if isinstance(settings, Mapping) else {}
    policy = load_policy(repo_policy=settings.get("policy"))
    return policy, parse_overrides(settings.get("overrides"))


def fail(note: str, mode: str) -> bool:
    """Report ``note`` at the gate's mode, and return whether the pipeline goes on."""
    (warn if mode == "warn" else error)(note)
    return mode == "warn"


def finish(
    findings: Sequence[Finding],
    entries: Sequence[NoticeEntry],
    policy: Policy,
    tools: Mapping[str, str],
    mode: str,
    overrides: Sequence[Override] = (),
) -> bool:
    """Write the report and the notices, then fail on every deny and ask.

    Args:
        findings: One finding per shipped dependency.
        entries: The same dependencies with the texts they shipped.
        policy: The effective policy.
        tools: Tool name to the version that read the licences.
        mode: ``blocking`` or ``warn``.
        overrides: The repo's answers, whose ``licence_file`` fills in licence
            text a package did not ship.

    Returns:
        True if the pipeline should continue, False on a blocking failure.

    """
    findings = [restrict_override_only(f, policy) for f in findings]
    entries = [
        replace(e, finding=restrict_override_only(e.finding, policy)) for e in entries
    ]
    REPORT_PATH.parent.mkdir(exist_ok=True)
    (REPORT_PATH.parent / ".gitignore").write_text(
        "*\n", encoding="utf-8", newline="\n"
    )
    REPORT_PATH.write_text(
        json.dumps(report(findings, policy, tools), indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    info(
        f"  dep licences: {len(findings)} shipped dependencies judged, see {REPORT_PATH}"
    )
    notices_current = check_notices(entries, mode, overrides)
    held = blocking(findings)
    for finding in held:
        (warn if mode == "warn" else error)(f"  {finding.line()}")
        info(f"    evidence: {finding.evidence}")
    if held:
        return fail(
            f"  dep licences: {len(held)} dependencies denied or need a human answer. "
            "Answer a package in quality.dep_licenses.overrides (name, licence, "
            "reason), a copied-in file with its vendored entry's licence and reason.",
            mode,
        )
    return notices_current


def _with_licence_files(
    entries: Sequence[NoticeEntry], overrides: Sequence[Override]
) -> list[NoticeEntry]:
    """Fill in licence text from an override's ``licence_file``."""
    filled = []
    for entry in entries:
        finding = entry.finding
        override = find_override(
            overrides, finding.ecosystem, finding.name, finding.version
        )
        if not _shipped(entry.licence_text) and override and override.licence_file:
            path = Path(override.licence_file)
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")
                entry = replace(entry, licence_text=text)
        filled.append(entry)
    return filled


def check_notices(
    entries: Sequence[NoticeEntry], mode: str, overrides: Sequence[Override] = ()
) -> bool:
    """Write the generated notices and hold the repo's copy to them, if it has one.

    A dependency with no licence text fails the check rather than shipping a
    placeholder. Its override's ``licence_file`` supplies the text.

    Returns:
        True unless text is missing or the repo's copy is stale, and the mode
        blocks.

    """
    entries = _with_licence_files(entries, overrides)
    generated = render_notices(entries)
    NOTICES_OUTPUT.write_text(generated, encoding="utf-8", newline="\n")
    missing = [
        f"{e.finding.name} {e.finding.version}"
        for e in entries
        if e.finding.verdict is not Verdict.ALLOW and not _shipped(e.licence_text)
    ]
    passed = True
    if missing:
        passed = fail(
            f"  dep licences: no licence text for {', '.join(sorted(missing))}. "
            "Commit the text and point its override's licence_file at it.",
            mode,
        )
    if not NOTICES_PATH.is_file():
        return passed
    if NOTICES_PATH.read_text(encoding="utf-8") == generated:
        info(f"  dep licences: {NOTICES_PATH} matches the lock")
        return passed
    stale = fail(
        f"  dep licences: {NOTICES_PATH} is stale against the lock. "
        f"Replace it with {NOTICES_OUTPUT}, which this run generated.",
        mode,
    )
    return passed and stale
