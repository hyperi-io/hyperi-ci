# Project:   HyperI CI
# File:      tests/unit/test_deps.py
# Purpose:   Tests for `hyperi-ci deps` -- surfaces, pins, drift, gaps
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Real repositories on disk, no mocks.

Every fixture writes actual manifests, locks, workflows and Dockerfiles into a
``tmp_path`` and runs the real code over them. The only thing stubbed anywhere
is nothing: the optional language-toolchain enrichment is genuinely optional,
so a box without cargo simply exercises the parse path, which is what the
design says must always be sufficient.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from hyperi_ci import deps, pin_marker
from hyperi_ci.deps import ecosystems, render, renovate, surfaces, versions


def _git_init(root: Path) -> None:
    """Make ``root`` a real git repo with everything tracked.

    ``repo_files`` prefers ``git ls-files``; a fixture that skipped this would
    silently exercise the walk fallback instead of the path production uses.
    """
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8", newline="\n")
    return path


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


class TestCatalogue:
    """The shipped catalogue must load and stay internally consistent."""

    def test_ships_inside_the_package(self) -> None:
        # config/ at the repo ROOT is excluded from the wheel; this one lives
        # under src/hyperi_ci/config/, so a consumer repo can read it.
        catalogue = Path(surfaces.__file__).parent.parent / "config"
        assert (catalogue / "dep-surfaces.yaml").is_file()

    def test_every_surface_has_the_required_fields(self) -> None:
        for surface in surfaces.load():
            assert surface.id
            assert surface.kind in {
                "python",
                "rust",
                "node",
                "go",
                "container",
                "ci",
                "toolchain",
                "hooks",
                "infra",
            }
            # A null-manager surface must say WHY Renovate cannot see it.
            if surface.renovate_manager is None:
                assert surface.gap, f"{surface.id} has no gap note"
            # An empty-pattern surface must say why it can look covered.
            if not surface.patterns:
                assert surface.caveat, f"{surface.id} has no caveat"

    def test_pin_marker_surface_matches_the_shared_definition(self) -> None:
        # The catalogue declares the marker regex as data; pin_marker.py is the
        # reference definition update-versions.py also uses. If they diverge,
        # one of the two stops seeing marked pins.
        surface = next(s for s in surfaces.load() if s.id == "pin-marker")
        declared = surface.pins_multiline[0].pattern
        assert declared == pin_marker.discovery_pattern().pattern


# ---------------------------------------------------------------------------
# Version + constraint parsing
# ---------------------------------------------------------------------------


class TestVersions:
    @pytest.mark.parametrize(
        "constraint,expected",
        [
            (">=8.0.0", "8.0.0"),
            (">=8.0.0,<10", "8.0.0"),
            ("~=1.4", "1.4"),
            ("^1.2.3", "1.2.3"),
            ("~0.5", "0.5"),
            ("==6.0.4", "6.0.4"),
            ("=1.0", "1.0"),
            (">2", "2"),
            ("1.2.3", "1.2.3"),
            ("v18.2.0", "18.2.0"),
        ],
    )
    def test_floor_extracted(self, constraint: str, expected: str) -> None:
        assert versions.floor_of(constraint) == expected

    @pytest.mark.parametrize(
        "constraint",
        ["*", "", "latest", "<2.0", "<=2.0", "!=1.0", "workspace:*", "file:../x"],
    )
    def test_no_floor_is_none(self, constraint: str) -> None:
        assert versions.floor_of(constraint) is None

    def test_marker_is_stripped_before_the_floor(self) -> None:
        assert versions.floor_of('>=0.28.0; python_version < "3.12"') == "0.28.0"

    def test_prerelease_suffix_ignored(self) -> None:
        assert versions.parse("1.2.3rc1") == (1, 2, 3)
        assert versions.parse("2.0.0-alpha.1") == (2, 0, 0)

    @pytest.mark.parametrize(
        "floor,locked,expected",
        [
            ("8.0.0", "9.0.3", "major"),
            ("1.0.0", "2.1.0", "major"),
            ("0.23.0", "1.3.0", "major"),  # 0.x -> 1.x is still a major
            ("0.23.0", "0.40.1", "minor"),  # 0.x treats minor as breaking
            ("8.0.0", "8.4.1", None),
            ("0.23.0", "0.23.9", None),
            ("2.0.0", "1.9.0", None),
        ],
    )
    def test_drift_kind(self, floor: str, locked: str, expected: str | None) -> None:
        assert versions.drift_kind(floor, locked) == expected

    def test_extras_stripped_from_the_name(self) -> None:
        assert versions.split_requirement("moto[secretsmanager]>=5.2.0") == (
            "moto",
            ">=5.2.0",
        )


# ---------------------------------------------------------------------------
# Drift -- the original contribution
# ---------------------------------------------------------------------------


_UV_LOCK = """\
version = 1

[[package]]
name = "pytest"
version = "9.0.3"

[[package]]
name = "pytest-asyncio"
version = "1.3.0"

[[package]]
name = "mypy"
version = "2.1.0"

[[package]]
name = "pyyaml"
version = "6.0.4"
"""


def _python_repo(root: Path, dev_floor: str = "8.0.0") -> Path:
    _write(
        root,
        "pyproject.toml",
        f"""\
[project]
name = "demo"
version = "0.1.0"
dependencies = ["pyyaml>=6.0.0"]

[project.optional-dependencies]
dev = [
    "pytest>={dev_floor}",
    "pytest-asyncio>=0.23.0",
    "mypy>=1.0.0",
]
""",
    )
    _write(root, "uv.lock", _UV_LOCK)
    _git_init(root)
    return root


class TestPythonDrift:
    def test_stale_dev_floor_flagged_as_dev_not_runtime(self, tmp_path: Path) -> None:
        _python_repo(tmp_path)
        result = ecosystems.drift(tmp_path)

        by_dep = {item["dep"]: item for item in result["drift"]}
        assert set(by_dep) == {"pytest", "pytest-asyncio", "mypy"}
        for item in by_dep.values():
            # The group is the whole point: a dev/test group rotting is
            # invisible if everything is merged into one number.
            assert item["group"] == "project.optional-dependencies.dev"
            assert item["ecosystem"] == "python"
        assert by_dep["pytest"]["floor"] == "8.0.0"
        assert by_dep["pytest"]["locked"] == "9.0.3"
        assert by_dep["pytest"]["drift"] == "major"
        # pyyaml is runtime and current -- it must NOT be flagged.
        assert "pyyaml" not in by_dep

    def test_current_floor_is_not_flagged(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "pyproject.toml",
            '[project]\nname = "demo"\nversion = "0.1.0"\n'
            'dependencies = ["pyyaml>=6.0.0", "pytest>=9.0.0"]\n',
        )
        _write(tmp_path, "uv.lock", _UV_LOCK)
        _git_init(tmp_path)

        result = ecosystems.drift(tmp_path)
        assert result["drift"] == []
        assert result["compared"] == 2

    def test_zero_x_floor_with_locked_minor_ahead_is_flagged(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "pyproject.toml",
            '[project]\nname = "demo"\nversion = "0.1.0"\n'
            'dependencies = ["ty>=0.0.34"]\n',
        )
        _write(
            tmp_path,
            "uv.lock",
            'version = 1\n\n[[package]]\nname = "ty"\nversion = "0.4.1"\n',
        )
        _git_init(tmp_path)

        result = ecosystems.drift(tmp_path)
        assert [(i["dep"], i["drift"]) for i in result["drift"]] == [("ty", "minor")]

    def test_pep735_dependency_group_is_walked(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "pyproject.toml",
            '[project]\nname = "demo"\nversion = "0.1.0"\ndependencies = []\n\n'
            '[dependency-groups]\ntest = ["pytest>=8.0.0"]\n',
        )
        _write(tmp_path, "uv.lock", _UV_LOCK)
        _git_init(tmp_path)

        result = ecosystems.drift(tmp_path)
        assert [i["group"] for i in result["drift"]] == ["dependency-groups.test"]

    def test_declared_but_unlocked_is_reported_not_dropped(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "pyproject.toml",
            '[project]\nname = "demo"\nversion = "0.1.0"\n'
            'dependencies = ["never-locked>=1.0.0"]\n',
        )
        _write(tmp_path, "uv.lock", _UV_LOCK)
        _git_init(tmp_path)

        result = ecosystems.drift(tmp_path)
        rows = result["ecosystems"][0]["groups"][0]["entries"]
        assert rows[0]["dep"] == "never-locked"
        assert rows[0]["locked"] == ""
        assert result["compared"] == 0
        assert result["declared"] == 1


class TestCargoDrift:
    def test_dev_dependencies_drift(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "Cargo.toml",
            """\
[package]
name = "demo"
version = "0.1.0"

[dependencies]
serde = "1.0"

[dev-dependencies]
criterion = "0.4"
proptest = { version = "1.2" }
""",
        )
        _write(
            tmp_path,
            "Cargo.lock",
            """\
version = 3

[[package]]
name = "serde"
version = "1.0.219"

[[package]]
name = "criterion"
version = "0.7.0"

[[package]]
name = "proptest"
version = "1.9.0"
""",
        )
        _git_init(tmp_path)

        result = ecosystems.drift(tmp_path)
        by_dep = {item["dep"]: item for item in result["drift"]}
        assert set(by_dep) == {"criterion"}
        assert by_dep["criterion"]["group"] == "dev-dependencies"
        assert by_dep["criterion"]["ecosystem"] == "rust"
        assert by_dep["criterion"]["drift"] == "minor"  # 0.4 -> 0.7 on the 0.x axis


class TestMultiLanguage:
    def test_python_and_rust_and_node_in_one_pass(self, tmp_path: Path) -> None:
        # The assumption we cannot make is "this is a Python repo". A polyglot
        # tree must report every ecosystem, not the first marker that hits.
        _python_repo(tmp_path)
        _write(
            tmp_path,
            "Cargo.toml",
            '[package]\nname = "demo"\nversion = "0.1.0"\n\n'
            '[dev-dependencies]\ncriterion = "0.4"\n',
        )
        _write(
            tmp_path,
            "Cargo.lock",
            'version = 3\n\n[[package]]\nname = "criterion"\nversion = "0.7.0"\n',
        )
        _write(
            tmp_path,
            "package.json",
            json.dumps({"name": "demo", "devDependencies": {"typescript": "^4.9.0"}}),
        )
        _write(
            tmp_path,
            "package-lock.json",
            json.dumps(
                {
                    "lockfileVersion": 3,
                    "packages": {
                        "": {"name": "demo"},
                        "node_modules/typescript": {"version": "5.9.2"},
                    },
                }
            ),
        )
        _write(tmp_path, "go.mod", "module demo\n\ngo 1.24\n")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)

        result = ecosystems.drift(tmp_path)
        assert {eco["name"] for eco in result["ecosystems"]} == {
            "python",
            "rust",
            "node",
        }
        assert {item["ecosystem"] for item in result["drift"]} == {
            "python",
            "rust",
            "node",
        }
        # Go is skipped LOUDLY, never silently omitted.
        assert any("go.mod" in note for note in result["notes"])

    def test_workspace_lock_above_the_member_is_found(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "Cargo.lock",
            'version = 3\n\n[[package]]\nname = "criterion"\nversion = "0.7.0"\n',
        )
        _write(
            tmp_path,
            "crates/member/Cargo.toml",
            '[package]\nname = "member"\nversion = "0.1.0"\n\n'
            '[dev-dependencies]\ncriterion = "0.4"\n',
        )
        _git_init(tmp_path)

        result = ecosystems.drift(tmp_path)
        assert result["ecosystems"][0]["lock"] == "Cargo.lock"
        assert [i["dep"] for i in result["drift"]] == ["criterion"]


# ---------------------------------------------------------------------------
# scan -- surfaces, pins, three states
# ---------------------------------------------------------------------------


class TestScanActions:
    def test_workflow_and_composite_action_pins_extracted(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            ".github/workflows/x.yml",
            """\
name: x
jobs:
  build:
    steps:
      - uses: actions/checkout@v5
      - uses: astral-sh/setup-uv@v7
""",
        )
        # The commonly missed one: a composite action pins actions too.
        _write(
            tmp_path,
            ".github/actions/foo/action.yml",
            "name: foo\nruns:\n  using: composite\n  steps:\n"
            "    - uses: actions/setup-node@v6\n",
        )
        _git_init(tmp_path)

        result = surfaces.scan(tmp_path)
        record = next(r for r in result["surfaces"] if r["id"] == "github-actions")
        assert record["state"] == surfaces.FOUND
        assert set(record["files"]) == {
            ".github/actions/foo/action.yml",
            ".github/workflows/x.yml",
        }
        assert {(p["dep"], p["version"]) for p in record["pins"]} == {
            ("actions/checkout", "v5"),
            ("astral-sh/setup-uv", "v7"),
            ("actions/setup-node", "v6"),
        }

    def test_files_for_matches_the_update_versions_discovery(
        self, tmp_path: Path
    ) -> None:
        # update-versions.py globs two hardcoded dirs for THIS repo; the
        # github-actions surface is the generalisation. Pin them together so
        # they cannot quietly disagree about what a pipeline file is.
        _write(tmp_path, ".github/workflows/a.yml", "name: a\n")
        _write(tmp_path, ".github/workflows/b.yaml", "name: b\n")
        _write(tmp_path, ".github/actions/c/action.yml", "name: c\n")
        _write(tmp_path, ".github/actions/d/action.yaml", "name: d\n")
        _git_init(tmp_path)

        assert set(surfaces.files_for(tmp_path, "github-actions")) == {
            ".github/actions/c/action.yml",
            ".github/actions/d/action.yaml",
            ".github/workflows/a.yml",
            ".github/workflows/b.yaml",
        }


class TestScanDockerfile:
    def test_every_stage_of_a_multi_stage_build(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "Dockerfile",
            """\
FROM --platform=$BUILDPLATFORM rust:1.90-bookworm AS builder
RUN cargo build --release

FROM node:24-alpine AS assets
RUN npm ci

FROM debian:trixie-slim
COPY --from=builder /app /app
""",
        )
        _git_init(tmp_path)

        result = surfaces.scan(tmp_path)
        record = next(r for r in result["surfaces"] if r["id"] == "dockerfile")
        assert record["state"] == surfaces.FOUND
        assert [(p["dep"], p["version"]) for p in record["pins"]] == [
            ("rust", "1.90-bookworm"),
            ("node", "24-alpine"),
            ("debian", "trixie-slim"),
        ]

    def test_a_source_file_named_dockerfile_py_is_not_a_dockerfile(
        self, tmp_path: Path
    ) -> None:
        # Renovate's own second pattern (`...file[^/]*$`) swallows this. It can
        # afford to: its manager parses and finds nothing. We report a STATE,
        # so the false match would surface as a false `inert` in a repo with no
        # Dockerfile at all.
        _write(tmp_path, "src/pkg/dockerfile.py", "ANCHOR = 'FROM'\n")
        _git_init(tmp_path)

        result = surfaces.scan(tmp_path)
        record = next(r for r in result["surfaces"] if r["id"] == "dockerfile")
        assert record["state"] == surfaces.ABSENT

    def test_a_real_dockerfile_suffix_still_matches(self, tmp_path: Path) -> None:
        _write(tmp_path, "Dockerfile.prod", "FROM debian:trixie-slim\n")
        _git_init(tmp_path)

        result = surfaces.scan(tmp_path)
        record = next(r for r in result["surfaces"] if r["id"] == "dockerfile")
        assert record["state"] == surfaces.FOUND
        assert record["pins"][0]["dep"] == "debian"


class TestScanStates:
    def test_inert_when_files_match_but_nothing_extracts(self, tmp_path: Path) -> None:
        # A test file with no container marker anywhere near it. The surface
        # must read `inert` -- files were read and nothing came out -- NOT
        # `absent` (which would be a lie) and NOT `found` (which reads clean).
        _write(
            tmp_path,
            "tests/test_thing.py",
            "def test_thing():\n    assert 1 + 1 == 2\n",
        )
        _git_init(tmp_path)

        result = surfaces.scan(tmp_path)
        record = next(r for r in result["surfaces"] if r["id"] == "testcontainers")
        assert record["state"] == surfaces.INERT
        assert record["files"] == ["tests/test_thing.py"]
        assert record["pins"] == []

    def test_found_when_a_guarded_container_tag_is_present(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "tests/test_thing.py",
            "from testcontainers.core.container import DockerContainer\n\n"
            "def test_thing():\n"
            '    with DockerContainer("clickhouse/clickhouse-server:25.3.1") as c:\n'
            "        assert c\n",
        )
        _git_init(tmp_path)

        result = surfaces.scan(tmp_path)
        record = next(r for r in result["surfaces"] if r["id"] == "testcontainers")
        assert record["state"] == surfaces.FOUND
        assert ("clickhouse/clickhouse-server", "25.3.1") in {
            (p["dep"], p["version"]) for p in record["pins"]
        }

    def test_empty_upstream_patterns_are_inert_never_absent(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, "README.md", "# demo\n")
        _git_init(tmp_path)

        result = surfaces.scan(tmp_path)
        for surface_id in ("kubernetes", "pip-compile"):
            record = next(r for r in result["surfaces"] if r["id"] == surface_id)
            assert record["state"] == surfaces.INERT, surface_id

    def test_absent_when_nothing_matches(self, tmp_path: Path) -> None:
        _write(tmp_path, "README.md", "# demo\n")
        _git_init(tmp_path)

        result = surfaces.scan(tmp_path)
        record = next(r for r in result["surfaces"] if r["id"] == "cargo")
        assert record["state"] == surfaces.ABSENT
        assert record["files"] == []

    def test_one_file_may_belong_to_several_surfaces(self, tmp_path: Path) -> None:
        _python_repo(tmp_path)
        result = surfaces.scan(tmp_path)
        claiming = {
            r["id"] for r in result["surfaces"] if "pyproject.toml" in r["files"]
        }
        assert {"pep621", "poetry"} <= claiming


class TestPinMarkerSurface:
    def test_marked_pin_in_arbitrary_source_is_discovered(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "src/thing/tools.py",
            '# hyperi-ci:pin tools.gitleaks\n_GITLEAKS_VERSION = "v8.30.1"\n',
        )
        _git_init(tmp_path)

        result = surfaces.scan(tmp_path)
        record = next(r for r in result["surfaces"] if r["id"] == "pin-marker")
        assert record["state"] == surfaces.FOUND
        assert [(p["dep"], p["version"]) for p in record["pins"]] == [
            ("tools.gitleaks", "v8.30.1")
        ]

    def test_unmarked_source_leaves_the_surface_inert(self, tmp_path: Path) -> None:
        _write(tmp_path, "src/thing/tools.py", '_VERSION = "v8.30.1"\n')
        _git_init(tmp_path)

        result = surfaces.scan(tmp_path)
        record = next(r for r in result["surfaces"] if r["id"] == "pin-marker")
        assert record["state"] == surfaces.INERT


class TestUnclassified:
    def test_version_bearing_file_no_surface_claims(self, tmp_path: Path) -> None:
        _write(tmp_path, ".python-version", "3.12\n")
        _write(tmp_path, "ci/constraints.yaml", "a: 1\n")
        _write(tmp_path, "README.md", "# demo\n")
        _git_init(tmp_path)

        result = surfaces.scan(tmp_path)
        assert set(result["unclassified"]["shown"]) == {
            ".python-version",
            "ci/constraints.yaml",
        }
        assert result["unclassified"]["capped"] is False

    def test_lockfile_owned_by_a_surface_is_not_unclassified(
        self, tmp_path: Path
    ) -> None:
        _python_repo(tmp_path)
        result = surfaces.scan(tmp_path)
        assert "uv.lock" not in result["unclassified"]["shown"]

    def test_cap_is_reported_honestly(self, tmp_path: Path) -> None:
        total = surfaces.UNCLASSIFIED_CAP + 7
        for index in range(total):
            _write(tmp_path, f"ci/thing-{index:03d}-version.yaml", "a: 1\n")
        _git_init(tmp_path)

        result = surfaces.scan(tmp_path)
        unclassified = result["unclassified"]
        assert unclassified["total"] == total
        assert len(unclassified["shown"]) == surfaces.UNCLASSIFIED_CAP
        assert unclassified["capped"] is True

        # And the human view must SAY so rather than quietly truncating.
        from hyperi_ci.deps import render

        text = "\n".join(render.unclassified_block(result))
        assert str(total) in text
        assert "capped" in text


class TestEnrichment:
    """The optional toolchain layer must never be load-bearing."""

    def test_absent_tool_is_silent_and_returns_nothing(self, tmp_path: Path) -> None:
        # A real probe against a binary that genuinely is not on PATH. A box
        # without cargo is normal, not a finding: no raise, no warning, no
        # partial result.
        assert (
            ecosystems._run_tool(["definitely-not-a-real-binary-xyz"], tmp_path) is None
        )

    def test_parse_wins_and_the_tool_only_adds(self) -> None:
        merged = ecosystems._locked_map(
            parsed={"pytest": "9.0.3"},
            enriched={"pytest": "1.0.0", "extra-from-workspace": "2.5.0"},
            tool="uv",
            norm=versions.norm_python,
        )
        # The parse is authoritative -- enrichment must not overwrite it.
        assert merged["pytest"] == ("9.0.3", "parse")
        # ... but it may fill a gap the parse could not reach.
        assert merged["extra-from-workspace"] == ("2.5.0", "uv")

    def test_drift_is_complete_without_any_toolchain(self, tmp_path: Path) -> None:
        # The core path must be sufficient on its own. Nothing about this repo
        # needs uv, cargo or npm to be installed.
        _python_repo(tmp_path)
        result = ecosystems.drift(tmp_path)
        assert {item["source"] for item in result["drift"]} == {"parse"}
        assert len(result["drift"]) == 3


# ---------------------------------------------------------------------------
# Renovate gaps
# ---------------------------------------------------------------------------


class TestRenovateGaps:
    def test_null_manager_surface_named_when_a_config_exists(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, "tox.ini", "[testenv]\ndeps =\n    pytest>=8.0.0\n")
        _write(
            tmp_path,
            "renovate.json",
            json.dumps({"enabledManagers": ["github-actions", "pep621"]}),
        )
        _git_init(tmp_path)

        result = renovate.gaps(tmp_path, surfaces.scan(tmp_path))
        by_id = {item["id"]: item for item in result["uncovered"]}
        assert result["config"] == "renovate.json"
        assert "tox" in by_id
        assert by_id["tox"]["reason"] == "no renovate manager exists for this surface"
        assert "renovatebot/renovate#2214" in by_id["tox"]["detail"]

    def test_manager_outside_enabled_managers_is_uncovered(
        self, tmp_path: Path
    ) -> None:
        _python_repo(tmp_path)
        _write(
            tmp_path,
            "renovate.json",
            json.dumps({"enabledManagers": ["github-actions"]}),
        )
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)

        result = renovate.gaps(tmp_path, surfaces.scan(tmp_path))
        by_id = {item["id"]: item for item in result["uncovered"]}
        assert "not in enabledManagers" in by_id["pep621"]["reason"]

    def test_inert_manager_is_uncovered_even_when_enabled(self, tmp_path: Path) -> None:
        _write(tmp_path, "renovate.json", json.dumps({}))
        _git_init(tmp_path)

        result = renovate.gaps(tmp_path, surfaces.scan(tmp_path))
        by_id = {item["id"]: item for item in result["uncovered"]}
        assert by_id["kubernetes"]["state"] == surfaces.INERT
        assert "inert" in by_id["kubernetes"]["reason"]

    def test_no_config_means_everything_present_is_uncovered(
        self, tmp_path: Path
    ) -> None:
        _python_repo(tmp_path)
        result = renovate.gaps(tmp_path, surfaces.scan(tmp_path))
        assert result["config"] is None
        assert "pep621" in {item["id"] for item in result["uncovered"]}


# ---------------------------------------------------------------------------
# report + show -- the one-call surface
# ---------------------------------------------------------------------------


class TestReportAndShow:
    def test_report_carries_all_three_slices(self, tmp_path: Path) -> None:
        _python_repo(tmp_path)
        payload = deps.report(tmp_path)
        assert set(payload) == {"root", "kind_filter", "scan", "drift", "gaps"}
        assert payload["drift"]["drift"], "the known-stale dev group must show"
        assert payload["scan"]["surfaces"]
        assert payload["gaps"]["uncovered"]

    def test_kind_filter_narrows_surfaces_and_ecosystems(self, tmp_path: Path) -> None:
        _python_repo(tmp_path)
        _write(
            tmp_path,
            "Cargo.toml",
            '[package]\nname = "demo"\nversion = "0.1.0"\n\n'
            '[dependencies]\nserde = "1.0"\n',
        )
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)

        payload = deps.report(tmp_path, kind="rust")
        assert {r["kind"] for r in payload["scan"]["surfaces"]} == {"rust"}
        assert {e["name"] for e in payload["drift"]["ecosystems"]} == {"rust"}

    def test_show_dumps_one_surface_with_line_numbers(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            ".github/workflows/x.yml",
            "name: x\njobs:\n  b:\n    steps:\n      - uses: actions/checkout@v5\n",
        )
        _git_init(tmp_path)

        detail = deps.show(tmp_path, "github-actions")
        assert detail["surface"]["state"] == surfaces.FOUND
        assert detail["surface"]["pins"][0]["line"] == 5
        assert detail["registry"]["renovate_manager"] == "github-actions"

    def test_show_of_a_manifest_surface_carries_the_group_table(
        self, tmp_path: Path
    ) -> None:
        _python_repo(tmp_path)
        detail = deps.show(tmp_path, "pep621")
        groups = {g["group"] for e in detail["ecosystems"] for g in e["groups"]}
        assert "project.optional-dependencies.dev" in groups

    def test_unknown_surface_lists_the_known_ids(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        detail = deps.show(tmp_path, "nope")
        assert "error" in detail
        assert "pep621" in detail["known"]


# ---------------------------------------------------------------------------
# Rendering + CLI
# ---------------------------------------------------------------------------


class TestRendering:
    def test_action_section_leads_the_report(self, tmp_path: Path) -> None:
        from hyperi_ci.deps import render

        _python_repo(tmp_path)
        text = render.report(deps.report(tmp_path))
        assert text.index("== ACTION") < text.index("== INVENTORY")
        assert text.index("FLOOR DRIFT") < text.index("== INVENTORY")

    def test_inert_is_explained_not_just_labelled(self, tmp_path: Path) -> None:
        from hyperi_ci.deps import render

        _write(tmp_path, "tests/test_thing.py", "def test_thing():\n    pass\n")
        _git_init(tmp_path)
        text = "\n".join(render.inert_block(surfaces.scan(tmp_path)))
        assert "nothing was extractable" in text
        assert "NOT clean" in text

    def test_output_is_ascii_only(self, tmp_path: Path) -> None:
        from hyperi_ci.deps import render

        _python_repo(tmp_path)
        text = render.report(deps.report(tmp_path), full=True)
        text.encode("ascii")  # raises if a smart quote or dash slipped in


class TestCli:
    def test_drift_exits_one_when_drift_found(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from hyperi_ci.cli import app

        _python_repo(tmp_path)
        result = CliRunner().invoke(app, ["deps", "drift", "--root", str(tmp_path)])
        assert result.exit_code == 1
        assert "pytest" in result.stdout

    def test_drift_exits_zero_when_clean(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from hyperi_ci.cli import app

        _write(
            tmp_path,
            "pyproject.toml",
            '[project]\nname = "demo"\nversion = "0.1.0"\n'
            'dependencies = ["pyyaml>=6.0.0"]\n',
        )
        _write(tmp_path, "uv.lock", _UV_LOCK)
        _git_init(tmp_path)

        result = CliRunner().invoke(app, ["deps", "drift", "--root", str(tmp_path)])
        assert result.exit_code == 0

    def test_scan_and_gaps_always_exit_zero(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from hyperi_ci.cli import app

        _python_repo(tmp_path)
        runner = CliRunner()
        for action in ("scan", "gaps"):
            result = runner.invoke(app, ["deps", action, "--root", str(tmp_path)])
            assert result.exit_code == 0, action

    def test_bare_deps_is_the_full_report(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from hyperi_ci.cli import app

        _python_repo(tmp_path)
        result = CliRunner().invoke(app, ["deps", "--root", str(tmp_path)])
        assert result.exit_code == 0
        for section in ("== ACTION", "== INVENTORY", "== PINS", "== GROUPS"):
            assert section in result.stdout, section

    def test_json_is_parseable(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from hyperi_ci.cli import app

        _python_repo(tmp_path)
        result = CliRunner().invoke(app, ["deps", "--root", str(tmp_path), "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["drift"]["drift"][0]["source"] in {"parse", "uv"}


# ---------------------------------------------------------------------------
# `deps show` rendering
# ---------------------------------------------------------------------------


class TestShowRendering:
    """The rendered output, not just the detail dict.

    `show` is the pre-canned follow-up -- the thing that exists so nobody
    reaches for an ad-hoc rg. If its rendering drops the pins or the file list
    it has failed at the one job it has, and the dict-level tests cannot see
    that.
    """

    def test_renders_header_patterns_files_and_pins(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            ".github/workflows/ci.yml",
            "jobs:\n  a:\n    steps:\n      - uses: actions/checkout@v4\n",
        )
        _git_init(tmp_path)
        out = render.show(deps.show(tmp_path, "github-actions"))

        assert "deps show: github-actions" in out
        assert str(tmp_path) in out
        assert "state: found" in out
        assert "renovate manager: github-actions" in out
        assert ".github/workflows/ci.yml" in out
        assert "actions/checkout" in out
        assert "v4" in out

    def test_unknown_id_renders_the_error_and_the_known_list(
        self, tmp_path: Path
    ) -> None:
        _git_init(tmp_path)
        out = render.show(deps.show(tmp_path, "no-such-surface"))

        assert "no-such-surface" in out
        assert "known:" in out
        assert "github-actions" in out

    def test_pins_are_not_capped(self, tmp_path: Path) -> None:
        """`show` is the uncapped view -- a truncated one would be a silent lie."""
        steps = "".join(f"      - uses: acme/action-{i}@v{i}\n" for i in range(60))
        _write(
            tmp_path, ".github/workflows/big.yml", f"jobs:\n  a:\n    steps:\n{steps}"
        )
        _git_init(tmp_path)
        out = render.show(deps.show(tmp_path, "github-actions"))

        for i in (0, 30, 59):
            assert f"acme/action-{i}" in out, i

    def test_manifest_surface_renders_the_declared_vs_locked_table(
        self, tmp_path: Path
    ) -> None:
        _python_repo(tmp_path)
        out = render.show(deps.show(tmp_path, "pep621"))

        assert "pytest" in out
        assert ">=8.0.0" in out
        assert "9.0.3" in out

    def test_absent_surface_still_renders_rather_than_erroring(
        self, tmp_path: Path
    ) -> None:
        _python_repo(tmp_path)
        out = render.show(deps.show(tmp_path, "dockerfile"))

        assert "deps show: dockerfile" in out
        assert "state: absent" in out


# ---------------------------------------------------------------------------
# Optional toolchain enrichment -- the lockfile-absent path
# ---------------------------------------------------------------------------


def _lockless_cargo_workspace(root: Path) -> Path:
    """A cargo workspace with a path dependency and NO Cargo.lock."""
    _write(
        root,
        "Cargo.toml",
        """\
[workspace]
members = ["member"]

[package]
name = "demo"
version = "0.1.0"
edition = "2021"

[dependencies]
member = { path = "member", version = "0.4.2" }
""",
    )
    _write(
        root,
        "member/Cargo.toml",
        """\
[package]
name = "member"
version = "0.4.2"
edition = "2021"
""",
    )
    _write(root, "src/lib.rs", "")
    _write(root, "member/src/lib.rs", "")
    _git_init(root)
    assert not (root / "Cargo.lock").exists()
    return root


class TestEnrichmentWithoutLock:
    """The path that only runs when a lockfile cannot answer.

    `TestEnrichment` above covers the degradation rules. Everything there has a
    lock on disk, so the parse always wins and `source` stays "parse" -- which
    means the code that reaches for a toolchain never actually fires. These
    remove the lock so it does.
    """

    def test_failing_tool_degrades_silently_to_an_empty_map(
        self, tmp_path: Path
    ) -> None:
        """A directory that is not a cargo or npm project.

        The binary exists, the command fails. Same contract as an absent
        binary: no raise, no warning, no partial result.
        """
        assert ecosystems.enrich_cargo(tmp_path) == {}
        assert ecosystems.enrich_npm(tmp_path) == {}

    @pytest.mark.skipif(
        shutil.which("cargo") is None,
        reason="enrichment is optional by design; without cargo there is "
        "nothing to enrich from",
    )
    def test_cargo_resolves_a_version_with_no_lock_on_disk(
        self, tmp_path: Path
    ) -> None:
        """The path dependency keeps this offline.

        No registry access, so the result does not depend on whatever happens
        to be in the host's cargo cache.
        """
        _lockless_cargo_workspace(tmp_path)
        assert ecosystems.enrich_cargo(tmp_path).get("member") == "0.4.2"

    @pytest.mark.skipif(
        shutil.which("cargo") is None,
        reason="enrichment is optional by design; without cargo there is "
        "nothing to enrich from",
    )
    def test_drift_attributes_the_version_to_cargo_when_the_lock_is_absent(
        self, tmp_path: Path
    ) -> None:
        """Needs a tree cargo has never run in.

        `cargo metadata` WRITES Cargo.lock as a side effect, so anything that
        shells out first leaves a lock behind and the parse path wins on the
        next call. That is why this gets its own tmp_path rather than sharing
        one with the test above.
        """
        _lockless_cargo_workspace(tmp_path)

        rows = [
            row
            for eco in ecosystems.drift(tmp_path)["ecosystems"]
            for group in eco["groups"]
            for row in group["entries"]
            if row["dep"] == "member"
        ]
        assert rows, "the member dependency was not compared at all"
        assert rows[0]["source"] == "cargo"
        assert rows[0]["locked"] == "0.4.2"
