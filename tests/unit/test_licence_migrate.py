# Project:   HyperI CI
# File:      tests/unit/test_licence_migrate.py
# Purpose:   Bringing a repo to the licence its classification ships under
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Bringing a repo to the licence its classification ships under."""

import pytest

from hyperi_ci import licenses
from hyperi_ci.licence_migrate import (
    MigrateError,
    apply,
    plan,
    rewrite_header,
    rewrite_manifest,
)

OWN = licenses.own_licences()
OURS = "// Copyright (c) 2026 HYPERI PTY LIMITED\n"


@pytest.mark.parametrize(
    ("text", "expected", "changed"),
    [
        (
            "// SPDX-License-Identifier: FSL-1.1-ALv2\n" + OURS,
            "// SPDX-License-Identifier: BUSL-1.1\n" + OURS,
            [("FSL-1.1-ALv2", "BUSL-1.1")],
        ),
        (
            "# License:   Proprietary - HYPERI PTY LIMITED\n# Copyright: (c) 2026 HYPERI PTY LIMITED\n",
            "# License:   BUSL-1.1 - HYPERI PTY LIMITED\n# Copyright: (c) 2026 HYPERI PTY LIMITED\n",
            [("Proprietary", "BUSL-1.1")],
        ),
        (
            "<!--\nLicense:   FSL-1.1-ALv2 -- HYPERI PTY LIMITED\nCopyright: (c) 2026 HYPERI PTY LIMITED\n-->\n",
            "<!--\nLicense:   BUSL-1.1 -- HYPERI PTY LIMITED\nCopyright: (c) 2026 HYPERI PTY LIMITED\n-->\n",
            [("FSL-1.1-ALv2", "BUSL-1.1")],
        ),
        # Someone else's copyright: never ours to relicense.
        (
            "// SPDX-License-Identifier: FSL-1.1-ALv2\n// Copyright (c) Example Ltd\n",
            None,
            [],
        ),
        # Another project's licence on a file we copied in stays.
        ("# License: Apache-2.0\n" + OURS, None, []),
        ("// SPDX-License-Identifier: BUSL-1.1\n" + OURS, None, []),
        # Past the header block is code, not a header.
        (OURS + "\n" * 20 + "// SPDX-License-Identifier: FSL-1.1-ALv2\n", None, []),
        ("", None, []),
    ],
)
def test_rewrite_header(text, expected, changed):
    new, got = rewrite_header(text, "BUSL-1.1", OWN)
    assert (new, got) == (expected if expected is not None else text, changed)


_CARGO = """\
# Project:   x
[package]
name = "x"
license = "FSL-1.1-ALv2"  # set by hand

[dependencies]
serde = "1"
"""

_FIELDS = licenses.outbound("product")["manifest"]


@pytest.mark.parametrize(
    ("name", "text", "expected", "changed"),
    [
        (
            "Cargo.toml",
            _CARGO,
            _CARGO.replace('license = "FSL-1.1-ALv2"  # set by hand\n', "").replace(
                'name = "x"\n', 'name = "x"\nlicense-file = "LICENSE"\n'
            ),
            [("FSL-1.1-ALv2", "(removed)"), ("None", "LICENSE")],
        ),
        (
            "Cargo.toml",
            '[workspace.package]\nlicense-file = "LICENSE"\n',
            None,
            [],
        ),
        (
            "pyproject.toml",
            '[project]\nname = "x"\nlicense = "Proprietary"\n',
            '[project]\nname = "x"\nlicense = "LicenseRef-BUSL-1.1-HyperI"\n'
            'license-files = ["LICENSE", "COMMERCIAL.md", "AI-TRAINING-POLICY.md"]\n',
            [
                ("Proprietary", "LicenseRef-BUSL-1.1-HyperI"),
                ("None", "['LICENSE', 'COMMERCIAL.md', 'AI-TRAINING-POLICY.md']"),
            ],
        ),
        # A table the manifest does not have is never added.
        ("pyproject.toml", "[tool.ruff]\nline-length = 88\n", None, []),
        (
            "package.json",
            '{"name": "x", "license": "MIT"}',
            '{\n  "name": "x",\n  "license": "SEE LICENSE IN LICENSE"\n}\n',
            [("MIT", "SEE LICENSE IN LICENSE")],
        ),
    ],
)
def test_rewrite_manifest(name, text, expected, changed):
    new, got = rewrite_manifest(name, text, _FIELDS)
    assert got == changed
    assert new == (expected if expected is not None else text)


_BUNDLE = {
    "LICENSE": b"Business Source License 1.1\nCopyright (c) <YEAR> HYPERI PTY LIMITED\n",
    "COMMERCIAL.md": b"commercial terms\n",
}


def _repo(tmp_path, files):
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return list(files)


def test_plan_conforms_the_repo_and_lists_what_it_left(tmp_path):
    files = _repo(
        tmp_path,
        {
            "LICENSE": "Business Source License 1.1\nCopyright (c) 2024 HYPERI PTY LIMITED\n",
            "Cargo.toml": _CARGO,
            "src/main.rs": "// SPDX-License-Identifier: FSL-1.1-ALv2\n" + OURS,
            "vendor/lib.c": "// SPDX-License-Identifier: FSL-1.1-ALv2\n" + OURS,
            "README.md": "Licensed FSL-1.1-ALv2, see LICENSE.\nNot FSL-1.1-ALv2x.\n",
        },
    )
    migration = plan(
        tmp_path,
        files,
        licenses.outbound("product"),
        OWN,
        vendored=["vendor"],
        fetch=lambda source: _BUNDLE,
    )
    # A LICENSE matching the template with its own year is current.
    assert set(migration.writes) == {"COMMERCIAL.md", "Cargo.toml", "src/main.rs"}
    assert migration.leftovers == ["README.md:1"]
    apply(tmp_path, migration)
    assert (
        (tmp_path / "src/main.rs")
        .read_text()
        .startswith("// SPDX-License-Identifier: BUSL-1.1\n")
    )
    assert "FSL" in (tmp_path / "vendor/lib.c").read_text()
    again = plan(
        tmp_path,
        files,
        licenses.outbound("product"),
        OWN,
        vendored=["vendor"],
        fetch=lambda s: _BUNDLE,
    )
    assert again.changes == []


def test_a_missing_bundle_file_gets_this_year(tmp_path):
    migration = plan(
        tmp_path, [], licenses.outbound("product"), OWN, fetch=lambda source: _BUNDLE
    )
    licence = migration.writes["LICENSE"]
    assert b"<YEAR>" not in licence and b"Copyright (c) 20" in licence


def test_a_fork_has_nothing_to_migrate(tmp_path):
    with pytest.raises(MigrateError, match="upstream"):
        plan(tmp_path, [], licenses.outbound("fork"), OWN)


def test_migrate_command_is_a_dry_run_without_apply(tmp_path):
    from typer.testing import CliRunner

    from hyperi_ci.cli import app

    result = CliRunner().invoke(app, ["licenses", "migrate", "-C", str(tmp_path)])
    # Not a git checkout: nothing is read or written.
    assert result.exit_code == 2
    assert "not a git checkout" in result.output
