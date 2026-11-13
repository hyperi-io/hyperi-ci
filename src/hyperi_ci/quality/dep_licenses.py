# Project:   HyperI CI
# File:      src/hyperi_ci/quality/dep_licenses.py
# Purpose:   Judge each shipped dependency's licence against config/licensing.yaml
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Judge each shipped dependency's licence against ``config/licensing.yaml``.

The question is whether anything we ship poisons the licence we ship under.
Each language reads what its tool reported, and this module turns it into a
:class:`Finding` an agent can cite, so a verdict never comes from memory.
"""

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from packaging.licenses import InvalidLicenseExpression, canonicalize_license_expression

from hyperi_ci import __version__
from hyperi_ci.common import error, info, warn
from hyperi_ci.config import CIConfig

_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
POLICY_PATH = _CONFIG_DIR / "licensing.yaml"
_TEXTS = _CONFIG_DIR / "licence-texts"

REPORT_PATH = Path(".hyperi-ci") / "licenses.json"
NOTICES_OUTPUT = REPORT_PATH.parent / "THIRD-PARTY-NOTICES"

SCHEMA_VERSION = 1

_LISTS = ("ok", "ok_when_dynamic", "poisons", "human", "first_party")

# Licence file lines quoted when metadata declares nothing.
_QUOTE_LINES = 3

_SEPARATORS = re.compile(r"[-_.]+")

# Metadata values that mean "no licence declared".
_UNDECLARED = frozenset({"", "UNKNOWN", "NONE", "NOASSERTION"})

# Where an MPL file's source is, by ecosystem.
_SOURCE_URLS = {
    "pypi": "https://pypi.org/project/{name}/{version}/",
    "crates": "https://crates.io/crates/{name}/{version}",
    "npm": "https://www.npmjs.com/package/{name}/v/{version}",
}

# GNU texts an `lgpl-texts` dependency ships with: its LGPL, and the GPL beside it.
_GNU_TEXTS = (
    ("LGPL-3.0", ("LGPL-3.0-only", "GPL-3.0-only")),
    ("LGPL-2.1", ("LGPL-2.1-only", "GPL-2.0-only")),
    ("LGPL-2.0", ("LGPL-2.0-only", "GPL-2.0-only")),
)
_ALL_GNU_TEXTS = tuple(sorted({n for _, names in _GNU_TEXTS for n in names}))


class Mode(StrEnum):
    """How a dependency reaches what ships. Only ``import`` is a dynamic load."""

    IMPORT = "import"
    STATIC = "static"
    VENDORED = "vendored"
    MODIFIED = "modified"


class Verdict(StrEnum):
    """The table's answer: ok, poisons, or a human must answer."""

    OBLIGATIONS = "allow-with-obligations"
    ASK = "ask"
    DENY = "deny"


class PolicyError(ValueError):
    """The licence table or a repo's overrides are malformed."""


@dataclass(frozen=True, slots=True)
class Rule:
    """A verdict, the obligations it carries, and which list decided it."""

    verdict: Verdict
    obligations: tuple[str, ...] = ()
    name: str = ""

    @property
    def rank(self) -> int:
        """Order from best to worst: ok, human, unknown, poisons."""
        if self.verdict is Verdict.ASK:
            return 1 if self.name == "human" else 2
        return 0 if self.verdict is Verdict.OBLIGATIONS else 3


@dataclass(frozen=True, slots=True)
class Policy:
    """The loaded licence table.

    Attributes:
        lists: SPDX id to the list naming it.
        aliases: Metadata name to the SPDX id it means.
        exceptions: ``WITH`` exceptions that clear their base licence.
        obligations: SPDX id prefix to what ships beyond the notice.
        digest: sha256 of the table, so a citation names the table it used.

    """

    lists: Mapping[str, str]
    aliases: Mapping[str, str]
    exceptions: frozenset[str]
    obligations: Mapping[str, tuple[str, ...]]
    digest: str


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


def load_policy(path: Path | None = None) -> Policy:
    """Load the licence table.

    Raises:
        PolicyError: An id is on two lists.

    """
    text = (path or POLICY_PATH).read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    lists: dict[str, str] = {}
    for name in _LISTS:
        for spdx in raw.get(name) or ():
            if spdx in lists:
                raise PolicyError(f"{spdx!r} is in both {lists[spdx]!r} and {name!r}")
            lists[spdx] = name
    return Policy(
        lists=lists,
        aliases={str(k): str(v) for k, v in (raw.get("aliases") or {}).items()},
        exceptions=frozenset(str(e) for e in raw.get("exceptions") or ()),
        obligations={k: tuple(v) for k, v in (raw.get("obligations") or {}).items()},
        digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def parse_overrides(raw: Iterable[Any] | None) -> list[Override]:
    """Parse ``quality.dep_licenses.overrides`` from .hyperi-ci.yaml.

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


def _resolve(term: str, policy: Policy) -> str:
    if term in policy.aliases:
        return policy.aliases[term]
    try:
        return canonicalize_license_expression(term)
    except InvalidLicenseExpression:
        return term


def _ok(spdx: str, policy: Policy, name: str) -> Rule:
    extra = [
        obligation
        for prefix, obligations in policy.obligations.items()
        if spdx.startswith(prefix)
        for obligation in obligations
    ]
    return Rule(Verdict.OBLIGATIONS, tuple(dict.fromkeys(["notice", *extra])), name)


def _leaf(term: str, policy: Policy, mode: Mode, override: bool) -> tuple[str, Rule]:
    """Judge one licence id or alias, with an optional ``WITH`` exception."""
    base, _, exception = term.partition(" WITH ")
    spdx = _resolve(base.strip(), policy)
    listed = policy.lists.get(spdx)
    if exception:
        text = f"{spdx} WITH {exception.strip()}"
        if exception.strip() in policy.exceptions and listed:
            return text, _ok(spdx, policy, "exception")
        return text, Rule(Verdict.ASK, (), "unrecognised")
    if listed == "first_party":
        if override:
            return spdx, _ok(spdx, policy, "first-party")
        return spdx, Rule(Verdict.ASK, (), "override-only")
    if listed == "ok" or (listed == "ok_when_dynamic" and mode is Mode.IMPORT):
        return spdx, _ok(spdx, policy, listed)
    verdicts = {
        "ok_when_dynamic": Rule(Verdict.ASK, (), "static-link"),
        "poisons": Rule(Verdict.DENY, (), "poisons"),
        "human": Rule(Verdict.ASK, (), "human"),
    }
    return spdx, verdicts.get(listed or "", Rule(Verdict.ASK, (), "unrecognised"))


def _split_top(expression: str, operator: str) -> list[str]:
    """Split ``expression`` on ``operator`` outside any brackets."""
    parts, depth, start, i = [], 0, 0, 0
    token = f" {operator} "
    while i < len(expression):
        depth += (expression[i] == "(") - (expression[i] == ")")
        if depth == 0 and expression.startswith(token, i):
            parts.append(expression[start:i])
            i += len(token)
            start = i
            continue
        i += 1
    parts.append(expression[start:])
    return parts


def _strip_brackets(expression: str) -> str:
    """Drop brackets that wrap the whole of ``expression``."""
    while expression.startswith("(") and expression.endswith(")"):
        depth = 0
        for i, char in enumerate(expression):
            depth += (char == "(") - (char == ")")
            if depth == 0 and i < len(expression) - 1:
                return expression
        expression = expression[1:-1].strip()
    return expression


def _bracket(text: str) -> str:
    return f"({text})" if " OR " in text or " AND " in text else text


def _evaluate(
    expression: str, policy: Policy, mode: Mode, override: bool
) -> tuple[str, Rule]:
    """Judge an expression. AND binds tighter, OR takes its best term, AND its worst."""
    expression = _strip_brackets(expression.strip())
    for operator in ("OR", "AND"):
        parts = _split_top(expression, operator)
        if len(parts) == 1:
            continue
        judged = [_evaluate(part, policy, mode, override) for part in parts]
        text = f" {operator} ".join(_bracket(spdx) for spdx, _ in judged)
        rules = [rule for _, rule in judged]
        if operator == "OR":
            return text, min(rules, key=lambda r: (r.rank, len(r.obligations)))
        worst = max(rules, key=lambda r: r.rank)
        if worst.verdict is not Verdict.OBLIGATIONS:
            return text, worst
        obligations = tuple(dict.fromkeys(o for r in rules for o in r.obligations))
        return text, replace(worst, obligations=obligations)
    return _leaf(expression, policy, mode, override)


def judge(
    raw_licence: str | None, mode: Mode, policy: Policy, *, override: bool = False
) -> tuple[str | None, Verdict, tuple[str, ...], str]:
    """Return the SPDX expression, verdict, obligations and deciding list.

    Args:
        raw_licence: The licence as metadata gave it: an SPDX expression, a
            trove classifier name, or several joined with ``"; "``.
        mode: How the dependency reaches what ships.
        policy: The licence table.
        override: The licence came from a repo override, the only way a
            first-party id is ok.

    Returns:
        ``(spdx, verdict, obligations, rule)``.

    """
    text = (raw_licence or "").strip()
    if text.upper() in _UNDECLARED:
        return None, Verdict.ASK, (), "undeclared"
    # Several classifiers on one package mean the user may pick any of them.
    terms = [term.strip() for term in text.split(";") if term.strip()]
    expression = " OR ".join(_bracket(term) for term in terms)
    try:
        expression = canonicalize_license_expression(expression)
    except InvalidLicenseExpression:
        pass  # alias names, resolved term by term
    spdx, rule = _evaluate(expression, policy, mode, override)
    return spdx, rule.verdict, rule.obligations, rule.name


def judge_override(licence: str, mode: Mode, policy: Policy) -> dict[str, Any]:
    """Return the table's answer for an override that names ``licence``.

    ``policy`` is the digest a licenses.json report carries, so a caller can
    tie the answer to the report it overrides. ``override_only`` marks a
    first-party id, ok only because an override named it.
    """
    spdx, verdict, obligations, rule = judge(licence, mode, policy, override=True)
    return {
        "licence": licence,
        "spdx": spdx,
        "mode": mode.value,
        "verdict": verdict.value,
        "obligations": list(obligations),
        "rule": rule,
        "override_only": rule == "first-party",
        "policy": policy.digest,
    }


def blocking(findings: Iterable[Finding]) -> list[Finding]:
    """Return the findings a blocking gate fails on: every deny and ask."""
    return [f for f in findings if f.verdict is not Verdict.OBLIGATIONS]


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
        # The writing version: a hyperi-ci upgrade can change the packaged table.
        "hyperi_ci": __version__,
        "policy": policy.digest,
        "tool_versions": dict(tool_versions),
        "deps": deps,
    }


@dataclass(frozen=True, slots=True)
class NoticeEntry:
    """One dependency's attribution: its finding and the texts it shipped."""

    finding: Finding
    licence_text: str | None
    notice_text: str | None


def _shipped(text: str | None) -> bool:
    return bool(text) and str(text).strip().upper() not in _UNDECLARED


def standard_text(expression: str | None) -> str | None:
    """Return the bundled SPDX text of every licence ``expression`` names, else None."""
    words = re.split(r"[\s()]+", expression or "")
    ids = [
        word
        for i, word in enumerate(words)
        if word and word not in ("AND", "OR", "WITH") and words[i - 1] != "WITH"
    ]
    paths = [_TEXTS / "spdx" / f"{spdx}.txt" for spdx in dict.fromkeys(ids)]
    if not paths or not all(path.is_file() for path in paths):
        return None
    texts = [path.read_text(encoding="utf-8").strip() for path in paths]
    header = f"SPDX standard text for {expression}: the package ships no licence file."
    return "\n\n".join([header, *texts])


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


def render_notices(entries: Iterable[NoticeEntry]) -> str:
    """Return THIRD-PARTY-NOTICES text, byte-identical for the same lock.

    Every shipped dependency is listed with its licence text, else the SPDX
    standard text, and any NOTICE file it ships. An MPL dependency says where
    its source is. The GNU texts the LGPL dependencies need follow once.
    """
    rule = "-" * 78
    listed = sorted(
        entries,
        key=lambda e: (e.finding.ecosystem, e.finding.name.lower(), e.finding.version),
    )
    parts = ["THIRD-PARTY-NOTICES", "", "Generated by hyperi-ci from the lock.", ""]
    for entry in listed:
        finding = entry.finding
        text = entry.licence_text if _shipped(entry.licence_text) else None
        text = text or standard_text(finding.spdx) or "[no licence file in the package]"
        heading = f"{finding.name} {finding.version} ({finding.spdx or 'unknown'})"
        parts += [rule, heading, rule, "", text.strip()]
        url = _SOURCE_URLS.get(finding.ecosystem)
        if "mpl-source" in finding.obligations and url:
            source = url.format(name=finding.name, version=finding.version)
            parts += ["", f"Source: {source}"]
        if _shipped(entry.notice_text):
            parts += ["", "NOTICE:", "", str(entry.notice_text).strip()]
    for name in _gnu_texts(entry.finding for entry in listed):
        text = (_TEXTS / f"{name}.txt").read_text(encoding="utf-8")
        heading = f"{name}, for the LGPL dependencies above"
        parts += [rule, heading, rule, "", text.strip()]
    return "\n".join(parts).rstrip() + "\n"


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
    """Return the licence table and the repo's overrides.

    Raises:
        PolicyError: The repo's ``quality.dep_licenses.overrides`` is malformed.

    """
    settings = config.setting("quality.dep_licenses")
    settings = settings if isinstance(settings, Mapping) else {}
    if settings.get("policy"):
        warn(
            "  dep licences: quality.dep_licenses.policy is no longer read. "
            "Answer one dependency under quality.dep_licenses.overrides instead."
        )
    return load_policy(), parse_overrides(settings.get("overrides"))


def fail(note: str, mode: str) -> bool:
    """Report ``note`` at the gate's mode, and return whether the pipeline goes on."""
    (warn if mode == "warn" else error)(note)
    return mode == "warn"


def _with_licence_files(
    entries: Sequence[NoticeEntry], overrides: Sequence[Override]
) -> list[NoticeEntry]:
    """Fill in licence text from an override's ``licence_file``."""
    filled = []
    for entry in entries:
        finding = entry.finding
        found = find_override(
            overrides, finding.ecosystem, finding.name, finding.version
        )
        path = Path(found.licence_file) if found and found.licence_file else None
        if not _shipped(entry.licence_text) and path and path.is_file():
            entry = replace(entry, licence_text=path.read_text(encoding="utf-8"))
        filled.append(entry)
    return filled


def finish(
    findings: Sequence[Finding],
    entries: Sequence[NoticeEntry],
    policy: Policy,
    tools: Mapping[str, str],
    mode: str,
    overrides: Sequence[Override] = (),
) -> bool:
    """Write the report and the notices, then fail on every deny and ask.

    Returns:
        True if the pipeline should continue, False on a blocking failure.

    """
    REPORT_PATH.parent.mkdir(exist_ok=True)
    (REPORT_PATH.parent / ".gitignore").write_text(
        "*\n", encoding="utf-8", newline="\n"
    )
    REPORT_PATH.write_text(
        json.dumps(report(findings, policy, tools), indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    notices = render_notices(_with_licence_files(entries, overrides))
    NOTICES_OUTPUT.write_text(notices, encoding="utf-8", newline="\n")
    info(
        f"  dep licences: {len(findings)} shipped dependencies judged, see {REPORT_PATH}"
    )
    held = blocking(findings)
    for finding in held:
        (warn if mode == "warn" else error)(f"  {finding.line()}")
        info(f"    evidence: {finding.evidence}")
    if not held:
        return True
    return fail(
        f"  dep licences: {len(held)} dependencies poison the licence or need a "
        "human answer. Answer a package in quality.dep_licenses.overrides (name, "
        "licence, reason), a copied-in file with its vendored entry's licence and "
        "reason.",
        mode,
    )


def fresh_notices(run_gate: Callable[..., bool], config: CIConfig) -> Path | None:
    """Run a language's licence reader and return the notices it just wrote.

    The reader runs in ``warn`` mode, so a held licence never stops it. None
    when its tool did not run, so a notices file an earlier run left is never
    passed off as current.
    """
    before = NOTICES_OUTPUT.stat().st_mtime_ns if NOTICES_OUTPUT.is_file() else 0
    run_gate(config, mode="warn")
    if NOTICES_OUTPUT.is_file() and NOTICES_OUTPUT.stat().st_mtime_ns > before:
        return NOTICES_OUTPUT
    return None
