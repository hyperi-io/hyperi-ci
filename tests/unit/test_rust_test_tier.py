# Project:   HyperI CI
# File:      tests/unit/test_rust_test_tier.py
# Purpose:   Rust test commands per test tier, and the tier notice counts
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from pathlib import Path
from typing import Any

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.languages.rust import test as rust_test
from hyperi_ci.languages.rust.test import _build_test_cmd, _run_coverage, tier_detail
from hyperi_ci.languages.tiering import SuiteTier

MODULE = "hyperi_ci.languages.rust.test"
FULL_NEXTEST = ["--run-ignored", "all", "--ignore-default-filter"]

# Real cargo-nextest 0.9.146 output, one #[ignore] test and one test left out
# by `default-filter`. The rule lines nextest draws are left out: not ASCII.
_NEXTEST_CORE = """\
 Nextest run ID d9de5ea4-7839-4dcb-90be-b36d9264d953 with nextest profile: default
    Starting 1 test across 1 binary (2 tests skipped, including 1 test via profile.default.default-filter)
        PASS [   0.006s] (1/1) nextest-probe tests::plain
     Summary [   0.007s] 1 test run: 1 passed, 2 skipped
"""

_NEXTEST_FULL = """\
    Starting 3 tests across 1 binary
        PASS [   0.007s] (1/3) nextest-probe tests::ignored_one
        PASS [   0.008s] (2/3) nextest-probe tests::heavy_filtered
        PASS [   0.009s] (3/3) nextest-probe tests::plain
     Summary [   0.010s] 3 tests run: 3 passed, 0 skipped
"""

# CARGO_TERM_COLOR=always, as nextest wrote it into a pipe.
_NEXTEST_COLOURED = (
    "\x1b[32;1m     Summary\x1b[0m [   0.006s] \x1b[1m1\x1b[0m test run: "
    "\x1b[1m1\x1b[0m \x1b[32;1mpassed\x1b[0m, \x1b[1m2\x1b[0m \x1b[33;1mskipped\x1b[0m"
)

_NEXTEST_FAIL_FAST = (
    "     Summary [   1.204s] 3/10 tests run: 2 passed, 1 failed, 5 skipped\n"
    "warning: 7/10 tests were not run due to test failure\n"
)

# Real cargo test output: lib, one integration binary, doctests.
_LIBTEST = """\
running 3 tests
test result: ok. 2 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out; finished in 0.00s
running 1 test
test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.01s
   Doc-tests nextest_probe
test result: ok. 0 passed; 0 failed; 4 ignored; 0 measured; 0 filtered out; finished in 0.00s
"""


class TestTierDetail:
    def test_nextest_core_names_the_skipped_count(self) -> None:
        assert tier_detail(_NEXTEST_CORE) == "1 run, 2 skipped"

    def test_nextest_full_reports_zero_skipped(self) -> None:
        assert tier_detail(_NEXTEST_FULL) == "3 run, 0 skipped"

    def test_colour_codes_do_not_hide_the_counts(self) -> None:
        assert tier_detail(_NEXTEST_COLOURED) == "1 run, 2 skipped"

    def test_a_cancelled_run_keeps_its_fraction(self) -> None:
        assert tier_detail(_NEXTEST_FAIL_FAST) == "3/10 run, 1 failed, 5 skipped"

    def test_libtest_results_are_summed_across_binaries(self) -> None:
        assert tier_detail(_LIBTEST) == "2 passed, 1 failed, 5 ignored"

    def test_no_summary_is_unknown_not_zero(self) -> None:
        assert "counts unknown" in tier_detail("error: could not compile\n")


class TestCommandPerTier:
    def test_core_nextest_command_is_unchanged(self) -> None:
        assert _build_test_cmd("all", runner="nextest") == [
            "cargo",
            "nextest",
            "run",
            "--all-features",
        ]

    def test_core_cargo_command_is_unchanged(self) -> None:
        assert _build_test_cmd("all", runner="cargo") == [
            "cargo",
            "test",
            "--all-features",
        ]

    def test_full_nextest_runs_ignored_and_default_filtered(self) -> None:
        cmd = _build_test_cmd("all", runner="nextest", test_tier=SuiteTier.FULL)
        assert cmd == ["cargo", "nextest", "run", "--all-features", *FULL_NEXTEST]

    def test_full_cargo_includes_ignored_after_the_separator(self) -> None:
        cmd = _build_test_cmd("all", runner="cargo", test_tier=SuiteTier.FULL)
        assert cmd[-2:] == ["--", "--include-ignored"]

    def test_full_and_serial_share_one_separator(self) -> None:
        cmd = _build_test_cmd(
            "default",
            rust_tier="integration",
            runner="cargo",
            test_tier=SuiteTier.FULL,
        )
        assert cmd.count("--") == 1
        assert cmd[-3:] == ["--", "--test-threads=1", "--include-ignored"]

    def test_full_nextest_keeps_the_serial_jobs_flag(self) -> None:
        cmd = _build_test_cmd(
            "default", rust_tier="e2e", runner="nextest", test_tier=SuiteTier.FULL
        )
        assert cmd[-5:] == ["--jobs", "1", *FULL_NEXTEST]
        assert "--" not in cmd


class _Streams:
    def __init__(self, output: str = _NEXTEST_CORE, rc: int = 0) -> None:
        self.commands: list[list[str]] = []
        self.notices: list[tuple[SuiteTier, str]] = []
        self._output = output
        self._rc = rc

    def stream(self, cmd: list[str], *, on_line: Any = None, **_kw: Any) -> Any:
        self.commands.append(cmd)
        for line in self._output.splitlines():
            on_line(line)
        return self._rc, self._output

    def announce(self, tier: SuiteTier, detail: str) -> None:
        self.notices.append((tier, detail))


@pytest.fixture
def streams(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Streams:
    monkeypatch.chdir(tmp_path)
    rec = _Streams()
    monkeypatch.setattr(f"{MODULE}.stream_cmd", rec.stream)
    monkeypatch.setattr(f"{MODULE}.announce_tier", rec.announce)
    monkeypatch.setattr(f"{MODULE}.subprocess.run", lambda *_a, **_k: None)
    monkeypatch.setattr(f"{MODULE}._has_nextest", lambda: True)
    return rec


def _only_tool(monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    monkeypatch.setattr(
        f"{MODULE}.shutil.which", lambda name: "/usr/bin/x" if name == tool else None
    )


class TestCoveragePerTier:
    def test_llvm_cov_nextest_takes_the_nextest_switches(
        self, monkeypatch: pytest.MonkeyPatch, streams: _Streams
    ) -> None:
        _only_tool(monkeypatch, "cargo-llvm-cov")
        assert _run_coverage("all", runner="nextest", test_tier=SuiteTier.FULL) == 0
        assert streams.commands == [
            [
                "cargo",
                "llvm-cov",
                "nextest",
                "--lcov",
                "--output-path",
                "test-results/lcov.info",
                "--all-features",
                *FULL_NEXTEST,
            ]
        ]

    def test_llvm_cov_core_is_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, streams: _Streams
    ) -> None:
        _only_tool(monkeypatch, "cargo-llvm-cov")
        _run_coverage("all", runner="nextest")
        assert streams.commands[0][-1] == "--all-features"

    def test_llvm_cov_on_libtest_includes_ignored(
        self, monkeypatch: pytest.MonkeyPatch, streams: _Streams
    ) -> None:
        _only_tool(monkeypatch, "cargo-llvm-cov")
        _run_coverage("all", runner="cargo", test_tier=SuiteTier.FULL)
        assert streams.commands[0][-2:] == ["--", "--include-ignored"]

    def test_tarpaulin_includes_ignored(
        self, monkeypatch: pytest.MonkeyPatch, streams: _Streams
    ) -> None:
        _only_tool(monkeypatch, "cargo-tarpaulin")
        _run_coverage("all", runner="cargo", test_tier=SuiteTier.FULL)
        assert streams.commands[0][:2] == ["cargo", "tarpaulin"]
        assert streams.commands[0][-2:] == ["--", "--include-ignored"]


class TestRunAnnouncesEachFeatureSet:
    def test_one_notice_per_feature_set(
        self, monkeypatch: pytest.MonkeyPatch, streams: _Streams
    ) -> None:
        _only_tool(monkeypatch, "cargo-nextest")
        config = CIConfig(_raw={"test": {"coverage": False}})
        rc = rust_test.run(
            config, extra_env={"RUST_FEATURES": "a|b", "TEST_TIER": "core"}
        )
        assert rc == 0
        assert streams.notices == [
            (SuiteTier.CORE, "features a: 1 run, 2 skipped"),
            (SuiteTier.CORE, "features b: 1 run, 2 skipped"),
        ]

    def test_full_reaches_the_command(
        self, monkeypatch: pytest.MonkeyPatch, streams: _Streams
    ) -> None:
        _only_tool(monkeypatch, "cargo-nextest")
        config = CIConfig(_raw={"test": {"coverage": False}})
        assert rust_test.run(config, extra_env={"TEST_TIER": "full"}) == 0
        assert streams.commands[0][-3:] == FULL_NEXTEST
        assert streams.notices[0][0] is SuiteTier.FULL

    def test_a_failure_is_announced_and_returned(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        failing = _Streams(output=_NEXTEST_FAIL_FAST, rc=100)
        monkeypatch.setattr(f"{MODULE}.stream_cmd", failing.stream)
        monkeypatch.setattr(f"{MODULE}.announce_tier", failing.announce)
        monkeypatch.setattr(f"{MODULE}._has_nextest", lambda: True)
        config = CIConfig(_raw={"test": {"coverage": False}})
        assert rust_test.run(config, extra_env={"TEST_TIER": "full"}) == 100
        assert failing.notices == [(SuiteTier.FULL, "3/10 run, 1 failed, 5 skipped")]

    def test_rust_tier_subset_still_selects_one_kind(
        self, monkeypatch: pytest.MonkeyPatch, streams: _Streams
    ) -> None:
        """`test.rust.tier` and the test tier are separate axes."""
        _only_tool(monkeypatch, "cargo-nextest")
        config = CIConfig(_raw={"test": {"coverage": False, "rust": {"tier": "unit"}}})
        assert rust_test.run(config, extra_env={"TEST_TIER": "full"}) == 0
        assert len(streams.commands) == 1
        assert "--lib" in streams.commands[0]
        assert streams.commands[0][-3:] == FULL_NEXTEST
        assert streams.notices[0][1].startswith("unit: ")


def _full_config(nextest: bool = True, **full_rust: Any) -> CIConfig:
    return CIConfig(
        _raw={
            "test": {
                "coverage": False,
                "rust": {"nextest": nextest},
                "full": {"rust": full_rust},
            }
        }
    )


class TestFullExclusions:
    """Full runs what the runner CAN execute; live-cloud and perf tests stay out."""

    def test_filterset_and_skip_reach_nextest(
        self, monkeypatch: pytest.MonkeyPatch, streams: _Streams
    ) -> None:
        _only_tool(monkeypatch, "cargo-nextest")
        config = _full_config(filter="not test(/live_aws/)", skip=["perf_", "manual"])
        assert rust_test.run(config, extra_env={"TEST_TIER": "full"}) == 0
        assert streams.commands == [
            [
                "cargo",
                "nextest",
                "run",
                "--all-features",
                *FULL_NEXTEST,
                "-E",
                "not test(/live_aws/)",
                "--",
                "--skip",
                "perf_",
                "--skip",
                "manual",
            ]
        ]

    def test_skip_reaches_cargo_test_after_include_ignored(
        self, monkeypatch: pytest.MonkeyPatch, streams: _Streams
    ) -> None:
        _only_tool(monkeypatch, "nothing")
        config = _full_config(nextest=False, skip=["smoke_remote"])
        assert rust_test.run(config, extra_env={"TEST_TIER": "full"}) == 0
        assert streams.commands == [
            [
                "cargo",
                "test",
                "--all-features",
                "--",
                "--include-ignored",
                "--skip",
                "smoke_remote",
            ]
        ]

    def test_filterset_under_cargo_test_fails_rather_than_run_it_all(
        self, streams: _Streams
    ) -> None:
        """A filterset libtest cannot apply would let live-cloud tests run."""
        config = _full_config(nextest=False, filter="not test(/live_aws/)")
        assert rust_test.run(config, extra_env={"TEST_TIER": "full"}) == 1
        assert streams.commands == []

    def test_filterset_under_tarpaulin_fails(
        self, monkeypatch: pytest.MonkeyPatch, streams: _Streams
    ) -> None:
        _only_tool(monkeypatch, "cargo-tarpaulin")
        selection = rust_test.FullSelection(filterset="not test(x)")
        rc = _run_coverage(
            "all", runner="nextest", test_tier=SuiteTier.FULL, selection=selection
        )
        assert rc == 1
        assert streams.commands == []

    def test_llvm_cov_nextest_takes_filterset_and_skip(
        self, monkeypatch: pytest.MonkeyPatch, streams: _Streams
    ) -> None:
        _only_tool(monkeypatch, "cargo-llvm-cov")
        selection = rust_test.FullSelection(filterset="not test(x)", skip=("perf_",))
        _run_coverage(
            "all", runner="nextest", test_tier=SuiteTier.FULL, selection=selection
        )
        assert streams.commands[0][-8:] == [
            *FULL_NEXTEST,
            "-E",
            "not test(x)",
            "--",
            "--skip",
            "perf_",
        ]

    def test_exclusions_do_not_touch_core(
        self, monkeypatch: pytest.MonkeyPatch, streams: _Streams
    ) -> None:
        _only_tool(monkeypatch, "cargo-nextest")
        config = _full_config(filter="not test(x)", skip=["perf_"])
        assert rust_test.run(config, extra_env={"TEST_TIER": "core"}) == 0
        assert streams.commands == [["cargo", "nextest", "run", "--all-features"]]

    @pytest.mark.parametrize(
        ("full_rust", "key"),
        [
            ({"filter": ["not test(x)"]}, "test.full.rust.filter"),
            ({"skip": "perf_"}, "test.full.rust.skip"),
            ({"skip": ["perf_", ""]}, "test.full.rust.skip"),
        ],
    )
    def test_malformed_keys_fail_the_stage(
        self,
        monkeypatch: pytest.MonkeyPatch,
        streams: _Streams,
        full_rust: dict[str, Any],
        key: str,
    ) -> None:
        said: list[str] = []
        monkeypatch.setattr(f"{MODULE}.error", said.append)
        config = _full_config(**full_rust)
        assert rust_test.run(config, extra_env={"TEST_TIER": "full"}) == 1
        assert streams.commands == []
        assert key in said[0]
