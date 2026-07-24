# Project:   HyperI CI
# File:      tests/unit/deployment/test_detect_tier.py
# Purpose:   Tier auto-detection tests
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for `hyperi_ci.deployment.detect.detect_tier`."""

from __future__ import annotations

from pathlib import Path

import pytest

from hyperi_ci.deployment.detect import Tier, detect_tier, resolve_tier

# Every Tier 1/2 fixture needs a POSITIVE producer signal alongside the
# marker dep — a binary target for Rust, a console script for Python.
# Without one the repo is a library consumer, not a producer (issue #76,
# covered in TestProducerSignal below).
_RUST_BIN = '[[bin]]\nname = "x"\npath = "src/main.rs"\n'
_PY_SCRIPT = '[project.scripts]\nx = "x.main:main"\n'

# Rust needs one more thing: the marker crate's `deployment` feature.
# It is a cargo cfg gate, so without it generate-artefacts compiles out
# the contract emission and writes nothing. Helpers keep the fixtures
# honest about the real dependency shape.
_FEATURES = '{ version = "2.9", features = ["cli-service", "deployment"] }'


def _rust_dep(dep: str = "scalo") -> str:
    """A marker dep declared the way a real producer declares it."""
    return f"[dependencies]\n{dep} = {_FEATURES}\n"


class TestDetectTier:
    """Tier auto-detection covers the four expected outcomes."""

    def test_empty_dir_is_none(self, tmp_path: Path) -> None:
        assert detect_tier(tmp_path) == Tier.NONE

    def test_cargo_with_rustlib_is_rust(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "x"\n' + _RUST_BIN + _rust_dep("hyperi-rustlib"),
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.RUST

    def test_cargo_with_workspace_inheritance_is_rust(self, tmp_path: Path) -> None:
        # Real-world pattern from ci-test-rust-workspace/crates/cli/Cargo.toml.
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "x"\n'
            + _RUST_BIN
            + "[dependencies]\nhyperi-rustlib.workspace = true\n",
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.RUST

    def test_cargo_with_table_form_is_rust(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "x"\n'
            + _RUST_BIN
            + "[dependencies]\n"
            + 'hyperi-rustlib = { version = "2.5", features = ["cli", "deployment"] }\n',
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.RUST

    def test_cargo_with_scalo_is_rust(self, tmp_path: Path) -> None:
        # scalo is the current crate name (scalo-rs on crates.io).
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "x"\n' + _RUST_BIN + _rust_dep(),
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.RUST

    def test_cargo_without_rustlib_is_not_rust(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "x"\n' + _RUST_BIN + '[dependencies]\nserde = "1"\n',
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.NONE

    def test_pyproject_with_scalo_is_python(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\ndependencies = ["scalo>=2.28"]\n' + _PY_SCRIPT,
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.PYTHON

    def test_pyproject_with_scalo_extras_is_python(self, tmp_path: Path) -> None:
        # The metrics extra string still contains "scalo".
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\ndependencies = ["scalo[metrics]>=2.28"]\n'
            + _PY_SCRIPT,
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.PYTHON

    def test_pyproject_without_scalo_is_not_python(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\ndependencies = ["pyyaml>=6"]\n' + _PY_SCRIPT,
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.NONE

    def test_only_contract_json_is_other(self, tmp_path: Path) -> None:
        ci_dir = tmp_path / "ci"
        ci_dir.mkdir()
        (ci_dir / "deployment-contract.json").write_text(
            '{"app_name": "x"}', encoding="utf-8"
        )
        assert detect_tier(tmp_path) == Tier.OTHER

    def test_rust_wins_over_other(self, tmp_path: Path) -> None:
        # If a repo somehow has both Cargo.toml AND ci/deployment-contract.json,
        # rust takes precedence — the producer chain is more authoritative
        # than the manually maintained JSON.
        (tmp_path / "Cargo.toml").write_text(
            _RUST_BIN + _rust_dep("hyperi-rustlib"), encoding="utf-8"
        )
        ci_dir = tmp_path / "ci"
        ci_dir.mkdir()
        (ci_dir / "deployment-contract.json").write_text(
            '{"app_name": "x"}', encoding="utf-8"
        )
        assert detect_tier(tmp_path) == Tier.RUST

    def test_rust_wins_over_python(self, tmp_path: Path) -> None:
        # Some repos vendor both. Rust is the main producer; scalo is for
        # the integrations subdir. Rust takes precedence.
        (tmp_path / "Cargo.toml").write_text(
            _RUST_BIN + _rust_dep("hyperi-rustlib"), encoding="utf-8"
        )
        (tmp_path / "pyproject.toml").write_text(
            'dependencies = ["scalo>=2.24"]\n' + _PY_SCRIPT, encoding="utf-8"
        )
        assert detect_tier(tmp_path) == Tier.RUST

    def test_python_wins_over_other(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\ndependencies = ["scalo>=2.24"]\n' + _PY_SCRIPT,
            encoding="utf-8",
        )
        ci_dir = tmp_path / "ci"
        ci_dir.mkdir()
        (ci_dir / "deployment-contract.json").write_text(
            '{"app_name": "x"}', encoding="utf-8"
        )
        assert detect_tier(tmp_path) == Tier.PYTHON


class TestSelfMatchExclusion:
    """A library's own repo is NOT a Tier 1/2 consumer of itself.

    These tests cover the case where a library's Cargo.toml /
    pyproject.toml has its own package name (e.g. `hyperi-rustlib`)
    in the `[package]` / `[project]` table. The substring match would
    otherwise misdetect the library as a consumer and dispatch the
    Tier 1/2 producer, which then fails with "no binary found".
    """

    def test_scalo_rust_own_repo_is_not_rust(self, tmp_path: Path) -> None:
        # scalo-rs's own Cargo.toml has `name = "scalo"` in [package] but
        # no `scalo = ...` dep line — must not self-detect as Tier RUST.
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "scalo"\nversion = "2.9.0"\n',
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.NONE

    def test_hyperi_rustlib_own_repo_is_not_rust(self, tmp_path: Path) -> None:
        # The legacy library's own Cargo.toml has `name = "hyperi-rustlib"`
        # in [package] but no `hyperi-rustlib = ...` dep line.
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "hyperi-rustlib"\nversion = "2.7.0"\n',
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.NONE

    def test_hyperi_rustlib_with_self_in_dev_deps_still_excluded(
        self, tmp_path: Path
    ) -> None:
        # Hypothetical: library lists its own name in [dev-dependencies]
        # for an example-binary workspace pattern. Still not a consumer.
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "hyperi-rustlib"\nversion = "2.7.0"\n'
            '[dev-dependencies]\nhyperi-rustlib = { path = "." }\n',
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.NONE

    def test_scalo_own_repo_is_not_python(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "scalo"\nversion = "2.29.0"\n',
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.NONE

    def test_scalo_poetry_section_also_excluded(self, tmp_path: Path) -> None:
        # Poetry-managed projects use [tool.poetry] instead of [project].
        (tmp_path / "pyproject.toml").write_text(
            '[tool.poetry]\nname = "scalo"\nversion = "2.29.0"\n',
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.NONE

    def test_single_quoted_name_excluded(self, tmp_path: Path) -> None:
        # TOML allows single-quoted strings — must still match.
        (tmp_path / "Cargo.toml").write_text(
            "[package]\nname = 'hyperi-rustlib'\nversion = '2.7.0'\n",
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.NONE

    def test_consumer_with_real_dep_still_detected(self, tmp_path: Path) -> None:
        # A real consumer has its own name AND lists the library as a dep.
        # Self-match exclusion must not break this case.
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "dfe-loader"\n'
            + _RUST_BIN
            + _rust_dep("hyperi-rustlib"),
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.RUST

    def test_consumer_with_similar_prefix_not_misdetected(self, tmp_path: Path) -> None:
        # `name = "hyperi-rustlib-extras"` should NOT count as self-match
        # for `hyperi-rustlib` because the full string differs. This
        # consumer DOES depend on hyperi-rustlib.
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "hyperi-rustlib-extras"\n'
            + _RUST_BIN
            + _rust_dep("hyperi-rustlib"),
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.RUST

    def test_name_with_trailing_comment_handled(self, tmp_path: Path) -> None:
        # Inline comments after the name field shouldn't break parsing.
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "hyperi-rustlib"  # the library itself\n',
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.NONE

    def test_name_outside_recognised_section_not_self_match(
        self, tmp_path: Path
    ) -> None:
        # A `name = "hyperi-rustlib"` in some other table (e.g.
        # [features.something]) shouldn't count as the manifest's own
        # name. We only treat [package] / [project] / [tool.poetry] as
        # self-name sections.
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "consumer-app"\n'
            + _RUST_BIN
            + "[features.weird]\n"
            + 'name = "hyperi-rustlib"\n'
            + _rust_dep("hyperi-rustlib"),
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.RUST


class TestProducerSignal:
    """Depending on scalo is not the same as producing artefacts (#76).

    The culvert case: a VPN container that uses scalo for logging /
    config / secrets, builds from its own Dockerfile, and commits its
    deployment artefacts by hand. It carries the marker dep but has
    nothing to run `generate-artefacts` on, so the Build job used to
    die at the generate stage with exit 7.
    """

    def test_python_library_consumer_is_none(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "culvert"\ndependencies = ["scalo>=2.28"]\n',
            encoding="utf-8",
        )
        decision = resolve_tier(tmp_path)
        assert decision.tier == Tier.NONE
        assert decision.demoted
        assert "[project.scripts]" in decision.reason

    def test_rust_library_consumer_is_none(self, tmp_path: Path) -> None:
        # A lib crate depending on scalo: no [[bin]], no src/main.rs.
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "demo-lib"\n[lib]\n[dependencies]\nscalo = "2.9"\n',
            encoding="utf-8",
        )
        decision = resolve_tier(tmp_path)
        assert decision.tier == Tier.NONE
        assert decision.demoted

    def test_rust_implicit_main_rs_is_a_producer(self, tmp_path: Path) -> None:
        # No [[bin]] table — cargo discovers src/main.rs implicitly.
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "demo-app"\n' + _rust_dep(),
            encoding="utf-8",
        )
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
        assert detect_tier(tmp_path) == Tier.RUST

    def test_rust_src_bin_is_a_producer(self, tmp_path: Path) -> None:
        # cargo also auto-discovers src/bin/*.rs as binary targets.
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "demo-app"\n' + _rust_dep(),
            encoding="utf-8",
        )
        (tmp_path / "src" / "bin").mkdir(parents=True)
        (tmp_path / "src" / "bin" / "tool.rs").write_text(
            "fn main() {}\n", encoding="utf-8"
        )
        assert detect_tier(tmp_path) == Tier.RUST

    def test_rust_workspace_member_binary_is_a_producer(self, tmp_path: Path) -> None:
        # dfe-archiver shape: the workspace root pins the version, the
        # member inherits it and names the features it actually wants.
        (tmp_path / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["crates/app"]\n'
            '[workspace.dependencies]\nscalo = { version = "2.9" }\n',
            encoding="utf-8",
        )
        (tmp_path / "crates" / "app").mkdir(parents=True)
        (tmp_path / "crates" / "app" / "Cargo.toml").write_text(
            '[package]\nname = "app"\n[[bin]]\nname = "app"\npath = "src/main.rs"\n'
            "[dependencies]\n"
            'scalo = { workspace = true, features = ["cli-service", "deployment"] }\n',
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.RUST

    def test_rust_binary_without_deployment_feature_is_none(
        self, tmp_path: Path
    ) -> None:
        # A scalo app with a CLI but no `deployment` feature. This one
        # is nastier than the no-binary case: generate-artefacts EXISTS,
        # runs, and exits 0 having written no contract, so the failure
        # only surfaces in the container stage as "no deployment
        # artefacts found" — pointing at the wrong cause.
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "demo-app"\n'
            + _RUST_BIN
            + '[dependencies]\nscalo = { version = "2.9", features = ["cli-service"] }\n',
            encoding="utf-8",
        )
        decision = resolve_tier(tmp_path)
        assert decision.tier == Tier.NONE
        assert decision.demoted
        assert "deployment" in decision.reason

    def test_rust_default_features_only_is_none(self, tmp_path: Path) -> None:
        # A bare `scalo = "2.9"` takes default features, which do not
        # include deployment.
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "demo-app"\n'
            + _RUST_BIN
            + '[dependencies]\nscalo = "2.9"\n',
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.NONE

    def test_deployment_feature_in_dev_deps_does_not_count(
        self, tmp_path: Path
    ) -> None:
        # dev-dependencies do not affect the shipped binary.
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "demo-app"\n'
            + _RUST_BIN
            + '[dependencies]\nscalo = { version = "2.9", features = ["cli-service"] }\n'
            "[dev-dependencies]\n"
            'scalo = { version = "2.9", features = ["deployment"] }\n',
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.NONE

    def test_unparseable_features_stay_permissive(self, tmp_path: Path) -> None:
        # Workspace inheritance with no local feature list: we cannot
        # tell from here, so dispatch and let it fail loudly rather than
        # silently skipping a real producer.
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "demo-app"\n'
            + _RUST_BIN
            + "[dependencies]\nscalo.workspace = true\n",
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.RUST

    def test_python_needs_no_deployment_extra(self, tmp_path: Path) -> None:
        # The Rust `deployment` feature is a cfg gate; scalo-py's
        # `deployment` extra is only a pydantic pin. dfe-engine emits
        # contracts without declaring it, so requiring it here would
        # demote a real producer.
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "demo"\n'
            'dependencies = ["scalo[expression,http,metrics]>=2.29"]\n' + _PY_SCRIPT,
            encoding="utf-8",
        )
        assert detect_tier(tmp_path) == Tier.PYTHON

    def test_library_consumer_falls_through_to_tier3(self, tmp_path: Path) -> None:
        # A scalo library consumer that ALSO commits a Tier 3 contract
        # is a legitimate Tier 3 repo — the failed producer check must
        # fall through, not short-circuit the whole detection.
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "culvert"\ndependencies = ["scalo>=2.28"]\n',
            encoding="utf-8",
        )
        ci_dir = tmp_path / "ci"
        ci_dir.mkdir()
        (ci_dir / "deployment-contract.json").write_text(
            '{"app_name": "x"}', encoding="utf-8"
        )
        assert detect_tier(tmp_path) == Tier.OTHER

    def test_demoted_rust_does_not_fall_through_to_python(self, tmp_path: Path) -> None:
        # A Rust repo whose crate builds no binary, with a Python tools
        # subdir that DOES have scalo + a console script. Dispatching
        # the tools CLI as the producer would emit the wrong artefacts
        # silently, which is worse than skipping.
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "demo-lib"\n[lib]\n[dependencies]\nscalo = "2.9"\n',
            encoding="utf-8",
        )
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "demo-tools"\ndependencies = ["scalo>=2.28"]\n'
            + _PY_SCRIPT,
            encoding="utf-8",
        )
        decision = resolve_tier(tmp_path)
        assert decision.tier == Tier.NONE
        assert decision.demoted

    def test_both_deps_neither_a_producer_is_none(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "demo-lib"\n[lib]\n[dependencies]\nscalo = "2.9"\n',
            encoding="utf-8",
        )
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "demo-py"\ndependencies = ["scalo>=2.28"]\n',
            encoding="utf-8",
        )
        decision = resolve_tier(tmp_path)
        assert decision.tier == Tier.NONE
        assert decision.demoted

    def test_require_producer_false_takes_the_dep_alone(self, tmp_path: Path) -> None:
        # What `deployment.producer: true` does: the marker dep alone
        # selects the tier, for a producer whose shape we can't see.
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "culvert"\ndependencies = ["scalo>=2.28"]\n',
            encoding="utf-8",
        )
        assert resolve_tier(tmp_path, require_producer=False).tier == Tier.PYTHON

    def test_no_marker_dep_is_not_demoted(self, tmp_path: Path) -> None:
        # `demoted` marks "has the dep, lacks the producer" specifically
        # — a plain repo with neither shouldn't be nudged towards
        # deployment.producer: true.
        decision = resolve_tier(tmp_path)
        assert decision.tier == Tier.NONE
        assert not decision.demoted


class TestTierEnum:
    """`Tier` enum has the four values expected by callers."""

    @pytest.mark.parametrize(
        "name,value",
        [
            ("RUST", "rust"),
            ("PYTHON", "python"),
            ("OTHER", "other"),
            ("NONE", "none"),
        ],
    )
    def test_tier_values(self, name: str, value: str) -> None:
        assert getattr(Tier, name).value == value

    def test_tier_string_compatible(self) -> None:
        # StrEnum lets callers compare directly to a string value.
        assert Tier.RUST == "rust"
        assert "python" == Tier.PYTHON
