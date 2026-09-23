# Project:   HyperI CI
# File:      tests/unit/test_fleet_gate.py
# Purpose:   Tests for the rehearsal gate and the full-fleet sweep
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""The two ways a fleet gate lies, and the tests that stop it (issue #215).

It reports success on having run NOTHING, or it treats a repo it could not
reach as a repo that is fine. Both look identical to a green tick from outside,
which is why each has its own test here rather than being folded into the
happy path.
"""

import importlib.util
import sys
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))

import fixture_fleet  # noqa: E402


def _load(stem: str, name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / "scripts" / stem)
    assert spec is not None and spec.loader is not None  # a real file always resolves
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load("rehearse-gate.py", "rehearse_gate")
sweep = _load("sweep-fleet.py", "sweep_fleet")
rehearse = _load("rehearse-branch.py", "rehearse_branch")


class TestTheRehearsalRecord:
    """What the rehearsal writes is what the gate reads back."""

    def test_round_trip(self) -> None:
        block = rehearse.record_block("a" * 40, 12345, "pass")
        record = rehearse.parse_record("body text" + block)
        assert record == {
            "hyperi-ci-sha": "a" * 40,
            "run-id": "12345",
            "verdict": "pass",
        }

    def test_a_body_without_a_record_reads_as_none(self) -> None:
        assert rehearse.parse_record("Throwaway rehearsal PR.") is None

    def test_an_empty_body_reads_as_none(self) -> None:
        assert rehearse.parse_record(None) is None
        assert rehearse.parse_record("") is None

    def test_a_truncated_record_reads_as_none(self) -> None:
        """Half a record is not a rehearsal."""
        assert rehearse.parse_record("Rehearsal record\nhyperi-ci-sha: abc\n") is None

    def test_the_newest_record_wins(self) -> None:
        body = (
            "x"
            + rehearse.record_block("a" * 40, 1, "fail")
            + rehearse.record_block("b" * 40, 2, "pass")
        )
        record = rehearse.parse_record(body)
        assert record is not None
        assert record["hyperi-ci-sha"] == "b" * 40

    def test_find_record_matches_the_commit_not_the_fixture(self) -> None:
        prs = [
            {"body": rehearse.record_block("a" * 40, 1, "pass")},
            {"body": rehearse.record_block("b" * 40, 2, "pass")},
        ]
        found = gate.find_record(prs, "b" * 40)
        assert found is not None and found["run-id"] == "2"

    def test_a_record_for_another_commit_does_not_count(self) -> None:
        """Push another commit and the rehearsal stops applying."""
        prs = [{"body": rehearse.record_block("a" * 40, 1, "pass")}]
        assert gate.find_record(prs, "c" * 40) is None


class TestTheGateVerdict:
    def test_nothing_required_is_a_pass(self) -> None:
        code, _ = gate.gate_verdict([], [])
        assert code == 0

    def test_all_proven_passes(self) -> None:
        outcomes = [gate.Outcome("ci-test-go-app", gate.PROVEN, "run 1")]
        assert gate.gate_verdict(["ci-test-go-app"], outcomes)[0] == 0

    def test_required_but_nothing_checked_is_not_a_pass(self) -> None:
        """The failure this gate exists for: a green tick over zero evidence."""
        code, lines = gate.gate_verdict(["ci-test-go-app"], [])
        assert code == 2
        assert any("NOT CHECKED" in line for line in lines)

    def test_one_fixture_answered_out_of_two_is_not_a_pass(self) -> None:
        outcomes = [gate.Outcome("ci-test-go-app", gate.PROVEN, "run 1")]
        code, lines = gate.gate_verdict(
            ["ci-test-go-app", "ci-test-rust-app"], outcomes
        )
        assert code == 2
        assert any("ci-test-rust-app" in line for line in lines)

    def test_an_unreachable_fixture_is_not_a_pass(self) -> None:
        outcomes = [gate.Outcome("ci-test-go-app", gate.UNREACHABLE, "no token")]
        assert gate.gate_verdict(["ci-test-go-app"], outcomes)[0] == 2

    def test_unreachable_outranks_unproven(self) -> None:
        """Unknown and known-bad are different answers; say the weaker one."""
        outcomes = [
            gate.Outcome("ci-test-go-app", gate.UNPROVEN, "no record"),
            gate.Outcome("ci-test-rust-app", gate.UNREACHABLE, "gh failed"),
        ]
        code, _ = gate.gate_verdict(["ci-test-go-app", "ci-test-rust-app"], outcomes)
        assert code == 2

    def test_an_unrehearsed_fixture_fails(self) -> None:
        outcomes = [gate.Outcome("ci-test-go-app", gate.UNPROVEN, "no record")]
        assert gate.gate_verdict(["ci-test-go-app"], outcomes)[0] == 1

    def test_a_failed_rehearsal_fails(self) -> None:
        outcomes = [gate.Outcome("ci-test-go-app", gate.FAILED, "run 9 did not pass")]
        assert gate.gate_verdict(["ci-test-go-app"], outcomes)[0] == 1

    def test_the_advice_names_the_command(self) -> None:
        lines = gate._advice("fix/thing", ["ci-test-go-app"])
        assert any(
            "rehearse-branch.py --branch fix/thing --repo hyperi-io/ci-test-go-app"
            in line
            for line in lines
        )


class TestTheSweepVerdict:
    def test_a_green_fleet_passes(self) -> None:
        results = [sweep.Result("ci-test-go-app", sweep.PASS, "run 1")]
        assert sweep.sweep_verdict(["ci-test-go-app"], results)[0] == 0

    def test_selecting_nothing_is_not_a_pass(self) -> None:
        code, lines = sweep.sweep_verdict([], [])
        assert code == 2
        assert any("0 fixtures" in line for line in lines)

    def test_running_nothing_is_not_a_pass(self) -> None:
        """A sweep that dispatched nothing has proven nothing about main."""
        code, lines = sweep.sweep_verdict(["ci-test-go-app"], [])
        assert code == 2
        assert any("0 fixtures" in line for line in lines)

    def test_a_fixture_with_no_answer_is_not_a_pass(self) -> None:
        results = [sweep.Result("ci-test-go-app", sweep.PASS, "run 1")]
        code, lines = sweep.sweep_verdict(
            ["ci-test-go-app", "ci-test-rust-app"], results
        )
        assert code == 2
        assert any("NOT RUN" in line and "ci-test-rust-app" in line for line in lines)

    def test_an_unreachable_repo_is_not_a_pass(self) -> None:
        results = [
            sweep.Result("ci-test-go-app", sweep.PASS, "run 1"),
            sweep.Result("ci-test-rust-app", sweep.UNREACHABLE, "dispatch refused"),
        ]
        code, _ = sweep.sweep_verdict(["ci-test-go-app", "ci-test-rust-app"], results)
        assert code == 2

    def test_a_failing_fixture_is_a_red_fleet(self) -> None:
        results = [sweep.Result("ci-test-go-app", sweep.FAIL, "run 1")]
        assert sweep.sweep_verdict(["ci-test-go-app"], results)[0] == 1

    def test_a_timed_out_run_is_a_red_fleet(self) -> None:
        results = [sweep.Result("ci-test-go-app", sweep.TIMEOUT, "run 1 still going")]
        assert sweep.sweep_verdict(["ci-test-go-app"], results)[0] == 1

    def test_a_failure_outranks_an_unreachable_neighbour(self) -> None:
        """A proven failure is an answer whatever else could not be read.

        Reported as inconclusive, negative-cases prints "it did not prove any
        gate" over a gate it just proved leaks.
        """
        results = [
            sweep.Result("ci-test-go-app", sweep.FAIL, "run 1"),
            sweep.Result("ci-test-rust-app", sweep.UNREACHABLE, "gone"),
        ]
        code, lines = sweep.sweep_verdict(
            ["ci-test-go-app", "ci-test-rust-app"], results
        )
        assert code == 1
        assert any("unreachable" in line for line in lines)

    def test_a_failure_outranks_a_fixture_that_never_ran(self) -> None:
        results = [sweep.Result("ci-test-go-app", sweep.FAIL, "run 1")]
        code, lines = sweep.sweep_verdict(
            ["ci-test-go-app", "ci-test-rust-app"], results
        )
        assert code == 1
        assert any("NOT RUN" in line and "ci-test-rust-app" in line for line in lines)


class TestSelectingSweepTargets:
    FLEET = [
        {"name": "ci-test-rs-app", "language": "rust"},
        {"name": "ci-test-go-app", "language": "go"},
        {"name": "ci-test-py-app", "language": "python"},
    ]

    def test_the_whole_fleet_by_default_in_name_order(self) -> None:
        names = [e["name"] for e in sweep.select_targets(self.FLEET, [], "")]
        assert names == ["ci-test-go-app", "ci-test-py-app", "ci-test-rs-app"]

    def test_one_language(self) -> None:
        names = [e["name"] for e in sweep.select_targets(self.FLEET, [], "rust")]
        assert names == ["ci-test-rs-app"]

    def test_an_explicit_list(self) -> None:
        names = [
            e["name"] for e in sweep.select_targets(self.FLEET, ["ci-test-go-app"], "")
        ]
        assert names == ["ci-test-go-app"]

    def test_a_filter_matching_nobody_returns_empty_rather_than_the_fleet(self) -> None:
        """Returning everything on an unmatched filter would sweep the fleet by
        accident; returning empty lets the verdict refuse it."""
        assert sweep.select_targets(self.FLEET, [], "cobol") == []


class TestThePushFilterIsTheConsumerSurface:
    """The sweep's `paths:` filter and config/fixtures.yaml have to agree about
    which workflows a consumer actually runs."""

    def _sweep_workflow(self) -> dict:
        text = (_ROOT / ".github" / "workflows" / "fleet-sweep.yml").read_text("utf-8")
        return yaml.safe_load(text)

    def test_the_exclusions_are_exactly_the_non_consumer_workflows(self) -> None:
        fleet = fixture_fleet.load_fleet()
        present = {p.name for p in (_ROOT / ".github" / "workflows").glob("*.yml")}
        consumed = fixture_fleet.language_workflows(fleet) | {
            name for name in present if name.startswith("_")
        }
        # `on` parses as the boolean True in YAML 1.1, which is what PyYAML is.
        paths = self._sweep_workflow()[True]["push"]["paths"]
        excluded = {
            p.removeprefix("!.github/workflows/") for p in paths if p.startswith("!")
        }
        assert excluded == present - consumed

    def test_the_filter_covers_both_surfaces(self) -> None:
        paths = self._sweep_workflow()[True]["push"]["paths"]
        assert ".github/workflows/**" in paths
        assert ".github/actions/**" in paths
