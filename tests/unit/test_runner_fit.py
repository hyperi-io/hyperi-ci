# Project:   HyperI CI
# File:      tests/unit/test_runner_fit.py
# Purpose:   Tests for the Rust Build runner-fit record, verdict and fleet report
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for :mod:`hyperi_ci.languages.rust.runner_fit` and the report on top."""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from hyperi_ci import runner_report
from hyperi_ci.config import CIConfig
from hyperi_ci.gh import ApiResult
from hyperi_ci.languages.rust import _jobs, build, runner_fit
from hyperi_ci.languages.rust.runner_fit import (
    FitSettings,
    RunnerProfile,
    parse_settings,
    verdict,
)
from hyperi_ci.memory import GIB, MemoryLimit

REPO_ROOT = Path(__file__).resolve().parents[2]
ARC = "arc-native-16cpu"
NS_AMD = "namespace-profile-hyperi-amd64-32x64"
NS_ARM_16 = "namespace-profile-hyperi-arm64-16x32"
NS_ARM_32 = "namespace-profile-hyperi-arm64-32x64"

SETTINGS = FitSettings(
    min_headroom=0.2,
    min_jobs=2,
    per_job_drift=0.25,
    profiles=(
        RunnerProfile(ARC, "amd64", 16, 32),
        RunnerProfile(NS_AMD, "amd64", 32, 64),
        RunnerProfile(NS_ARM_16, "arm64", 16, 32),
        RunnerProfile(NS_ARM_32, "arm64", 32, 64),
    ),
)


def _verdict(**overrides: Any) -> runner_fit.Verdict:
    args: dict[str, Any] = {
        "arch": "amd64",
        "runner_label": ARC,
        "per_job_peak_gib": 5.0,
        "max_peak_gib": 10.0,
        "limit_gib": 32.0,
        "memory_per_job_gib": 5.0,
        "settings": SETTINGS,
    }
    return verdict(**{**args, **overrides})


class TestVerdict:
    @pytest.mark.parametrize(
        ("overrides", "status", "recommended", "move"),
        [
            pytest.param({}, "fits", ARC, False, id="fits-and-stays"),
            pytest.param(
                {"max_peak_gib": 27.0, "per_job_peak_gib": 5.0},
                "tight",
                ARC,
                False,
                id="tight-at-the-peak-yet-two-jobs-still-fit",
            ),
            pytest.param(
                {"runner_label": NS_AMD}, "fits", ARC, True, id="oversized-moves-down"
            ),
            pytest.param(
                {"per_job_peak_gib": 13.1, "max_peak_gib": 26.2, "limit_gib": 64.0},
                "fits",
                NS_AMD,
                True,
                id="too-big-for-arc-moves-up",
            ),
            pytest.param(
                {"per_job_peak_gib": 12.8, "max_peak_gib": 25.6},
                "tight",
                ARC,
                False,
                id="exactly-at-the-profile-edge-fits",
            ),
            pytest.param(
                {"per_job_peak_gib": 40.0, "max_peak_gib": 40.0, "limit_gib": 64.0},
                "fits",
                None,
                False,
                id="nothing-holds-it",
            ),
            pytest.param(
                {
                    "arch": "arm64",
                    "runner_label": NS_ARM_16,
                    "per_job_peak_gib": 13.1,
                    "max_peak_gib": 26.2,
                    "limit_gib": 32.0,
                },
                "tight",
                NS_ARM_32,
                True,
                id="arm64-picks-only-arm64-profiles",
            ),
            pytest.param(
                {"runner_label": None}, "fits", ARC, True, id="unknown-label-moves"
            ),
        ],
    )
    def test_recommendation(
        self,
        overrides: dict[str, Any],
        status: str,
        recommended: str | None,
        move: bool,
    ) -> None:
        got = _verdict(**overrides)
        assert (got.status, got.recommended, got.move) == (status, recommended, move)
        assert bool(got.move_reason) is (move or recommended is None)

    @pytest.mark.parametrize(
        ("overrides", "reason_has"),
        [
            pytest.param({"max_peak_gib": None}, "no memory peak", id="no-peak"),
            pytest.param({"limit_gib": None}, "no memory limit", id="no-limit"),
            pytest.param({"limit_gib": 0.0}, "no memory limit", id="zero-limit"),
            pytest.param(
                {"max_peak_gib": None, "limit_gib": None},
                "no memory peak recorded, no memory limit found",
                id="neither",
            ),
        ],
    )
    def test_unknown_names_what_is_missing(
        self, overrides: dict[str, Any], reason_has: str
    ) -> None:
        got = _verdict(**overrides)
        assert got.status == "unknown"
        assert reason_has in got.reason

    def test_unknown_still_recommends_when_the_per_job_peak_is_known(self) -> None:
        got = _verdict(limit_gib=None)
        assert (got.status, got.recommended) == ("unknown", ARC)

    def test_no_per_job_peak_means_no_recommendation(self) -> None:
        got = _verdict(per_job_peak_gib=None)
        assert got.recommended is None
        assert "no per-job peak" in got.reason

    def test_unusable_settings_are_unknown(self) -> None:
        got = _verdict(settings=None)
        assert (got.status, got.recommended) == ("unknown", None)

    @pytest.mark.parametrize(
        ("per_job", "planned", "noted"),
        [
            pytest.param(13.1, 14.0, False, id="close-to-the-plan"),
            pytest.param(13.1, 2.0, True, id="far-above-the-plan"),
            pytest.param(2.0, 14.0, True, id="far-below-the-plan"),
            pytest.param(3.0, 4.0, False, id="exactly-at-the-drift-limit"),
            pytest.param(13.1, None, False, id="no-plan-to-compare"),
        ],
    )
    def test_drift_note(
        self, per_job: float, planned: float | None, noted: bool
    ) -> None:
        got = _verdict(per_job_peak_gib=per_job, memory_per_job_gib=planned)
        assert bool(got.notes) is noted
        if noted:
            assert f"{per_job:.1f}" in got.notes[0]
            assert f"{planned:g}" in got.notes[0]

    def test_move_reason_names_the_numbers_and_the_cost_order(self) -> None:
        got = _verdict(runner_label=NS_AMD)
        assert got.move_reason == (
            f"per-job peak 5.0 GiB x 2 jobs fits {ARC} (32 GiB), cheaper than {NS_AMD}"
        )

    @pytest.mark.parametrize(
        ("label", "ending"),
        [
            pytest.param(NS_AMD, f"cheaper than {NS_AMD}", id="current-dearer"),
            pytest.param(
                "some-other-runner",
                "some-other-runner is not in build.rust.runner_fit.profiles",
                id="current-unlisted",
            ),
            pytest.param(None, "the runner label is unknown", id="no-label"),
        ],
    )
    def test_move_reason_endings(self, label: str | None, ending: str) -> None:
        assert _verdict(runner_label=label).move_reason.endswith(ending)

    def test_current_runner_too_small(self) -> None:
        got = _verdict(
            runner_label=ARC, per_job_peak_gib=13.1, max_peak_gib=26.2, limit_gib=64.0
        )
        assert got.recommended == NS_AMD
        assert got.move_reason.endswith(f"{ARC} is too small for it")


class TestSettings:
    def test_shipped_defaults_parse(self) -> None:
        parsed = parse_settings(CIConfig(_raw={}).setting("build.rust.runner_fit"))
        assert parsed is not None
        assert (parsed.min_headroom, parsed.min_jobs) == (0.2, 2)
        # Cheapest first per arch, so the first profile that holds a build is the pick.
        for arch in ("amd64", "arm64"):
            sizes = [p.memory_gib for p in parsed.profiles if p.arch == arch]
            assert sizes and sizes == sorted(sizes), arch
        # x64 under 32 cores runs on ARC, so ARC sizes come before Namespace.
        amd64 = [p.label for p in parsed.profiles if p.arch == "amd64"]
        assert amd64[-1].startswith("namespace-profile-") and all(
            label.startswith("arc-") for label in amd64[:-1]
        )

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param(None, id="absent"),
            pytest.param("fast", id="not-a-mapping"),
            pytest.param({}, id="empty"),
            pytest.param(
                {
                    "min_headroom": 1.0,
                    "min_jobs": 2,
                    "per_job_drift": 0.2,
                    "profiles": [
                        {"label": "a", "arch": "amd64", "cpus": 4, "memory_gib": 8}
                    ],
                },
                id="headroom-one",
            ),
            pytest.param(
                {
                    "min_headroom": "0.2",
                    "min_jobs": 2,
                    "per_job_drift": 0.2,
                    "profiles": [
                        {"label": "a", "arch": "amd64", "cpus": 4, "memory_gib": 8}
                    ],
                },
                id="headroom-string",
            ),
            pytest.param(
                {
                    "min_headroom": 0.2,
                    "min_jobs": 0,
                    "per_job_drift": 0.2,
                    "profiles": [
                        {"label": "a", "arch": "amd64", "cpus": 4, "memory_gib": 8}
                    ],
                },
                id="zero-jobs",
            ),
            pytest.param(
                {
                    "min_headroom": 0.2,
                    "min_jobs": True,
                    "per_job_drift": 0.2,
                    "profiles": [
                        {"label": "a", "arch": "amd64", "cpus": 4, "memory_gib": 8}
                    ],
                },
                id="bool-jobs",
            ),
            pytest.param(
                {
                    "min_headroom": 0.2,
                    "min_jobs": 2,
                    "per_job_drift": -1,
                    "profiles": [
                        {"label": "a", "arch": "amd64", "cpus": 4, "memory_gib": 8}
                    ],
                },
                id="negative-drift",
            ),
            pytest.param(
                {
                    "min_headroom": 0.2,
                    "min_jobs": 2,
                    "per_job_drift": 0.2,
                    "profiles": [],
                },
                id="no-profiles",
            ),
            pytest.param(
                {
                    "min_headroom": 0.2,
                    "min_jobs": 2,
                    "per_job_drift": 0.2,
                    "profiles": "arc",
                },
                id="profiles-not-a-list",
            ),
            pytest.param(
                {
                    "min_headroom": 0.2,
                    "min_jobs": 2,
                    "per_job_drift": 0.2,
                    "profiles": [
                        {"label": "a", "arch": "riscv", "cpus": 4, "memory_gib": 8}
                    ],
                },
                id="unknown-arch",
            ),
            pytest.param(
                {
                    "min_headroom": 0.2,
                    "min_jobs": 2,
                    "per_job_drift": 0.2,
                    "profiles": [
                        {"label": "a", "arch": "amd64", "cpus": 4, "memory_gib": 0}
                    ],
                },
                id="zero-memory",
            ),
            pytest.param(
                {
                    "min_headroom": 0.2,
                    "min_jobs": 2,
                    "per_job_drift": 0.2,
                    "profiles": [
                        {"label": "a", "arch": "amd64", "cpus": 4, "memory_gib": 8},
                        "junk",
                    ],
                },
                id="one-bad-profile",
            ),
        ],
    )
    def test_malformed_block_warns_and_yields_none(
        self, raw: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        warnings: list[str] = []
        monkeypatch.setattr(runner_fit, "warn", warnings.append)
        assert parse_settings(raw) is None
        assert len(warnings) == 1
        assert "build.rust.runner_fit" in warnings[0]

    def test_a_partial_project_override_keeps_the_shipped_rest(self) -> None:
        config = CIConfig(_raw={"build": {"rust": {"runner_fit": {"min_jobs": 4}}}})
        raw = config.setting("build.rust.runner_fit")
        assert raw == {"min_jobs": 4}


@pytest.mark.parametrize(
    ("machine", "arch"),
    [
        ("x86_64", "amd64"),
        ("AMD64", "amd64"),
        ("aarch64", "arm64"),
        ("arm64", "arm64"),
        ("riscv64", "riscv64"),
    ],
)
def test_normalise_arch(machine: str, arch: str) -> None:
    assert runner_fit.normalise_arch(machine) == arch


ENV = {
    "GITHUB_REPOSITORY": "hyperi-io/dfe-loader",
    "GITHUB_RUN_ID": "123",
    "GITHUB_RUN_ATTEMPT": "2",
    "HYPERCI_RUNNER_LABEL": ARC,
    "CARGO_BUILD_JOBS": "2",
}
LIMIT = MemoryLimit(32 * GIB, "cgroup v2 /sys/fs/cgroup/memory.max")


def _record(
    phases: list[runner_fit.Phase],
    *,
    environ: dict[str, str] | None = None,
    exit_code: int = 0,
    limit: MemoryLimit | None = LIMIT,
    config: CIConfig | None = None,
) -> dict[str, Any]:
    return runner_fit.build_record(
        config or CIConfig(_raw={}),
        phases,
        total_seconds=900.04,
        exit_code=exit_code,
        optimization="pgo+bolt",
        environ=ENV if environ is None else environ,
        limit=limit,
        cpus=16,
        cpu_name="AMD EPYC",
        machine="x86_64",
    )


class TestRecord:
    def test_a_full_build(self) -> None:
        phases = [
            runner_fit.Phase("pgo-instrument", 300.0, 8 * GIB),
            runner_fit.Phase("release build", 590.0, 12 * GIB),
        ]
        got = _record(phases)
        assert got | {"verdict": None, "phases": None} == {
            "schema": 1,
            "repo": "hyperi-io/dfe-loader",
            "run_id": "123",
            "run_attempt": "2",
            "arch": "amd64",
            "runner_label": ARC,
            "cpu_model": "AMD EPYC",
            "cpu_budget": 16,
            "memory_limit_gib": 32.0,
            "memory_limit_source": "cgroup v2 /sys/fs/cgroup/memory.max",
            "cargo_jobs": 2,
            "memory_per_job_gib": 2.0,
            "optimization": "pgo+bolt",
            "exit_code": 0,
            "phases": None,
            "total_seconds": 900.0,
            "max_peak_gib": 12.0,
            "per_job_peak_gib": 6.0,
            "verdict": None,
        }
        assert got["phases"] == [
            {"name": "pgo-instrument", "seconds": 300.0, "peak_gib": 8.0},
            {"name": "release build", "seconds": 590.0, "peak_gib": 12.0},
        ]
        assert got["verdict"]["status"] == "fits"
        # 6 GiB a job x 2 jobs fits 8x16's 12.8 GiB with headroom, the cheaper set.
        assert got["verdict"]["recommended"] == "arc-native-8cpu"
        assert got["verdict"]["move"] is True
        json.dumps(got)

    def test_a_failed_build_keeps_the_phases_it_reached(self) -> None:
        got = _record(
            [runner_fit.Phase("pgo-instrument", 300.0, 20 * GIB)], exit_code=1
        )
        assert got["exit_code"] == 1
        assert [p["name"] for p in got["phases"]] == ["pgo-instrument"]
        assert got["max_peak_gib"] == 20.0

    def test_a_build_that_died_before_any_phase(self) -> None:
        got = _record([], exit_code=1)
        assert got["phases"] == []
        assert got["max_peak_gib"] is None
        assert got["per_job_peak_gib"] is None
        assert got["verdict"]["status"] == "unknown"
        assert "no memory peak" in got["verdict"]["reason"]

    def test_a_host_that_reports_no_peak_keeps_the_phase_lengths(self) -> None:
        got = _record([runner_fit.Phase("release build", 42.0, None)])
        assert got["phases"] == [
            {"name": "release build", "seconds": 42.0, "peak_gib": None}
        ]
        assert got["max_peak_gib"] is None

    def test_no_memory_limit(self) -> None:
        got = _record([runner_fit.Phase("release build", 1.0, GIB)], limit=None)
        assert got["memory_limit_gib"] is None
        assert got["memory_limit_source"] is None
        assert got["verdict"]["status"] == "unknown"

    @pytest.mark.parametrize(
        ("environ", "label", "jobs"),
        [
            pytest.param(ENV, ARC, 2, id="label-from-the-workflow"),
            pytest.param(
                {"RUNNER_NAME": "runner-7"}, "runner-7", None, id="runner-name-fallback"
            ),
            pytest.param({}, None, None, id="nothing-known"),
            pytest.param({"CARGO_BUILD_JOBS": "auto"}, None, None, id="bad-jobs"),
            pytest.param({"CARGO_BUILD_JOBS": "0"}, None, None, id="zero-jobs"),
        ],
    )
    def test_environment_fields(
        self, environ: dict[str, str], label: str | None, jobs: int | None
    ) -> None:
        got = _record(
            [runner_fit.Phase("release build", 1.0, 4 * GIB)], environ=environ
        )
        assert (got["runner_label"], got["cargo_jobs"]) == (label, jobs)
        assert (got["per_job_peak_gib"] is None) is (jobs is None)

    def test_unusable_runner_fit_block_gives_unknown_not_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runner_fit, "warn", lambda _msg: None)
        config = CIConfig(_raw={"build": {"rust": {"runner_fit": "off"}}})
        got = _record([runner_fit.Phase("release build", 1.0, 4 * GIB)], config=config)
        assert got["verdict"]["status"] == "unknown"

    def test_summary_markdown_has_a_phase_table_and_one_verdict_line(self) -> None:
        got = _record(
            [
                runner_fit.Phase("pgo-instrument", 300.0, None),
                runner_fit.Phase("release build", 590.0, 26 * GIB),
            ],
            environ={**ENV, "HYPERCI_RUNNER_LABEL": NS_AMD},
        )
        text = runner_fit.summary_markdown(got)
        assert "| pgo-instrument | 300 | n/a |" in text
        assert "| release build | 590 | 26.0 |" in text
        assert f"Move to {ARC}" not in text or "cheaper than" in text
        assert text.count("Runner fit (") == 1


class TestRecorder:
    def test_phase_seconds_run_from_the_previous_mark(self) -> None:
        runner_fit.begin(now=100.0)
        runner_fit.record_phase("a", GIB, now=130.0)
        runner_fit.record_phase("b", None, now=190.0)
        assert runner_fit._recorder is not None
        assert [(p.name, p.seconds) for p in runner_fit._recorder.phases] == [
            ("a", 30.0),
            ("b", 60.0),
        ]

    def test_begin_discards_earlier_marks(self) -> None:
        runner_fit.begin(now=0.0)
        runner_fit.record_phase("old", None, now=1.0)
        runner_fit.begin(now=5.0)
        assert runner_fit._recorder is not None
        assert runner_fit._recorder.phases == []

    def test_marking_outside_a_build_does_nothing(self) -> None:
        runner_fit._recorder = None
        runner_fit.record_phase("stray", GIB)
        assert runner_fit._recorder is None

    def test_log_peak_memory_marks_the_phase_with_and_without_a_peak(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = tmp_path / "proc-self-cgroup"
        proc.write_text("0::/job\n", encoding="utf-8", newline="\n")
        peak = tmp_path / "cg" / "job" / "memory.peak"
        peak.parent.mkdir(parents=True)
        peak.write_text(f"{3 * GIB}\n", encoding="utf-8", newline="\n")
        monkeypatch.setattr(_jobs, "info", lambda _msg: None)
        runner_fit.begin(now=0.0)
        _jobs.log_peak_memory("with-peak", root=tmp_path / "cg", proc_self_cgroup=proc)
        _jobs.log_peak_memory("no-peak", root=tmp_path / "none", proc_self_cgroup=proc)
        assert runner_fit._recorder is not None
        assert [(p.name, p.peak_bytes) for p in runner_fit._recorder.phases] == [
            ("with-peak", 3 * GIB),
            ("no-peak", None),
        ]


class TestFinish:
    def _run(
        self, tmp_path: Path, environ: dict[str, str], exit_code: int = 0
    ) -> dict[str, Any] | None:
        runner_fit.begin(now=0.0)
        runner_fit.record_phase("release build", 6 * GIB, now=60.0)
        return runner_fit.finish(
            CIConfig(_raw={}),
            exit_code,
            root=tmp_path,
            environ=environ,
            limit=LIMIT,
            cpus=16,
            now=90.0,
        )

    def test_writes_the_file_and_the_summary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        got = self._run(tmp_path, ENV)
        on_disk = json.loads((tmp_path / "runner-fit.json").read_text(encoding="utf-8"))
        assert got == on_disk
        assert on_disk["total_seconds"] == 90.0
        assert "### Runner fit" in summary.read_text(encoding="utf-8")

    def test_a_failed_build_still_writes(self, tmp_path: Path) -> None:
        got = self._run(tmp_path, ENV, exit_code=1)
        assert got is not None
        assert got["exit_code"] == 1
        assert (tmp_path / "runner-fit.json").is_file()

    def test_writes_nothing_unless_the_workflow_named_the_runner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        assert self._run(tmp_path, {"CARGO_BUILD_JOBS": "2"}) is None
        assert not (tmp_path / "runner-fit.json").exists()
        assert not summary.exists()

    def test_a_build_that_never_began_writes_nothing(self, tmp_path: Path) -> None:
        runner_fit._recorder = None
        assert (
            runner_fit.finish(CIConfig(_raw={}), 0, root=tmp_path, environ=ENV) is None
        )

    def test_an_unwritable_root_warns_and_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        warnings: list[str] = []
        monkeypatch.setattr(runner_fit, "warn", warnings.append)
        assert self._run(tmp_path / "missing", ENV) is None
        assert len(warnings) == 1
        assert "runner-fit.json" in warnings[0]

    def test_finish_ends_the_recording(self, tmp_path: Path) -> None:
        self._run(tmp_path, ENV)
        assert runner_fit._recorder is None


class TestBuildRun:
    @pytest.mark.parametrize("rc", [0, 1])
    def test_run_writes_a_record_whatever_the_build_returns(
        self, rc: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HYPERCI_RUNNER_LABEL", ARC)
        monkeypatch.setattr(build, "_run_build", lambda _config, _extra: rc)
        assert build.run(CIConfig(_raw={})) == rc
        written = json.loads((tmp_path / "runner-fit.json").read_text(encoding="utf-8"))
        assert written["exit_code"] == rc

    def test_run_records_when_the_build_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def explode(_config: CIConfig, _extra: object) -> int:
            raise RuntimeError("cargo vanished")

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HYPERCI_RUNNER_LABEL", ARC)
        monkeypatch.setattr(build, "_run_build", explode)
        with pytest.raises(RuntimeError, match="cargo vanished"):
            build.run(CIConfig(_raw={}))
        written = json.loads((tmp_path / "runner-fit.json").read_text(encoding="utf-8"))
        assert written["exit_code"] == 1


def _rust_ci() -> dict[str, Any]:
    path = REPO_ROOT / ".github" / "workflows" / "rust-ci.yml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class TestWorkflow:
    def test_run_build_names_its_runner(self) -> None:
        steps = _rust_ci()["jobs"]["build"]["steps"]
        run_build = next(s for s in steps if s.get("name") == "Run build")
        assert run_build["env"]["HYPERCI_RUNNER_LABEL"] == "${{ matrix.runner }}"

    def test_the_record_is_its_own_artifact_outside_dist(self) -> None:
        steps = _rust_ci()["jobs"]["build"]["steps"]
        names = [s.get("name") for s in steps]
        step = steps[names.index("Upload runner-fit record")]
        assert names.index("Upload runner-fit record") > names.index(
            "Upload build artifacts"
        )
        assert step["if"] == "always()"
        assert step["with"]["name"] == "runner-fit-${{ matrix.os_arch }}"
        assert step["with"]["path"] == "runner-fit.json"
        assert step["with"]["if-no-files-found"] == "ignore"
        assert step["with"]["retention-days"] == 30
        shipped = next(s for s in steps if s.get("name") == "Upload build artifacts")
        assert "runner-fit" not in shipped["with"]["path"]
        assert step["uses"].split("@")[0] == shipped["uses"].split("@")[0]


def _api(
    runs: dict[str, list[int]], with_records: set[int], *, fail: str | None = None
) -> runner_report.Api:
    """Serve ``actions/runs`` and ``artifacts`` responses from plain data."""

    def call(args: list[str]) -> ApiResult:
        endpoint = args[0]
        if fail and fail in endpoint:
            return ApiResult(None, f"gh api {endpoint} failed: HTTP 404", "HTTP 404")
        repo = endpoint.split("/actions/")[0].removeprefix("repos/")
        if "/artifacts" in endpoint:
            run = int(endpoint.split("/runs/")[1].split("/")[0])
            names = ["runner-fit-linux-amd64"] if run in with_records else ["other"]
            return ApiResult({"artifacts": [{"name": n} for n in names]}, "", None)
        return ApiResult({"workflow_runs": [{"id": i} for i in runs[repo]]}, "", None)

    return call


def _downloader(per_run: dict[int, list[dict[str, Any]]]) -> runner_report.Download:
    def fetch(_repo: str, run_id: int, dest: Path) -> None:
        for number, record in enumerate(per_run.get(run_id, [])):
            target = dest / f"runner-fit-{number}" / "runner-fit.json"
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps(record), encoding="utf-8", newline="\n")

    return fetch


AMD = _record([runner_fit.Phase("release build", 600.0, 26 * GIB)])
ARM = {**AMD, "arch": "arm64", "runner_label": NS_ARM_16}


class TestReport:
    def test_takes_the_newest_run_with_records_and_one_row_per_arch(self) -> None:
        text, rc = runner_report.report(
            ["dfe-loader"],
            api=_api({"hyperi-io/dfe-loader": [30, 20, 10]}, {20, 10}),
            download=_downloader({20: [ARM, AMD], 10: [AMD]}),
        )
        lines = text.splitlines()
        assert rc == 0
        assert lines[0].split()[:3] == ["repo", "arch", "runner"]
        assert [line.split()[:2] for line in lines[1:3]] == [
            ["hyperi-io/dfe-loader", "amd64"],
            ["hyperi-io/dfe-loader", "arm64"],
        ]

    def test_a_repo_without_a_record_gets_a_row_not_an_error(self) -> None:
        text, rc = runner_report.report(
            ["hyperi-io/dfe-fetcher"],
            api=_api({"hyperi-io/dfe-fetcher": [3, 2]}, set()),
            download=_downloader({}),
        )
        assert rc == 0
        assert "no runner-fit record in the last 20 runs" in text

    def test_gh_failure_exits_non_zero_and_the_other_repos_still_report(self) -> None:
        text, rc = runner_report.report(
            ["hyperi-io/gone", "hyperi-io/dfe-loader"],
            api=_api({"hyperi-io/dfe-loader": [5]}, {5}, fail="/gone/"),
            download=_downloader({5: [AMD]}),
        )
        assert rc == 1
        assert "hyperi-io/dfe-loader" in text
        assert "hyperi-io/gone" not in text

    def test_run_option_reads_that_run_only(self) -> None:
        text, rc = runner_report.report(
            ["hyperi-io/dfe-loader"],
            run_id=7,
            api=_api({"hyperi-io/dfe-loader": [9, 7]}, {9}),
            download=_downloader({9: [AMD]}),
        )
        assert rc == 0
        assert "no runner-fit record in run 7" in text

    def test_json_carries_the_records_with_repo_and_run(self) -> None:
        text, rc = runner_report.report(
            ["dfe-loader"],
            as_json=True,
            api=_api({"hyperi-io/dfe-loader": [4]}, {4}),
            download=_downloader({4: [AMD]}),
        )
        data = json.loads(text)
        assert rc == 0
        assert (data[0]["repo"], data[0]["run"], data[0]["arch"]) == (
            "hyperi-io/dfe-loader",
            4,
            "amd64",
        )

    def test_a_row_that_should_move_prints_its_reason(self) -> None:
        big = _record(
            [runner_fit.Phase("release build", 600.0, 26 * GIB)],
            limit=MemoryLimit(64 * GIB, "cgroup v2 memory.max"),
        )
        text, _rc = runner_report.report(
            ["dfe-loader"],
            api=_api({"hyperi-io/dfe-loader": [4]}, {4}),
            download=_downloader({4: [big]}),
        )
        assert big["verdict"]["move_reason"] in text
        assert text.splitlines()[1].split()[-1] == "yes"

    def test_an_unreadable_record_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runner_report, "warn", lambda _msg: None)
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "runner-fit.json").write_text("{not json", encoding="utf-8")
        (tmp_path / "b").mkdir()
        (tmp_path / "b" / "runner-fit.json").write_text("[1]", encoding="utf-8")
        assert runner_report.read_records(tmp_path) == []

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            pytest.param({"artifacts": [{"name": "runner-fit-x"}]}, True, id="live"),
            pytest.param(
                {"artifacts": [{"name": "runner-fit-x", "expired": True}]},
                False,
                id="expired",
            ),
            pytest.param({"artifacts": [{"name": "build-dist-x"}]}, False, id="other"),
            pytest.param({"artifacts": []}, False, id="none"),
            pytest.param(None, False, id="no-payload"),
            pytest.param("junk", False, id="junk"),
        ],
    )
    def test_has_records(self, payload: object, expected: bool) -> None:
        assert runner_report.has_records(payload) is expected

    @pytest.mark.parametrize(
        ("name", "full"),
        [("dfe-loader", "hyperi-io/dfe-loader"), ("acme/tool", "acme/tool")],
    )
    def test_qualify(self, name: str, full: str) -> None:
        assert runner_report.qualify(name) == full
