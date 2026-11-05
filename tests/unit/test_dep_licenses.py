# Project:   HyperI CI
# File:      tests/unit/test_dep_licenses.py
# Purpose:   Golden table for the dependency-licence policy and the Python gate
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Golden table for the dependency-licence policy and the Python gate."""

import json

import pytest

from hyperi_ci.config import load_config
from hyperi_ci.languages.python import licenses
from hyperi_ci.languages.python.licenses import (
    findings_for,
    notice_entries,
    runtime_packages,
)
from hyperi_ci.quality.dep_licenses import (
    Mode,
    Override,
    PolicyError,
    Verdict,
    blocking,
    find_override,
    judge,
    load_policy,
    parse_overrides,
    render_notices,
    report,
)

# The first lines of pysigma-backend-clickhouse 1.1.1's wheel licence file, as
# pip-licenses 5.5.5 returns them. Its PyPI license and license_expression are null.
_GOLDEN_LGPL = {
    "Name": "pysigma-backend-clickhouse",
    "Version": "1.1.1",
    "License": "UNKNOWN",
    "NoticeText": "UNKNOWN",
    "LicenseFile": ".venv/lib/python3.14/site-packages/"
    "pysigma_backend_clickhouse-1.1.1.dist-info/licenses/LICENSE.md",
    "LicenseText": "                   GNU LESSER GENERAL PUBLIC LICENSE\n"
    "                       Version 3, 29 June 2007\n\n"
    " Copyright (C) 2007 Free Software Foundation, Inc. <https://fsf.org/>\n",
}


@pytest.fixture(scope="module")
def policy():
    return load_policy()


@pytest.mark.parametrize(
    ("licence", "mode", "spdx", "verdict", "obligations"),
    [
        (
            "LGPL-3.0-only",
            Mode.IMPORT,
            "LGPL-3.0-only",
            Verdict.OBLIGATIONS,
            ("notice", "lgpl-texts", "replaceable"),
        ),
        ("LGPL-3.0-only", Mode.VENDORED, "LGPL-3.0-only", Verdict.DENY, ()),
        ("LGPL-3.0-only", Mode.MODIFIED, "LGPL-3.0-only", Verdict.DENY, ()),
        ("LGPL-3.0-only", Mode.STATIC, "LGPL-3.0-only", Verdict.ASK, ()),
        (
            "GNU Lesser General Public License v2 or later (LGPLv2+)",
            Mode.IMPORT,
            "LGPL-2.0-or-later",
            Verdict.OBLIGATIONS,
            ("notice", "lgpl-texts", "replaceable"),
        ),
        ("GPL-3.0-only", Mode.IMPORT, "GPL-3.0-only", Verdict.DENY, ()),
        ("AGPL-3.0-or-later", Mode.IMPORT, "AGPL-3.0-or-later", Verdict.DENY, ()),
        ("SSPL-1.0", Mode.IMPORT, "SSPL-1.0", Verdict.DENY, ()),
        (
            "Apache-2.0",
            Mode.IMPORT,
            "Apache-2.0",
            Verdict.OBLIGATIONS,
            ("notice", "licence-text", "carry-notice"),
        ),
        (
            "Apache Software License",
            Mode.STATIC,
            "Apache-2.0",
            Verdict.OBLIGATIONS,
            ("notice", "licence-text", "carry-notice"),
        ),
        ("MIT", Mode.IMPORT, "MIT", Verdict.OBLIGATIONS, ("notice", "licence-text")),
        ("mit", Mode.IMPORT, "MIT", Verdict.OBLIGATIONS, ("notice", "licence-text")),
        # The classifier does not say which BSD, and BSD-4-Clause is one of them.
        ("BSD License", Mode.IMPORT, "BSD License", Verdict.ASK, ()),
        (
            "ISC License (ISCL)",
            Mode.IMPORT,
            "ISC",
            Verdict.OBLIGATIONS,
            ("notice", "licence-text"),
        ),
        ("CC0-1.0", Mode.MODIFIED, "CC0-1.0", Verdict.ALLOW, ()),
        # OR takes the best term, and the lighter obligations on a tie.
        (
            "Apache-2.0 OR GPL-2.0-or-later",
            Mode.IMPORT,
            "Apache-2.0 OR GPL-2.0-or-later",
            Verdict.OBLIGATIONS,
            ("notice", "licence-text", "carry-notice"),
        ),
        (
            "Apache Software License; MIT License",
            Mode.IMPORT,
            "Apache-2.0 OR MIT",
            Verdict.OBLIGATIONS,
            ("notice", "licence-text"),
        ),
        # AND takes the worst term and every obligation.
        (
            "MIT AND GPL-3.0-only",
            Mode.IMPORT,
            "MIT AND GPL-3.0-only",
            Verdict.DENY,
            ("notice", "licence-text"),
        ),
        (
            "Apache-2.0 AND CNRI-Python",
            Mode.IMPORT,
            "Apache-2.0 AND CNRI-Python",
            Verdict.OBLIGATIONS,
            ("notice", "licence-text", "carry-notice"),
        ),
        # Nothing a human has not answered is ever allowed.
        (None, Mode.IMPORT, None, Verdict.ASK, ()),
        ("UNKNOWN", Mode.IMPORT, None, Verdict.ASK, ()),
        ("", Mode.IMPORT, None, Verdict.ASK, ()),
        (
            "LicenseRef-Proprietary",
            Mode.IMPORT,
            "LicenseRef-Proprietary",
            Verdict.ASK,
            (),
        ),
        (
            "Some Custom Licence v1",
            Mode.IMPORT,
            "Some Custom Licence v1",
            Verdict.ASK,
            (),
        ),
        (
            "MIT AND LicenseRef-Proprietary",
            Mode.IMPORT,
            "MIT AND LicenseRef-Proprietary",
            Verdict.ASK,
            (),
        ),
        # An unknown alternative may be the permissive one, so it is never a deny.
        (
            "LicenseRef-Foo OR GPL-3.0-only",
            Mode.IMPORT,
            "LicenseRef-Foo OR GPL-3.0-only",
            Verdict.ASK,
            (),
        ),
        (
            "LicenseRef-Foo OR MIT",
            Mode.IMPORT,
            "LicenseRef-Foo OR MIT",
            Verdict.OBLIGATIONS,
            ("notice", "licence-text"),
        ),
        (
            "mit; apache-2.0",
            Mode.IMPORT,
            "MIT OR Apache-2.0",
            Verdict.OBLIGATIONS,
            ("notice", "licence-text"),
        ),
        (
            "GPL-2.0-only WITH Classpath-exception-2.0",
            Mode.IMPORT,
            "GPL-2.0-only WITH Classpath-exception-2.0",
            Verdict.ASK,
            (),
        ),
        (
            "(MIT OR Apache-2.0) AND BSD-3-Clause",
            Mode.IMPORT,
            "(MIT OR Apache-2.0) AND BSD-3-Clause",
            Verdict.ASK,
            (),
        ),
    ],
)
def test_judge_golden_table(policy, licence, mode, spdx, verdict, obligations):
    got_spdx, got_verdict, got_obligations, _ = judge(licence, mode, policy)
    assert (got_spdx, got_verdict, got_obligations) == (spdx, verdict, obligations)


def test_every_group_judges_every_mode(policy):
    for name, modes in policy.groups.items():
        assert set(modes) == set(Mode), name


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"gpl": {"import": {"verdict": "allow"}}}, "reason"),
        ({"gpl": {"import": {"verdict": "maybe", "reason": "r"}}}, "unknown verdict"),
        ({"gpl": {"linked": {"verdict": "allow", "reason": "r"}}}, "unknown mode"),
        ({"nope": {"import": {"verdict": "allow", "reason": "r"}}}, "unknown group"),
        (
            {"lgpl": {"static": {"verdict": "allow-with-obligations", "reason": "r"}}},
            "names no obligations",
        ),
        (
            {
                "lgpl": {
                    "static": {
                        "verdict": "allow-with-obligations",
                        "obligations": ["x"],
                        "reason": "r",
                    }
                }
            },
            "unknown obligations",
        ),
    ],
)
def test_repo_policy_override_rejects_bad_cells(override, message):
    with pytest.raises(PolicyError, match=message):
        load_policy(repo_policy=override)


def test_repo_policy_override_changes_one_cell_and_the_digest(policy):
    changed = load_policy(
        repo_policy={
            "lgpl": {"static": {"verdict": "deny", "reason": "no LGPL in binaries"}}
        }
    )
    assert judge("LGPL-3.0-only", Mode.STATIC, changed)[1] is Verdict.DENY
    assert judge("LGPL-3.0-only", Mode.IMPORT, changed)[1] is Verdict.OBLIGATIONS
    assert changed.digest != policy.digest


@pytest.mark.parametrize(
    "raw",
    [
        [{"name": "x", "licence": "MIT"}],
        [{"name": "x", "reason": "r"}],
        [{"licence": "MIT", "reason": "r"}],
        ["x"],
    ],
)
def test_overrides_need_name_licence_and_reason(raw):
    with pytest.raises(PolicyError):
        parse_overrides(raw)


def test_golden_lgpl_with_no_metadata_asks_and_quotes_the_licence_file(policy):
    findings = findings_for(
        [_GOLDEN_LGPL], {"pysigma-backend-clickhouse": "1.1.1"}, policy, []
    )
    (finding,) = findings
    assert finding.verdict is Verdict.ASK
    assert finding.source == "license-file"
    assert finding.evidence.startswith(
        ".venv/lib/python3.14/site-packages/"
        "pysigma_backend_clickhouse-1.1.1.dist-info/licenses/LICENSE.md: "
        "GNU LESSER GENERAL PUBLIC LICENSE / Version 3"
    )
    assert blocking(findings) == findings


@pytest.mark.parametrize(
    ("override_name", "override_version", "applies"),
    [
        ("pysigma-backend-clickhouse", "1.1.1", True),
        ("pysigma-backend-clickhouse", None, True),
        ("PySigma_Backend.ClickHouse", None, True),
        ("pysigma-backend-clickhouse", "1.0.0", False),
        ("pysigma-backend", None, False),
    ],
)
def test_golden_lgpl_answered_by_override(
    policy, override_name, override_version, applies
):
    overrides = [
        Override(override_name, "LGPL-3.0-only", "wheel LICENSE.md", override_version)
    ]
    (finding,) = findings_for(
        [_GOLDEN_LGPL], {"pysigma-backend-clickhouse": "1.1.1"}, policy, overrides
    )
    if applies:
        assert (finding.verdict, finding.source) == (Verdict.OBLIGATIONS, "override")
        assert finding.obligations == ("notice", "lgpl-texts", "replaceable")
    else:
        assert finding.verdict is Verdict.ASK


def test_runtime_package_not_installed_is_ask(policy):
    (finding,) = findings_for([], {"ghost": "1.0"}, policy, [])
    assert (finding.verdict, finding.source) == (Verdict.ASK, "missing")


def test_runtime_packages_reads_pins_and_sets_aside_other_platforms():
    export = (
        "# generated\n"
        "anyio==4.15.1\n"
        "colorama==0.4.6 ; sys_platform == 'never'\n"
        "-e .\n"
        "not a requirement ==\n"
        "Typing_Extensions==4.16.0 ; python_version >= '3.0'\n"
        "gitdep @ git+https://example.invalid/gitdep@abc123\n"
    )
    pins, elsewhere = runtime_packages(export)
    # A git or URL reference ships too, and takes its version from the install.
    assert pins == {"anyio": "4.15.1", "typing-extensions": "4.16.0", "gitdep": ""}
    assert elsewhere == ["colorama"]


def test_a_direct_reference_is_judged_with_its_installed_version(policy):
    gpl = {"Name": "gitdep", "Version": "0.3.0", "License": "GPL-3.0-only"}
    (finding,) = findings_for([gpl], {"gitdep": ""}, policy, [])
    assert (finding.version, finding.verdict) == ("0.3.0", Verdict.DENY)


@pytest.mark.parametrize(
    ("ecosystem", "override_name", "package_name", "applies"),
    [
        ("pypi", "Foo_Bar.baz", "foo-bar-baz", True),
        ("crates", "foo_bar", "foo-bar", True),
        ("crates", "foo.bar", "foo-bar", False),
        ("npm", "foo_bar", "foo-bar", False),
        ("npm", "foo.bar", "foo-bar", False),
        ("npm", "@scope/foo-bar", "@scope/foo-bar", True),
        ("vendored", "third_party/a.c", "third_party/a.c", True),
    ],
)
def test_override_names_match_the_way_each_registry_does(
    ecosystem, override_name, package_name, applies
):
    override = Override(override_name, "MIT", "checked", None)
    found = find_override([override], ecosystem, package_name, "1.0.0")
    assert (found is override) is applies


def test_no_uv_fails_the_gate_instead_of_passing_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (tmp_path / ".hyperi-ci.yaml").write_text(
        "quality:\n  dep_licenses: blocking\n", encoding="utf-8"
    )
    monkeypatch.setenv("PATH", str(tmp_path))
    assert licenses.run_gate(load_config(reload=True)) is False


def test_report_line_and_json_are_citable(policy):
    findings = findings_for(
        [
            {
                "Name": "certifi",
                "Version": "1",
                "License": "Mozilla Public License 2.0 (MPL 2.0)",
            }
        ],
        {"certifi": "1"},
        policy,
        [],
    )
    assert findings[0].line() == (
        "licence: pypi certifi 1 MPL-2.0 import -> allow-with-obligations "
        "[notice, licence-text] (rule mpl, source metadata)"
    )
    doc = json.loads(json.dumps(report(findings, policy, {"pip-licenses": "5.5.5"})))
    assert doc["schema"] == 1
    assert doc["policy"] == policy.digest
    assert doc["deps"][0]["verdict"] == "allow-with-obligations"
    assert doc["deps"][0]["obligations"] == ["notice", "licence-text"]


_NOTICE_PACKAGES = [
    {
        "Name": "zlib-ish",
        "Version": "1",
        "License": "MIT",
        "LicenseText": "MIT text",
        "NoticeText": "UNKNOWN",
    },
    {
        "Name": "Apache-Thing",
        "Version": "2",
        "License": "Apache-2.0",
        "LicenseText": "Apache text",
        "NoticeText": "Apache NOTICE",
    },
    {
        "Name": "mit-with-notice",
        "Version": "3",
        "License": "MIT",
        "LicenseText": "UNKNOWN",
        "NoticeText": "not carried for MIT",
    },
    {
        "Name": "public",
        "Version": "4",
        "License": "CC0-1.0",
        "LicenseText": "CC0 text",
        "NoticeText": "UNKNOWN",
    },
    _GOLDEN_LGPL,
]
_NOTICE_RUNTIME = {
    "zlib-ish": "1",
    "apache-thing": "2",
    "mit-with-notice": "3",
    "public": "4",
    "pysigma-backend-clickhouse": "1.1.1",
}


def _notices(policy):
    findings = findings_for(_NOTICE_PACKAGES, _NOTICE_RUNTIME, policy, [])
    return render_notices(notice_entries(_NOTICE_PACKAGES, findings))


def test_notices_list_what_needs_attribution_and_nothing_else(policy):
    text = _notices(policy)
    headers = [line for line in text.splitlines() if line[:1].isalpha()]
    assert headers[1:] == [
        "Generated by hyperi-ci from the lock. Do not edit by hand.",
        "Apache-Thing 2 (Apache-2.0)",
        "Apache text",
        "NOTICE:",
        "Apache NOTICE",
        "mit-with-notice 3 (MIT)",
        "zlib-ish 1 (MIT)",
        "MIT text",
    ]
    # CC0 carries no obligation, and an unanswered ask is not attributed yet.
    assert "public" not in text and "pysigma" not in text
    assert "[no licence file in the package]" in text
    assert "not carried for MIT" not in text


def test_notices_are_byte_identical_whatever_the_input_order(policy):
    findings = findings_for(_NOTICE_PACKAGES, _NOTICE_RUNTIME, policy, [])
    entries = notice_entries(_NOTICE_PACKAGES, findings)
    assert render_notices(entries) == render_notices(list(reversed(entries)))


@pytest.mark.parametrize(
    ("repo_copy", "mode", "passes"),
    [
        (None, "blocking", True),
        ("current", "blocking", True),
        ("stale", "blocking", False),
        ("stale", "warn", True),
    ],
)
def test_repo_notices_are_held_to_the_lock(
    policy, tmp_path, monkeypatch, repo_copy, mode, passes
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".hyperi-ci").mkdir()
    findings = findings_for(_NOTICE_PACKAGES, _NOTICE_RUNTIME, policy, [])
    entries = notice_entries(_NOTICE_PACKAGES, findings)
    if repo_copy is not None:
        text = render_notices(entries) if repo_copy == "current" else "old\n"
        (tmp_path / "THIRD-PARTY-NOTICES").write_text(text, encoding="utf-8")
    assert licenses._check_notices(entries, mode) is passes
    generated = (tmp_path / ".hyperi-ci" / "THIRD-PARTY-NOTICES").read_text(
        encoding="utf-8"
    )
    assert generated == render_notices(entries)
