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
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from packaging.licenses import InvalidLicenseExpression, canonicalize_license_expression

_POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "license-policy.yaml"

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
        digest: sha256 of the effective policy, so a citation names the table it used.

    """

    groups: Mapping[str, Mapping[Mode, Rule]]
    group_of: Mapping[str, str]
    canonical: Mapping[str, str]
    obligations: Mapping[str, str]
    digest: str


@dataclass(frozen=True, slots=True)
class Override:
    """A repo's answer for one dependency the tools could not settle."""

    name: str
    licence: str
    reason: str
    version: str | None = None


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
    for name, body in (raw.get("groups") or {}).items():
        ids = list(body.get("ids") or [])
        if not ids:
            raise PolicyError(f"group {name!r}: no `ids`")
        groups[name] = _parse_modes(body.get("modes"), name, obligations)
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
    digest = hashlib.sha256(
        json.dumps([effective, group_of], sort_keys=True).encode("utf-8")
    ).hexdigest()
    return Policy(
        groups=groups,
        group_of=group_of,
        canonical=canonical,
        obligations=obligations,
        digest=digest,
    )


def parse_overrides(raw: Iterable[Any] | None) -> list[Override]:
    """Parse ``quality.dep_licenses.overrides`` from .hyperi-ci.yaml.

    Args:
        raw: A list of ``{name, licence, reason, version?}`` mappings.

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
        overrides.append(
            Override(name, licence, reason, str(version) if version else None)
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


def _term_rule(term: str, policy: Policy, mode: Mode) -> tuple[str, Rule | None]:
    """Return the SPDX id and policy rule for one licence id or alias."""
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
    ``WITH`` exception, a parenthesised or mixed expression, an undeclared
    licence and an id no group lists are all ask.

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
            return text, Verdict.ASK, (), "unrecognised"
        if "(" in expression or " WITH " in expression:
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
        "policy": policy.digest,
        "tool_versions": dict(tool_versions),
        "deps": deps,
    }
