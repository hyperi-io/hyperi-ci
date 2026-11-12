# Project:   HyperI CI
# File:      tests/unit/test_dep_licenses.py
# Purpose:   Golden table for the dependency-licence table and the language readers
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Golden table for the dependency-licence table and the language readers."""

import hashlib
import json

import pytest

import hyperi_ci
from hyperi_ci.config import load_config
from hyperi_ci.languages.python import licenses
from hyperi_ci.languages.python.licenses import (
    declared_licence,
    findings_for,
    notice_entries,
    runtime_packages,
)
from hyperi_ci.languages.rust import licenses as rust_licenses
from hyperi_ci.languages.typescript import licenses as npm_licenses
from hyperi_ci.quality import vendored_licenses
from hyperi_ci.quality.dep_licenses import (
    NOTICES_OUTPUT,
    POLICY_PATH,
    REPORT_PATH,
    Mode,
    Override,
    PolicyError,
    Verdict,
    blocking,
    find_override,
    finish,
    judge,
    judge_override,
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

OK = Verdict.OBLIGATIONS
ASK = Verdict.ASK
DENY = Verdict.DENY
N = ("notice",)
LGPL = ("notice", "lgpl-texts", "replaceable")
IMPORT = Mode.IMPORT
STATIC = Mode.STATIC


@pytest.fixture(scope="module")
def policy():
    return load_policy()


@pytest.mark.parametrize(
    ("licence", "mode", "spdx", "verdict", "obligations"),
    [
        ("LGPL-3.0-only", IMPORT, "LGPL-3.0-only", OK, LGPL),
        # A static or copied-in LGPL ships no relinkable objects.
        ("LGPL-3.0-only", Mode.VENDORED, "LGPL-3.0-only", ASK, ()),
        ("LGPL-3.0-only", STATIC, "LGPL-3.0-only", ASK, ()),
        ("LGPL-2.1-or-later", STATIC, "LGPL-2.1-or-later", ASK, ()),
        (
            "GNU Lesser General Public License v2 or later (LGPLv2+)",
            IMPORT,
            "LGPL-2.0-or-later",
            OK,
            LGPL,
        ),
        ("EPL-2.0", IMPORT, "EPL-2.0", OK, N),
        ("EPL-2.0", STATIC, "EPL-2.0", ASK, ()),
        ("GPL-3.0-only", IMPORT, "GPL-3.0-only", DENY, ()),
        ("AGPL-3.0-or-later", IMPORT, "AGPL-3.0-or-later", DENY, ()),
        ("SSPL-1.0", IMPORT, "SSPL-1.0", DENY, ()),
        (
            "GNU General Public License (GPL)",
            IMPORT,
            "LicenseRef-GPL-Unversioned",
            DENY,
            (),
        ),
        # Third-party source-available or share-alike: a human decides.
        ("BUSL-1.1", IMPORT, "BUSL-1.1", ASK, ()),
        ("Elastic-2.0", STATIC, "Elastic-2.0", ASK, ()),
        ("CC-BY-SA-4.0", IMPORT, "CC-BY-SA-4.0", ASK, ()),
        ("Apache-2.0", IMPORT, "Apache-2.0", OK, N),
        ("Apache Software License", STATIC, "Apache-2.0", OK, N),
        ("MIT", IMPORT, "MIT", OK, N),
        ("mit", IMPORT, "MIT", OK, N),
        ("CC0-1.0", Mode.MODIFIED, "CC0-1.0", OK, N),
        ("MPL-2.0", Mode.MODIFIED, "MPL-2.0", OK, ("notice", "mpl-source")),
        ("MIT AND MPL-2.0", IMPORT, "MIT AND MPL-2.0", OK, ("notice", "mpl-source")),
        # The classifier does not say which BSD, so it carries BSD-4-Clause's duty.
        (
            "BSD License",
            IMPORT,
            "LicenseRef-BSD-Unspecified",
            OK,
            ("notice", "advertising"),
        ),
        ("BSD-4-Clause", STATIC, "BSD-4-Clause", OK, ("notice", "advertising")),
        # Spellings real wheels use (protobuf, uritemplate).
        ("3-Clause BSD License", IMPORT, "BSD-3-Clause", OK, N),
        ("BSD 3-Clause OR Apache-2.0", IMPORT, "BSD-3-Clause OR Apache-2.0", OK, N),
        (
            "BSD 3-Clause OR Apache-2.0 AND MIT",
            IMPORT,
            "BSD-3-Clause OR (Apache-2.0 AND MIT)",
            OK,
            N,
        ),
        ("Some Licence OR Apache-2.0", IMPORT, "Some Licence OR Apache-2.0", OK, N),
        ("ISC License (ISCL)", IMPORT, "ISC", OK, N),
        ("CDLA-Permissive-2.0", STATIC, "CDLA-Permissive-2.0", OK, N),
        # OR takes the best term, AND the worst.
        (
            "Apache-2.0 OR GPL-2.0-or-later",
            IMPORT,
            "Apache-2.0 OR GPL-2.0-or-later",
            OK,
            N,
        ),
        ("Apache Software License; MIT License", IMPORT, "Apache-2.0 OR MIT", OK, N),
        ("mit; apache-2.0", IMPORT, "MIT OR Apache-2.0", OK, N),
        ("MIT AND GPL-3.0-only", IMPORT, "MIT AND GPL-3.0-only", DENY, ()),
        ("Apache-2.0 AND CNRI-Python", IMPORT, "Apache-2.0 AND CNRI-Python", OK, N),
        # Nothing a human has not answered is ever ok.
        (None, IMPORT, None, ASK, ()),
        ("UNKNOWN", IMPORT, None, ASK, ()),
        ("", IMPORT, None, ASK, ()),
        ("LicenseRef-Proprietary", IMPORT, "LicenseRef-Proprietary", ASK, ()),
        ("Some Custom Licence v1", IMPORT, "Some Custom Licence v1", ASK, ()),
        (
            "MIT AND LicenseRef-Proprietary",
            IMPORT,
            "MIT AND LicenseRef-Proprietary",
            ASK,
            (),
        ),
        # An unknown alternative may be the permissive one, so it is never a deny.
        (
            "LicenseRef-Foo OR GPL-3.0-only",
            IMPORT,
            "LicenseRef-Foo OR GPL-3.0-only",
            ASK,
            (),
        ),
        ("LicenseRef-Foo OR MIT", IMPORT, "LicenseRef-Foo OR MIT", OK, N),
        # A runtime exception clears its base licence, a base on no list stays unknown.
        (
            "GPL-2.0-only WITH Classpath-exception-2.0",
            IMPORT,
            "GPL-2.0-only WITH Classpath-exception-2.0",
            OK,
            N,
        ),
        # scipy's wheel bundles libgfortran under this.
        (
            "GPL-3.0-or-later WITH GCC-exception-3.1",
            STATIC,
            "GPL-3.0-or-later WITH GCC-exception-3.1",
            OK,
            N,
        ),
        (
            "Apache-2.0 WITH LLVM-exception",
            STATIC,
            "Apache-2.0 WITH LLVM-exception",
            OK,
            N,
        ),
        (
            "LicenseRef-Foo WITH LLVM-exception",
            STATIC,
            "LicenseRef-Foo WITH LLVM-exception",
            ASK,
            (),
        ),
        (
            "Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT",
            STATIC,
            "Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT",
            OK,
            N,
        ),
        # Bracketed expressions: AND binds tighter than OR.
        (
            "(MIT OR Apache-2.0) AND BSD-3-Clause",
            IMPORT,
            "(MIT OR Apache-2.0) AND BSD-3-Clause",
            OK,
            N,
        ),
        (
            "(MIT OR Apache-2.0) AND GPL-3.0-only",
            IMPORT,
            "(MIT OR Apache-2.0) AND GPL-3.0-only",
            DENY,
            (),
        ),
        (
            "(MIT OR Apache-2.0) AND LicenseRef-Foo",
            IMPORT,
            "(MIT OR Apache-2.0) AND LicenseRef-Foo",
            ASK,
            (),
        ),
        (
            "MIT AND (GPL-3.0-only OR LicenseRef-Foo)",
            IMPORT,
            "MIT AND (GPL-3.0-only OR LicenseRef-Foo)",
            ASK,
            (),
        ),
        # npm wraps a lone expression in brackets.
        ("(MIT OR GPL-3.0-or-later)", IMPORT, "MIT OR GPL-3.0-or-later", OK, N),
        ("(MIT AND Zlib)", IMPORT, "MIT AND Zlib", OK, N),
        ("(MIT) OR (GPL-3.0-only)", IMPORT, "MIT OR GPL-3.0-only", OK, N),
        (
            "MIT OR Apache-2.0 OR LGPL-2.1-or-later",
            STATIC,
            "MIT OR Apache-2.0 OR LGPL-2.1-or-later",
            OK,
            N,
        ),
        # Metadata claiming our id is what a squatted package would do.
        ("LicenseRef-HyperI", IMPORT, "LicenseRef-HyperI", ASK, ()),
    ],
)
def test_judge_golden_table(policy, licence, mode, spdx, verdict, obligations):
    got_spdx, got_verdict, got_obligations, _ = judge(licence, mode, policy)
    assert (got_spdx, got_verdict, got_obligations) == (spdx, verdict, obligations)


def test_an_id_on_two_lists_is_refused(tmp_path):
    table = tmp_path / "licensing.yaml"
    table.write_text("ok: [MIT]\npoisons: [MIT]\n", encoding="utf-8")
    with pytest.raises(PolicyError, match="in both"):
        load_policy(table)


def test_the_digest_covers_the_whole_table(tmp_path, policy):
    table = tmp_path / "licensing.yaml"
    text = POLICY_PATH.read_text(encoding="utf-8")
    table.write_text(
        text.replace("first_party: [LicenseRef-HyperI]", ""), encoding="utf-8"
    )
    assert load_policy(table).digest != policy.digest


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
    assert finding.verdict is ASK
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
        assert (finding.verdict, finding.source, finding.obligations) == (
            OK,
            "override",
            LGPL,
        )
    else:
        assert finding.verdict is ASK


@pytest.mark.parametrize(
    ("expression", "metadata", "classifier", "expected"),
    [
        ("MIT", "BSD-3-Clause", "BSD License", "MIT"),
        # cloudpickle 3.1.2: an SPDX License field beats the bare classifier.
        ("UNKNOWN", "BSD-3-Clause", "BSD License", "BSD-3-Clause"),
        # scipy 1.18.1 puts its licence text in the License field.
        ("UNKNOWN", "Copyright (c) 2001-2002 Enthought", "BSD License", "BSD License"),
        ("UNKNOWN", "MIT License", "UNKNOWN", "MIT License"),
        ("UNKNOWN", "UNKNOWN", "UNKNOWN", "UNKNOWN"),
        ("", "", "", "UNKNOWN"),
    ],
)
def test_declared_licence_takes_the_most_specific_field(
    expression, metadata, classifier, expected
):
    record = {
        "License-Expression": expression,
        "License-Metadata": metadata,
        "License-Classifier": classifier,
    }
    assert declared_licence(record) == expected


@pytest.mark.parametrize(
    ("declared", "overrides", "verdict", "rule"),
    [
        (
            "BUSL-1.1",
            [Override("dfe-schemas", "LicenseRef-HyperI", "ours", "0.2.23")],
            OK,
            "first-party",
        ),
        ("LicenseRef-HyperI", [], ASK, "override-only"),
        ("LicenseRef-HyperI OR LicenseRef-Foo", [], ASK, "override-only"),
        ("BUSL-1.1", [], ASK, "human"),
        # A pin to another version is no answer for this one.
        (
            "BUSL-1.1",
            [Override("dfe-schemas", "LicenseRef-HyperI", "ours", "0.2.22")],
            ASK,
            "human",
        ),
    ],
)
def test_first_party_licence_passes_only_through_an_override(
    policy, declared, overrides, verdict, rule
):
    package = {"Name": "dfe-schemas", "Version": "0.2.23", "License": declared}
    (finding,) = findings_for([package], {"dfe-schemas": "0.2.23"}, policy, overrides)
    assert (finding.verdict, finding.rule) == (verdict, rule)


@pytest.mark.parametrize(
    ("licence", "mode", "spdx", "verdict", "rule", "override_only"),
    [
        ("MIT", "import", "MIT", "allow-with-obligations", "ok", False),
        (
            "Apache Software License",
            "static",
            "Apache-2.0",
            "allow-with-obligations",
            "ok",
            False,
        ),
        (
            "LicenseRef-HyperI",
            "import",
            "LicenseRef-HyperI",
            "allow-with-obligations",
            "first-party",
            True,
        ),
        (
            "MIT OR GPL-3.0-only",
            "import",
            "MIT OR GPL-3.0-only",
            "allow-with-obligations",
            "ok",
            False,
        ),
        ("GPL-3.0-only", "import", "GPL-3.0-only", "deny", "poisons", False),
        ("LGPL-3.0-only", "static", "LGPL-3.0-only", "ask", "static-link", False),
        ("Not-A-Licence", "import", "Not-A-Licence", "ask", "unrecognised", False),
        ("", "import", None, "ask", "undeclared", False),
    ],
)
def test_judge_override_answers_with_the_report_digest(
    policy, licence, mode, spdx, verdict, rule, override_only
):
    answer = judge_override(licence, Mode(mode), policy)
    assert (
        answer["spdx"],
        answer["verdict"],
        answer["rule"],
        answer["override_only"],
    ) == (spdx, verdict, rule, override_only)
    assert answer["policy"] == policy.digest


@pytest.mark.parametrize(
    ("args", "config", "code", "verdict"),
    [
        (["LGPL-3.0-only", "--mode", "static"], "", 0, "ask"),
        # A per-repo policy block is no longer read, so it changes nothing.
        (
            ["LGPL-3.0-only", "--mode", "static"],
            "    policy:\n      lgpl:\n        static: {verdict: allow, reason: r}\n",
            0,
            "ask",
        ),
        (["MIT", "--mode", "linked"], "", 2, None),
        (
            ["MIT"],
            "    overrides:\n      - {name: x}\n",
            2,
            None,
        ),
    ],
    ids=["shipped-table", "repo-policy-ignored", "bad-mode", "bad-override"],
)
def test_licenses_judge_command(tmp_path, args, config, code, verdict, policy):
    from typer.testing import CliRunner

    from hyperi_ci.cli import app

    text = "quality:\n  dep_licenses:\n    mode: blocking\n" + config
    (tmp_path / ".hyperi-ci.yaml").write_text(text, encoding="utf-8")
    result = CliRunner().invoke(
        app, ["licenses", "judge", *args, "--json", "-C", str(tmp_path)]
    )
    assert result.exit_code == code, result.output
    if verdict is not None:
        answer = json.loads(result.stdout.splitlines()[-1])
        assert (answer["verdict"], answer["policy"]) == (verdict, policy.digest)


def test_runtime_package_not_installed_is_ask(policy):
    (finding,) = findings_for([], {"ghost": "1.0"}, policy, [])
    assert (finding.verdict, finding.source) == (ASK, "missing")


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
    assert (finding.version, finding.verdict) == ("0.3.0", DENY)


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
        "[notice, mpl-source] (rule ok, source metadata)"
    )
    doc = json.loads(json.dumps(report(findings, policy, {"pip-licenses": "5.5.5"})))
    assert doc["schema"] == 1
    assert doc["hyperi_ci"] == hyperi_ci.__version__
    assert doc["policy"] == policy.digest
    assert doc["deps"][0]["verdict"] == "allow-with-obligations"
    assert doc["deps"][0]["obligations"] == ["notice", "mpl-source"]


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
        "Name": "mit-no-file",
        "Version": "3",
        "License": "MIT",
        "LicenseText": "UNKNOWN",
        "NoticeText": "a NOTICE beside an MIT licence",
    },
    {
        "Name": "certifi",
        "Version": "4",
        "License": "MPL-2.0",
        "LicenseText": "MPL text",
        "NoticeText": "UNKNOWN",
    },
    _GOLDEN_LGPL,
]
_NOTICE_RUNTIME = {
    "zlib-ish": "1",
    "apache-thing": "2",
    "mit-no-file": "3",
    "certifi": "4",
    "pysigma-backend-clickhouse": "1.1.1",
}


def _entries(policy):
    findings = findings_for(_NOTICE_PACKAGES, _NOTICE_RUNTIME, policy, [])
    return findings, notice_entries(_NOTICE_PACKAGES, findings)


def test_notices_list_every_shipped_dependency(policy):
    text = render_notices(_entries(policy)[1])
    names = [line for line in text.splitlines() if line.endswith(")")]
    # An unanswered ask still ships its licence text, so it is listed too.
    assert names == [
        "Apache-Thing 2 (Apache-2.0)",
        "certifi 4 (MPL-2.0)",
        "mit-no-file 3 (MIT)",
        "pysigma-backend-clickhouse 1.1.1 (unknown)",
        "zlib-ish 1 (MIT)",
    ]
    assert "Apache text\n\nNOTICE:\n\nApache NOTICE" in text
    assert "a NOTICE beside an MIT licence" in text
    assert "GNU LESSER GENERAL PUBLIC LICENSE" in text
    assert "MPL text\n\nSource: https://pypi.org/project/certifi/4/" in text
    # A package that ships no licence file gets the SPDX standard text.
    assert "SPDX standard text for MIT" in text
    assert "Permission is hereby granted, free of charge" in text


def test_notices_are_byte_identical_whatever_the_input_order(policy):
    entries = _entries(policy)[1]
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
        bundled = (POLICY_PATH.parent / "licence-texts" / f"{name}.txt").read_text()
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
    texts = POLICY_PATH.parent / "licence-texts"
    actual = {
        path.stem: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in texts.glob("*.txt")
    }
    assert actual == expected


@pytest.mark.parametrize(("mode", "passes"), [("blocking", False), ("warn", True)])
def test_finish_writes_the_report_and_notices_and_holds_each_ask(
    policy, tmp_path, monkeypatch, mode, passes
):
    monkeypatch.chdir(tmp_path)
    # mit-no-file ships no licence file, so the repo supplies one.
    (tmp_path / "mit.txt").write_text("MIT text from the repo", encoding="utf-8")
    overrides = [Override("mit-no-file", "MIT", "upstream", None, "mit.txt")]
    findings, entries = _entries(policy)
    assert finish(findings, entries, policy, {}, mode, overrides) is passes
    notices = NOTICES_OUTPUT.read_text(encoding="utf-8")
    assert "MIT text from the repo" in notices
    deps = json.loads(REPORT_PATH.read_text(encoding="utf-8"))["deps"]
    assert [d["name"] for d in deps if d["verdict"] == "ask"] == [
        "pysigma-backend-clickhouse"
    ]


def test_finish_passes_when_everything_is_ok(policy, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    package = {"Name": "x", "Version": "1", "License": "MIT", "LicenseText": "t"}
    findings = findings_for([package], {"x": "1"}, policy, [])
    entries = notice_entries([package], findings)
    assert finish(findings, entries, policy, {}, "blocking") is True


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
        "[notice] (rule ok, source metadata)"
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
    assert "isarray 1.0.0 (MIT)" in text and "SPDX standard text for MIT" in text
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
                "vendor/lib/both.c": (Verdict.ASK, "file-header"),
                "vendor/lib/mit.c": (Verdict.OBLIGATIONS, "file-header"),
                "vendor/lib/no_header.c": (Verdict.ASK, "missing-header"),
            },
        ),
        (
            {"path": "vendor", "licence": "MIT", "reason": "upstream LICENSE is MIT"},
            {
                "vendor/lib/both.c": (Verdict.ASK, "file-header"),
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


@pytest.mark.parametrize(
    ("marker", "code", "licences"),
    [
        ("classification: product\n", 0, ["BUSL-1.1"]),
        ("classification: hyperi\n", 0, ["LicenseRef-HyperI-Proprietary"]),
        ("classification: oss\n", 0, ["Apache-2.0", "MIT"]),
        ("classification: fork\n", 0, ["inherit"]),
        # Undeclared reads as internal, the most restrictive.
        ("", 0, ["LicenseRef-HyperI-Proprietary"]),
        ("classification: nonsense\n", 2, None),
    ],
)
def test_licenses_outbound_command(tmp_path, marker, code, licences):
    from typer.testing import CliRunner

    from hyperi_ci.cli import app

    (tmp_path / ".hyperi-ci.yaml").write_text(marker, encoding="utf-8")
    result = CliRunner().invoke(
        app, ["licenses", "outbound", "--json", "-C", str(tmp_path)]
    )
    assert result.exit_code == code, result.output
    if licences is not None:
        assert json.loads(result.stdout)["licences"] == licences


def test_licenses_notices_needs_a_language_it_reads(tmp_path):
    from typer.testing import CliRunner

    from hyperi_ci.cli import app

    result = CliRunner().invoke(app, ["licenses", "notices", "-C", str(tmp_path)])
    assert result.exit_code == 2


@pytest.mark.parametrize(
    ("reader", "lock", "written"),
    [
        # No uv.lock leaves only copied-in files, and the notices still write.
        (licenses, None, True),
        # No Cargo.lock: cargo never ran, so there is nothing current to ship.
        (rust_licenses, None, False),
    ],
)
def test_fresh_notices_never_passes_off_an_old_file(
    tmp_path, monkeypatch, reader, lock, written
):
    from hyperi_ci.quality.dep_licenses import fresh_notices

    monkeypatch.chdir(tmp_path)
    NOTICES_OUTPUT.parent.mkdir()
    NOTICES_OUTPUT.write_text("left by an earlier run\n", encoding="utf-8")
    (tmp_path / ".hyperi-ci.yaml").write_text("", encoding="utf-8")
    got = fresh_notices(reader.run_gate, load_config(reload=True))
    assert (got is not None) is written
    if written:
        assert "earlier run" not in NOTICES_OUTPUT.read_text(encoding="utf-8")
