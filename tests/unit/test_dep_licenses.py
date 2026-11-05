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
from hyperi_ci.languages.rust import licenses as rust_licenses
from hyperi_ci.languages.typescript import licenses as npm_licenses
from hyperi_ci.quality import vendored_licenses
from hyperi_ci.quality.dep_licenses import (
    Mode,
    Override,
    PolicyError,
    Verdict,
    blocking,
    check_notices,
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
            Verdict.ASK,
            (),
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
    entries = notice_entries(_NOTICE_PACKAGES, findings)
    check_notices(entries, "warn", overrides)
    current = (tmp_path / ".hyperi-ci" / "THIRD-PARTY-NOTICES").read_text(
        encoding="utf-8"
    )
    assert "MIT text from the repo" in current
    if repo_copy is not None:
        text = current if repo_copy == "current" else "old\n"
        (tmp_path / "THIRD-PARTY-NOTICES").write_text(text, encoding="utf-8")
    assert check_notices(entries, mode, overrides) is passes


@pytest.mark.parametrize(("mode", "passes"), [("blocking", False), ("warn", True)])
def test_missing_licence_text_fails_instead_of_shipping_a_placeholder(
    policy, tmp_path, monkeypatch, mode, passes
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".hyperi-ci").mkdir()
    findings = findings_for(_NOTICE_PACKAGES, _NOTICE_RUNTIME, policy, [])
    entries = notice_entries(_NOTICE_PACKAGES, findings)
    assert check_notices(entries, mode) is passes


_NORMAL = [{"kind": None, "target": None}]


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

    def edge(pkg, kinds=_NORMAL):
        return {"pkg": f"{pkg}-id", "dep_kinds": kinds}

    nodes = [
        {
            "id": "app-id",
            "deps": [
                edge("serde"),
                edge("ring"),
                edge("derive"),
                edge("aws-lc"),
                edge("proptest", [{"kind": "dev", "target": None}]),
                edge("cc", [{"kind": "build", "target": None}]),
            ],
        },
        {"id": "serde-id", "deps": [edge("tokio")]},
        {"id": "derive-id", "deps": [edge("syn")]},
    ]
    return {
        "packages": packages,
        "workspace_members": ["app-id"],
        "resolve": {"nodes": nodes},
    }


def test_rust_ships_normal_dependencies_and_nothing_compile_time(rust_metadata):
    shipped = rust_licenses.shipped_packages(rust_metadata)
    # proptest is dev, cc is build, derive is a proc-macro and syn only feeds it.
    assert [p["name"] for p in shipped] == ["aws-lc", "ring", "serde", "tokio"]


def test_rust_crates_are_judged_as_static_links(policy, rust_metadata, monkeypatch):
    monkeypatch.chdir(rust_metadata["packages"][0]["manifest_path"].rsplit("/", 2)[0])
    shipped = rust_licenses.shipped_packages(rust_metadata)
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


def test_rust_notices_carry_crate_licence_and_notice_files(policy, rust_metadata):
    shipped = rust_licenses.shipped_packages(rust_metadata)
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
