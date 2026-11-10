# Project:   HyperI CI
# File:      tests/unit/test_dep_licenses.py
# Purpose:   Golden table for the dependency-licence policy and the Python gate
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Golden table for the dependency-licence policy and the Python gate."""

import hashlib
import json

import pytest

import hyperi_ci
from hyperi_ci.config import load_config
from hyperi_ci.languages.python import licenses
from hyperi_ci.languages.python.licenses import (
    findings_for,
    notice_entries,
    repository,
    runtime_packages,
)
from hyperi_ci.languages.rust import licenses as rust_licenses
from hyperi_ci.languages.typescript import licenses as npm_licenses
from hyperi_ci.quality import vendored_licenses
from hyperi_ci.quality.bsd_text import bsd_licence
from hyperi_ci.quality.dep_licenses import (
    _POLICY_PATH,
    REPORT_PATH,
    Mode,
    Override,
    PolicyError,
    Verdict,
    blocking,
    check_notices,
    fill_and_judge,
    find_override,
    finish,
    judge,
    load_policy,
    parse_overrides,
    render_notices,
    report,
    restrict_override_only,
)
from hyperi_ci.quality.upstream_licences import Upstream

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
        # Spellings real wheels use (protobuf, uritemplate).
        (
            "3-Clause BSD License",
            Mode.IMPORT,
            "BSD-3-Clause",
            Verdict.OBLIGATIONS,
            ("notice", "licence-text"),
        ),
        (
            "BSD 3-Clause OR Apache-2.0",
            Mode.IMPORT,
            "BSD-3-Clause OR Apache-2.0",
            Verdict.OBLIGATIONS,
            ("notice", "licence-text"),
        ),
        (
            "BSD 3-Clause OR Apache-2.0 AND MIT",
            Mode.IMPORT,
            "BSD 3-Clause OR Apache-2.0 AND MIT",
            Verdict.ASK,
            (),
        ),
        (
            "Some Licence OR Apache-2.0",
            Mode.IMPORT,
            "Some Licence OR Apache-2.0",
            Verdict.ASK,
            (),
        ),
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
        # webpki-roots' licence: data, permissive, with attribution.
        (
            "CDLA-Permissive-2.0",
            Mode.STATIC,
            "CDLA-Permissive-2.0",
            Verdict.OBLIGATIONS,
            ("notice", "licence-text"),
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
        # Bracketed expressions: AND binds tighter than OR.
        (
            "(MIT OR Apache-2.0) AND BSD-3-Clause",
            Mode.IMPORT,
            "(MIT OR Apache-2.0) AND BSD-3-Clause",
            Verdict.OBLIGATIONS,
            ("notice", "licence-text"),
        ),
        (
            "(Apache-2.0 OR MIT) AND Unicode-3.0",
            Mode.STATIC,
            "(Apache-2.0 OR MIT) AND Unicode-3.0",
            Verdict.OBLIGATIONS,
            ("notice", "licence-text"),
        ),
        (
            "(MIT OR Apache-2.0) AND GPL-3.0-only",
            Mode.IMPORT,
            "(MIT OR Apache-2.0) AND GPL-3.0-only",
            Verdict.DENY,
            ("notice", "licence-text"),
        ),
        (
            "(MIT OR Apache-2.0) AND LicenseRef-Foo",
            Mode.IMPORT,
            "(MIT OR Apache-2.0) AND LicenseRef-Foo",
            Verdict.ASK,
            (),
        ),
        (
            "MIT AND (GPL-3.0-only OR LicenseRef-Foo)",
            Mode.IMPORT,
            "MIT AND (GPL-3.0-only OR LicenseRef-Foo)",
            Verdict.ASK,
            (),
        ),
        # npm wraps a lone expression in brackets. Only that one pair is dropped.
        (
            "(MIT OR GPL-3.0-or-later)",
            Mode.IMPORT,
            "MIT OR GPL-3.0-or-later",
            Verdict.OBLIGATIONS,
            ("notice", "licence-text"),
        ),
        (
            "(MIT AND Zlib)",
            Mode.IMPORT,
            "MIT AND Zlib",
            Verdict.OBLIGATIONS,
            ("notice", "licence-text"),
        ),
        (
            "(MIT) OR (GPL-3.0-only)",
            Mode.IMPORT,
            "(MIT) OR (GPL-3.0-only)",
            Verdict.OBLIGATIONS,
            ("notice", "licence-text"),
        ),
        # A listed exception is judged as its base licence, inside an OR too.
        (
            "Apache-2.0 WITH LLVM-exception",
            Mode.STATIC,
            "Apache-2.0 WITH LLVM-exception",
            Verdict.OBLIGATIONS,
            ("notice", "licence-text", "carry-notice"),
        ),
        (
            "Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT",
            Mode.STATIC,
            "Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT",
            Verdict.OBLIGATIONS,
            ("notice", "licence-text"),
        ),
        (
            "MIT OR Apache-2.0 OR LGPL-2.1-or-later",
            Mode.STATIC,
            "MIT OR Apache-2.0 OR LGPL-2.1-or-later",
            Verdict.OBLIGATIONS,
            ("notice", "licence-text"),
        ),
        (
            "LGPL-2.1-or-later",
            Mode.STATIC,
            "LGPL-2.1-or-later",
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


_BSD3 = """\
Copyright 2007 Pallets

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are
met:

1.  Redistributions of source code must retain the above copyright
    notice, this list of conditions and the following disclaimer.

2.  Redistributions in binary form must reproduce the above copyright
    notice, this list of conditions and the following disclaimer in the
    documentation and/or other materials provided with the distribution.

3.  Neither the name of the copyright holder nor the names of its
    contributors may be used to endorse or promote products derived from
    this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
"AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED
TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED
TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""
_NAME_CLAUSE = _BSD3[_BSD3.index("3.  Neither") : _BSD3.index("THIS SOFTWARE")]
_BSD2 = _BSD3.replace(_NAME_CLAUSE, "")
_ZERO_BSD = """\
Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH
REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY
AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT,
INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR
OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
PERFORMANCE OF THIS SOFTWARE.
"""
_ISC = _ZERO_BSD.replace(
    "hereby granted.",
    "hereby granted, provided that the above copyright notice and this "
    "permission notice appear in all copies.",
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (_BSD3, "BSD-3-Clause"),
        (_BSD2, "BSD-2-Clause"),
        ("BSD 3-Clause License\n\n" + _BSD3, "BSD-3-Clause"),
        ("Copyright (c) 2001 A\nCopyright (c) 2002 B\n" + _BSD3, "BSD-3-Clause"),
        (
            _BSD3.replace("1.  ", "* ").replace("2.  ", "* ").replace("3.  ", "* "),
            "BSD-3-Clause",
        ),
        (
            _BSD3.replace("1.  ", "- ").replace("2.  ", "- ").replace("3.  ", "- "),
            "BSD-3-Clause",
        ),
        (
            _BSD3.replace("the copyright holder nor", "Encode OSS Ltd. nor"),
            "BSD-3-Clause",
        ),
        # A clause that starts on a line beginning "copyright notice" is still text.
        (_BSD3.replace("copyright\n    notice", "\ncopyright notice"), "BSD-3-Clause"),
        # BSD-4-Clause: the advertising clause stays.
        (
            _BSD3.replace(
                "3.  Neither",
                "3.  All advertising materials mentioning features or use of this "
                "software must display the following acknowledgement: This product "
                "includes software developed by Example.\n\n4.  Neither",
            ),
            None,
        ),
        (
            _BSD3.replace(
                "THIS SOFTWARE", "4. Not for commercial use.\n\nTHIS SOFTWARE"
            ),
            None,
        ),
        (_BSD3 + "\nThis software may not be used by any government agency.\n", None),
        ("This module was extracted from another package.\n" + _BSD3, None),
        (
            _BSD3.replace(
                "the copyright holder nor", "the holder, who may revoke it, nor"
            ),
            None,
        ),
        (_BSD3.replace("must retain", "should retain"), None),
        # sphinx-doc's LICENCE.rst: an rst title, its underline, and a pointer.
        (
            "License for sphinxcontrib-applehelp\n===================================\n\n"
            "Copyright (c) 2007-2019 by the Sphinx team\n"
            "(see https://github.com/sphinx-doc/sphinx/blob/master/AUTHORS).\n"
            "All rights reserved.\n\n" + _BSD2,
            "BSD-2-Clause",
        ),
        # A pointer line with words in it is not decoration.
        (
            "(see https://example.org/terms for further restrictions).\n" + _BSD2,
            None,
        ),
        # alabaster's reworded name clause is not BSD-3-Clause's.
        (
            _BSD3.replace(
                "Neither the name of the copyright holder nor the names of its\n"
                "    contributors may be used",
                "The names of the contributors may not be used",
            ),
            None,
        ),
        (_ZERO_BSD, "0BSD"),
        (
            "BSD Zero Clause Licence\n\nCopyright 2022, Adam Turner\n\n" + _ZERO_BSD,
            "0BSD",
        ),
        # ISC asks for the notice to be kept, so it is ISC, not 0BSD.
        (_ISC, "ISC"),
        # requests-oauthlib 2.0.0 ships this under a `BSD License` classifier.
        ("ISC License\n\nCopyright (c) 2014 Kenneth Reitz.\n\n" + _ISC, "ISC"),
        (_ISC.replace("and/or distribute", "and distribute"), "ISC"),
        (_ISC + "\nThis software may not be used for military purposes.\n", None),
        ("", None),
        (None, None),
    ],
)
def test_bsd_licence_needs_the_whole_text_to_be_one_bsd(text, expected):
    assert bsd_licence(text) == expected


@pytest.mark.parametrize(
    ("declared", "text", "spdx", "verdict"),
    [
        ("BSD License", _BSD3, "BSD-3-Clause", Verdict.OBLIGATIONS),
        ("BSD", _BSD2, "BSD-2-Clause", Verdict.OBLIGATIONS),
        ("BSD License", _ISC, "ISC", Verdict.OBLIGATIONS),
        ("BSD License", _BSD3 + "\nNo military use.\n", "BSD License", Verdict.ASK),
        ("BSD License", "UNKNOWN", "BSD License", Verdict.ASK),
        # Only a bare BSD is read from the file. A declared licence is never second-guessed.
        ("GPL-3.0-only", _BSD3, "GPL-3.0-only", Verdict.DENY),
    ],
)
def test_bare_bsd_classifier_is_judged_from_the_licence_file(
    policy, declared, text, spdx, verdict
):
    package = {
        "Name": "jinja2",
        "Version": "3.1.6",
        "License": declared,
        "LicenseFile": "jinja2-3.1.6.dist-info/licenses/LICENSE.txt",
        "LicenseText": text,
    }
    (finding,) = findings_for([package], {"jinja2": "3.1.6"}, policy, [])
    assert (finding.spdx, finding.verdict) == (spdx, verdict)
    if verdict is Verdict.OBLIGATIONS:
        assert finding.source == "license-file"
        assert finding.evidence == f"{package['LicenseFile']}: {spdx} text"


@pytest.mark.parametrize(
    ("declared", "overrides", "verdict", "rule"),
    [
        (
            "BUSL-1.1",
            [Override("dfe-schemas", "LicenseRef-HyperI", "ours", "0.2.23")],
            Verdict.ALLOW,
            "first-party",
        ),
        # Metadata claiming our id is what a squatted package would do.
        ("LicenseRef-HyperI", [], Verdict.ASK, "override-only"),
        ("LicenseRef-HyperI OR MIT", [], Verdict.ASK, "override-only"),
        ("BUSL-1.1", [], Verdict.ASK, "unrecognised"),
        # A pin to another version is no answer for this one.
        (
            "BUSL-1.1",
            [Override("dfe-schemas", "LicenseRef-HyperI", "ours", "0.2.22")],
            Verdict.ASK,
            "unrecognised",
        ),
    ],
)
def test_first_party_licence_passes_only_through_an_override(
    policy, declared, overrides, verdict, rule
):
    package = {"Name": "dfe-schemas", "Version": "0.2.23", "License": declared}
    (finding,) = findings_for([package], {"dfe-schemas": "0.2.23"}, policy, overrides)
    finding = restrict_override_only(finding, policy)
    assert (finding.verdict, finding.rule) == (verdict, rule)


def test_the_gate_holds_a_first_party_claim_from_metadata(
    tmp_path, monkeypatch, policy
):
    monkeypatch.chdir(tmp_path)
    package = {"Name": "squat", "Version": "1.0", "License": "LicenseRef-HyperI"}
    findings = findings_for([package], {"squat": "1.0"}, policy, [])
    assert findings[0].verdict is Verdict.ALLOW
    assert finish(findings, [], policy, {}, "blocking") is False
    (dep,) = json.loads(REPORT_PATH.read_text(encoding="utf-8"))["deps"]
    assert (dep["verdict"], dep["rule"]) == ("ask", "override-only")


def test_override_only_groups_count_in_the_policy_digest(tmp_path, policy):
    table = tmp_path / "policy.yaml"
    text = _POLICY_PATH.read_text(encoding="utf-8")
    table.write_text(text.replace("override_only: true", ""), encoding="utf-8")
    loose = load_policy(table)
    assert loose.override_only == frozenset()
    assert policy.override_only == frozenset({"first-party"})
    assert loose.digest != policy.digest


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
    assert doc["hyperi_ci"] == hyperi_ci.__version__
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


def test_notices_list_every_shipped_dependency(policy):
    text = _notices(policy)
    names = [line for line in text.splitlines() if line.endswith(")")]
    # An unanswered ask still ships its licence text, so it is listed too.
    assert names == [
        "Apache-Thing 2 (Apache-2.0)",
        "mit-with-notice 3 (MIT)",
        "public 4 (CC0-1.0)",
        "pysigma-backend-clickhouse 1.1.1 (unknown)",
        "zlib-ish 1 (MIT)",
    ]
    assert "Apache text\n\nNOTICE:\n\nApache NOTICE" in text
    assert "GNU LESSER GENERAL PUBLIC LICENSE" in text
    assert "not carried for MIT" not in text


def test_notices_are_byte_identical_whatever_the_input_order(policy):
    findings = findings_for(_NOTICE_PACKAGES, _NOTICE_RUNTIME, policy, [])
    entries = notice_entries(_NOTICE_PACKAGES, findings)
    assert render_notices(entries) == render_notices(list(reversed(entries)))


_GNU_TEXTS = [
    "GPL-2.0-only",
    "GPL-3.0-only",
    "LGPL-2.0-only",
    "LGPL-2.1-only",
    "LGPL-3.0-only",
]


@pytest.mark.parametrize(
    ("licences", "texts"),
    [
        (["LGPL-3.0-or-later"], ["GPL-3.0-only", "LGPL-3.0-only"]),
        (["LGPL-2.1-only", "LGPL-2.1-or-later"], ["GPL-2.0-only", "LGPL-2.1-only"]),
        (
            ["LGPL-2.0-or-later", "LGPL-3.0-only"],
            ["GPL-2.0-only", "GPL-3.0-only", "LGPL-2.0-only", "LGPL-3.0-only"],
        ),
        (["GNU Library or Lesser General Public License (LGPL)"], _GNU_TEXTS),
        # A static LGPL link is ask, so it carries no obligation and no texts.
        (["MIT", "Apache-2.0"], []),
    ],
)
def test_lgpl_dependencies_bring_their_gnu_texts_once(policy, licences, texts):
    packages = [
        {"Name": f"dep{i}", "Version": "1", "License": lic, "LicenseText": "text"}
        for i, lic in enumerate(licences)
    ]
    runtime = {f"dep{i}": "1" for i in range(len(licences))}
    findings = findings_for(packages, runtime, policy, [])
    notices = render_notices(notice_entries(packages, findings))
    headings = {
        n: notices.count(f"\n{n}, for the LGPL dependencies") for n in _GNU_TEXTS
    }
    assert headings == {n: int(n in texts) for n in _GNU_TEXTS}
    for name in texts:
        bundled = (_POLICY_PATH.parent / "licence-texts" / f"{name}.txt").read_text()
        assert bundled.strip() in notices


def test_bundled_gnu_texts_are_the_published_ones():
    # sha256 of hyperi-io/licensing third-party/*.txt at af32f46, sync header removed.
    expected = {
        "GPL-2.0-only": "aaf135472f81c5b4a0dca9367e5bb5e9750032b5bebe5442b36e4c0a47430df3",
        "GPL-3.0-only": "fb981668c18a279e285fc4d83fba1e836cc84dd4daa73c9697d3cfd2d8aca6e0",
        "LGPL-2.0-only": "86dc99d7e5060915ab1dfc1378b7dd351c62088bfa74067e8aa1868c6fdba7d8",
        "LGPL-2.1-only": "5749785c8bdefafcb5d798270ed0a967036fe2ca63dcedade1627565dfef81d2",
        "LGPL-3.0-only": "996af0513df21f7496288951c41428a03c174e9e4a9d63665c57d670f845ccb1",
    }
    texts = _POLICY_PATH.parent / "licence-texts"
    actual = {
        path.stem: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in texts.glob("*.txt")
    }
    assert actual == expected


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
    # mit-with-notice ships no licence file, so the repo supplies one.
    (tmp_path / "mit.txt").write_text("MIT text from the repo", encoding="utf-8")
    overrides = [Override("mit-with-notice", "MIT", "upstream", None, "mit.txt")]
    findings = findings_for(_NOTICE_PACKAGES, _NOTICE_RUNTIME, policy, [])
    _, entries = fill_and_judge(
        findings, notice_entries(_NOTICE_PACKAGES, findings), policy, overrides
    )
    check_notices(entries, "warn")
    current = (tmp_path / ".hyperi-ci" / "THIRD-PARTY-NOTICES").read_text(
        encoding="utf-8"
    )
    assert "MIT text from the repo" in current
    if repo_copy is not None:
        text = current if repo_copy == "current" else "old\n"
        (tmp_path / "THIRD-PARTY-NOTICES").write_text(text, encoding="utf-8")
    assert check_notices(entries, mode) is passes


@pytest.mark.parametrize(("mode", "passes"), [("blocking", False), ("warn", True)])
def test_missing_licence_text_fails_instead_of_shipping_a_placeholder(
    policy, tmp_path, monkeypatch, mode, passes
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".hyperi-ci").mkdir()
    # No file, no repository, and no SPDX standard text for a LicenseRef.
    packages = [
        {"Name": "vendor", "Version": "1", "License": "LicenseRef-Vendor"},
    ]
    findings = findings_for(packages, {"vendor": "1"}, policy, [])
    _, entries = fill_and_judge(findings, notice_entries(packages, findings), policy)
    assert check_notices(entries, mode) is passes


@pytest.mark.parametrize(
    ("declared", "upstream_text", "spdx", "verdict"),
    [
        ("BSD License", _ZERO_BSD, "0BSD", Verdict.OBLIGATIONS),
        ("BSD License", _BSD3, "BSD-3-Clause", Verdict.OBLIGATIONS),
        ("BSD License", _BSD3 + "\nNo military use.\n", "BSD License", Verdict.ASK),
        ("BSD License", None, "BSD License", Verdict.ASK),
        # A declared licence is never second-guessed by the text.
        ("GPL-3.0-only", _ZERO_BSD, "GPL-3.0-only", Verdict.DENY),
    ],
)
def test_a_bare_bsd_is_judged_from_text_fetched_from_its_repository(
    policy, declared, upstream_text, spdx, verdict
):
    packages = [{"Name": "sphinxcontrib-jquery", "Version": "4.1", "License": declared}]
    findings = findings_for(packages, {"sphinxcontrib-jquery": "4.1"}, policy, [])
    entries = notice_entries(packages, findings)
    licence = f"Licence text from repo at tag v4.1: LICENCE.\n\n{upstream_text}"
    upstream = Upstream(licence if upstream_text else None)
    judged, filled = fill_and_judge(findings, entries, policy, (), lambda *_: upstream)
    (finding,) = judged
    assert (finding.spdx, finding.verdict) == (spdx, verdict)
    assert filled[0].finding == finding
    if spdx != declared:
        assert finding.source == "license-file"
        assert finding.evidence.endswith("Licence text from repo at tag v4.1: LICENCE.")


_APPLEHELP_LINKS = [
    (
        "Changelog",
        "https://github.com/sphinx-doc/sphinxcontrib-applehelp/blob/master/CHANGES.rst",
    ),
    ("Code", "https://github.com/sphinx-doc/sphinxcontrib-applehelp/"),
    ("Download", "https://pypi.org/project/sphinxcontrib-applehelp/"),
    ("Homepage", "https://www.sphinx-doc.org/"),
    ("Issue tracker", "https://github.com/sphinx-doc/sphinx/issues/"),
]


@pytest.mark.parametrize(
    ("links", "url", "expected"),
    [
        (
            _APPLEHELP_LINKS,
            "https://www.sphinx-doc.org/",
            "https://github.com/sphinx-doc/sphinxcontrib-applehelp/",
        ),
        # An issue tracker on a forge is another repository's.
        (
            [("Issue tracker", "https://github.com/sphinx-doc/sphinx/issues/")],
            "https://www.sphinx-doc.org/",
            "https://www.sphinx-doc.org/",
        ),
        ([], "https://github.com/pallets/jinja/", "https://github.com/pallets/jinja/"),
        (
            [("Docs", "https://jinja.palletsprojects.com/")],
            None,
            "https://jinja.palletsprojects.com/",
        ),
        (
            [("Source", "https://gitlab.com/g/p")],
            "https://example.org/",
            "https://gitlab.com/g/p",
        ),
        ([], None, None),
    ],
)
def test_the_repository_is_the_source_link_before_the_homepage(links, url, expected):
    assert repository(links, url) == expected


def _crate(root, name, licence, *, kinds=("lib",), files=None, license_file=None):
    """Write a crate dir with its licence files and return its metadata package."""
    crate_dir = root / f"{name}-1.0.0"
    crate_dir.mkdir()
    (crate_dir / "Cargo.toml").write_text("", encoding="utf-8")
    for file_name, text in (files or {}).items():
        (crate_dir / file_name).write_text(text, encoding="utf-8")
    return {
        "id": f"{name}-id",
        "name": name,
        "version": "1.0.0",
        "license": licence,
        "license_file": license_file,
        "manifest_path": str(crate_dir / "Cargo.toml"),
        "targets": [{"kind": list(kinds)}],
    }


@pytest.fixture
def rust_metadata(tmp_path):
    """Return a workspace graph: normal, dev, build and proc-macro edges."""
    packages = [
        _crate(tmp_path, "app", "BUSL-1.1"),
        _crate(
            tmp_path, "serde", "MIT OR Apache-2.0", files={"LICENSE-MIT": "MIT text"}
        ),
        _crate(tmp_path, "ring", None, files={"LICENSE": "ring licence text"}),
        _crate(tmp_path, "tokio", "MIT", files={"LICENSE": "tokio MIT"}),
        _crate(tmp_path, "derive", "MIT", kinds=("proc-macro",)),
        _crate(tmp_path, "syn", "MIT"),
        _crate(tmp_path, "proptest", "MIT"),
        _crate(tmp_path, "cc", "MIT"),
        _crate(
            tmp_path,
            "aws-lc",
            "Apache-2.0",
            files={"LICENSE": "Apache text", "NOTICE": "AWS NOTICE"},
        ),
    ]

    return {"packages": packages, "workspace_members": ["app-id"]}


# What `cargo tree -e normal,no-proc-macro` prints for app: no dev (proptest),
# build (cc) or proc-macro (derive, syn) crate, and tokio twice, deduped.
_TREE = """\
app v1.0.0 (/work/app)
serde v1.0.0
tokio v1.0.0
ring v1.0.0
aws-lc v1.0.0
tokio v1.0.0 (*)
"""


def _shipped(metadata, tree=_TREE):
    linked = rust_licenses.tree_packages(tree)
    return rust_licenses.shipped_packages(metadata, linked)


def test_rust_ships_what_cargo_tree_links_and_nothing_else(rust_metadata):
    assert [p["name"] for p in _shipped(rust_metadata)] == [
        "aws-lc",
        "ring",
        "serde",
        "tokio",
    ]


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("serde v1.0.219\n", {("serde", "1.0.219")}),
        ("app v0.1.0 (/work/app)\n", {("app", "0.1.0")}),
        ("tokio v1.48.0 (*)\n", {("tokio", "1.48.0")}),
        ("x v2.0.0 (https://github.com/o/x#abc123)\n", {("x", "2.0.0")}),
        ("", set()),
        ("\nwarning: nothing to print.\n", set()),
        ("serde\n", set()),
    ],
)
def test_tree_packages_reads_name_and_version(text, found):
    assert rust_licenses.tree_packages(text) == found


def test_rust_crates_a_target_does_not_link_are_not_shipped(rust_metadata):
    tree = "app v1.0.0 (/work/app)\nserde v1.0.0\n"
    assert [p["name"] for p in _shipped(rust_metadata, tree)] == ["serde"]


def test_tree_command_lists_one_target_with_normal_edges_only():
    cmd = rust_licenses.tree_command("aarch64-unknown-linux-gnu")
    assert cmd[cmd.index("--target") + 1] == "aarch64-unknown-linux-gnu"
    assert cmd[cmd.index("--edges") + 1] == "normal,no-proc-macro"
    assert {"--locked", "--all-features", "--workspace"} <= set(cmd)


def test_rust_crates_are_judged_as_static_links(policy, rust_metadata, monkeypatch):
    monkeypatch.chdir(rust_metadata["packages"][0]["manifest_path"].rsplit("/", 2)[0])
    shipped = _shipped(rust_metadata)
    findings = {f.name: f for f in rust_licenses.findings_for(shipped, policy, [])}
    assert {f.mode for f in findings.values()} == {Mode.STATIC}
    assert findings["serde"].verdict is Verdict.OBLIGATIONS
    ring = findings["ring"]
    assert (ring.verdict, ring.source) == (Verdict.ASK, "license-file")
    assert ring.evidence == "ring-1.0.0/LICENSE: ring licence text"
    assert findings["tokio"].line() == (
        "licence: crates tokio 1.0.0 MIT static -> allow-with-obligations "
        "[notice, licence-text] (rule permissive, source metadata)"
    )


@pytest.mark.parametrize(
    ("declared", "spdx", "verdict"),
    [
        ("MIT/Apache-2.0", "MIT OR Apache-2.0", Verdict.OBLIGATIONS),
        ("Apache-2.0 / MIT", "Apache-2.0 OR MIT", Verdict.OBLIGATIONS),
        ("MIT/GPL-3.0-only", "MIT OR GPL-3.0-only", Verdict.OBLIGATIONS),
        ("GPL-3.0-only/AGPL-3.0-only", "GPL-3.0-only OR AGPL-3.0-only", Verdict.DENY),
    ],
)
def test_legacy_slash_crate_licence_reads_as_or(policy, declared, spdx, verdict):
    crate = {"name": "core_detect", "version": "1.0.0", "license": declared}
    (finding,) = rust_licenses.findings_for([crate], policy, [])
    assert (finding.spdx, finding.verdict) == (spdx, verdict)


def test_rust_notices_carry_crate_licence_and_notice_files(policy, rust_metadata):
    shipped = _shipped(rust_metadata)
    findings = rust_licenses.findings_for(shipped, policy, [])
    text = render_notices(rust_licenses.notice_entries(shipped, findings))
    assert "aws-lc 1.0.0 (Apache-2.0)\n" in text
    assert "Apache text\n\nNOTICE:\n\nAWS NOTICE" in text
    assert "MIT text" in text and "tokio MIT" in text
    # ring is still an ask, and its licence text ships all the same.
    assert "ring licence text" in text


@pytest.fixture
def npm_records(tmp_path):
    """Return license-checker records with real package dirs under tmp_path."""

    def package(name, licences, files):
        package_dir = tmp_path / "node_modules" / name
        package_dir.mkdir(parents=True)
        for file_name, text in files.items():
            (package_dir / file_name).write_text(text, encoding="utf-8")
        first = next(iter(files), None)
        return {
            "licenses": licences,
            "path": str(package_dir),
            "licenseFile": str(package_dir / first) if first else None,
        }

    return {
        "@scope/pkg@1.2.3": package("@scope/pkg", "MIT", {"LICENSE": "scope MIT"}),
        "jszip@3.10.1": package(
            "jszip", "(MIT OR GPL-3.0-or-later)", {"LICENSE.markdown": "dual"}
        ),
        "old@1.0.0": package("old", ["MIT", "Apache-2.0"], {"LICENSE": "old"}),
        "guessed@2.0.0": package("guessed", "BSD*", {"LICENSE": "BSD-ish text"}),
        "isarray@1.0.0": package("isarray", "MIT", {"README.md": "# isarray"}),
        "aws@3.0.0": package(
            "aws", "Apache-2.0", {"LICENSE": "Apache text", "NOTICE": "AWS NOTICE"}
        ),
    }


def test_npm_packages_are_judged_as_imports(policy, npm_records, monkeypatch):
    monkeypatch.chdir(next(iter(npm_records.values()))["path"].rsplit("/", 3)[0])
    findings = {f.name: f for f in npm_licenses.findings_for(npm_records, policy, [])}
    assert set(findings) == {"@scope/pkg", "jszip", "old", "guessed", "isarray", "aws"}
    assert findings["@scope/pkg"].version == "1.2.3"
    assert {f.mode for f in findings.values()} == {Mode.IMPORT}
    assert findings["jszip"].verdict is Verdict.OBLIGATIONS
    assert findings["old"].spdx == "MIT OR Apache-2.0"
    # A licence license-checker guessed from text is never taken as declared.
    guessed = findings["guessed"]
    assert (guessed.verdict, guessed.source) == (Verdict.ASK, "license-file")
    assert guessed.evidence == "node_modules/guessed/LICENSE: BSD-ish text"


def test_npm_notices_skip_a_readme_and_carry_a_notice(policy, npm_records):
    findings = npm_licenses.findings_for(npm_records, policy, [])
    text = render_notices(npm_licenses.notice_entries(npm_records, findings))
    assert "# isarray" not in text
    assert "isarray 1.0.0 (MIT)" in text and "[no licence file in the package]" in text
    assert "Apache text\n\nNOTICE:\n\nAWS NOTICE" in text


# The shape `reuse lint --json` 6.2.0 and 6.3.0 both emit, trimmed to the fields read.
_REUSE_LINT = {
    "files": [
        {"path": "lib/no_header.c", "spdx_expressions": []},
        {"path": "lib/mit.c", "spdx_expressions": [{"value": "MIT"}]},
        {
            "path": "lib/both.c",
            "spdx_expressions": [{"value": "MIT"}, {"value": "LGPL-2.1-only"}],
        },
    ]
}


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ([{"mode": "vendored"}], "needs a path"),
        (["vendor"], "needs a path"),
        ([{"path": "vendor", "mode": "linked"}], "unknown mode"),
        ([{"path": "vendor", "mode": "import"}], "vendored or modified"),
        ([{"path": "vendor", "licence": "MIT"}], "no reason"),
    ],
)
def test_vendored_entries_are_validated(raw, message):
    with pytest.raises(PolicyError, match=message):
        vendored_licenses.parse_vendored(raw)


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (
            {"path": "vendor", "mode": "vendored"},
            {
                "vendor/lib/both.c": (Verdict.DENY, "file-header"),
                "vendor/lib/mit.c": (Verdict.OBLIGATIONS, "file-header"),
                "vendor/lib/no_header.c": (Verdict.ASK, "missing-header"),
            },
        ),
        (
            {"path": "vendor", "licence": "MIT", "reason": "upstream LICENSE is MIT"},
            {
                "vendor/lib/both.c": (Verdict.DENY, "file-header"),
                "vendor/lib/mit.c": (Verdict.OBLIGATIONS, "file-header"),
                "vendor/lib/no_header.c": (Verdict.OBLIGATIONS, "override"),
            },
        ),
    ],
)
def test_copied_in_files_are_judged_one_by_one(policy, entry, expected):
    (parsed,) = vendored_licenses.parse_vendored([entry])
    findings = vendored_licenses.findings_for(parsed, _REUSE_LINT, policy)
    assert {f.name: (f.verdict, f.source) for f in findings} == expected
    # A header the file carries always wins over the directory's answer.
    assert {f.mode for f in findings} == {Mode.VENDORED}
