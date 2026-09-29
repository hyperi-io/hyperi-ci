# Project:   HyperI CI
# File:      tests/unit/test_release_split.py
# Purpose:   The job that holds publish credentials runs none of the repo's code
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""The release tail's prepare/publish split (issue #409), read off the YAML.

Tag & Release holds the crates.io, npm, PyPI and R2 credentials and a token
that writes to the repo. Code the repo controls -- a build script under
cargo-semver-checks, an npm lifecycle script, ``release.stamp_cmd`` -- could
read every one of them, and a step in the same job can leave a process or a
``$GITHUB_ENV`` line behind for a later step. So that code runs in ``prepare``,
which holds no secret, and these tests hold both halves to it.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW = (
    Path(__file__).parent.parent.parent / ".github" / "workflows" / "_release-tail.yml"
)
PUBLISH_CREDENTIALS = (
    "CARGO_REGISTRY_TOKEN",
    "NPM_TOKEN",
    "PYPI_TOKEN",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
)

# The hyperi-ci subcommands the publish job may run: none of them builds,
# packs, stamps or runs a repo command.
UPLOAD_ONLY_SUBCOMMANDS = {
    "tag-head",
    "run release",
    "release-commit",
    "release-notify",
}

# Commands that execute repo code, or a toolchain the repo's files select.
REPO_CODE = re.compile(
    r"\b(release-prepare|stamp-version|run (build|test|quality|container)"
    r"|npm (pack|run|ci|install|publish)|cargo |uv run|make |go (build|list|run))\b"
)
REPO_CODE_ACTIONS = ("setup-rust-tools", "actions/setup-node")

# Known gap, tracked: the tagger loads a repo-controlled semantic-release config.
TAGGER_EXCEPTION = ("Tag (semantic-release)", "npx semantic-release", "#413")


def _jobs() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _hyperi_ci_calls(run: str) -> list[str]:
    return re.findall(r"HYPERCI_INSTALL \}\}\s+([a-z-]+(?: [a-z]+)?)", run)


class TestPrepareHoldsNoSecret:
    @pytest.fixture
    def job(self) -> dict[str, Any]:
        return _jobs()["prepare"]

    def test_its_token_is_read_only(self, job: dict[str, Any]) -> None:
        assert job["permissions"] == {"contents": "read"}

    def test_it_reads_no_secret_and_no_token(self, job: dict[str, Any]) -> None:
        text = yaml.safe_dump(job)
        assert "secrets." not in text
        assert "github.token" not in text
        for name in ("GITHUB_TOKEN", "GH_TOKEN", *PUBLISH_CREDENTIALS):
            assert name not in text

    def test_its_checkout_keeps_no_credential(self, job: dict[str, Any]) -> None:
        checkouts = [
            s
            for s in job["steps"]
            if str(s.get("uses", "")).startswith("actions/checkout@")
        ]
        assert len(checkouts) == 1
        assert checkouts[0]["with"]["persist-credentials"] is False

    def test_it_is_where_the_repo_code_runs(self, job: dict[str, Any]) -> None:
        runs = " ".join(str(s.get("run", "")) for s in job["steps"])
        assert "release-prepare" in runs
        uses = " ".join(str(s.get("uses", "")) for s in job["steps"])
        assert "setup-rust-tools" in uses


class TestPublishRunsNoRepoCode:
    @pytest.fixture
    def job(self) -> dict[str, Any]:
        return _jobs()["tag-and-release"]

    def test_it_waits_for_a_successful_prepare(self, job: dict[str, Any]) -> None:
        assert "prepare" in job["needs"]
        assert "needs.prepare.result == 'success'" in job["if"]

    def test_no_step_runs_repo_code(self, job: dict[str, Any]) -> None:
        offenders = []
        for step in job["steps"]:
            name = step.get("name", step.get("uses", ""))
            run = str(step.get("run", ""))
            if name == TAGGER_EXCEPTION[0]:
                run = run.replace(TAGGER_EXCEPTION[1], "")
            if REPO_CODE.search(run):
                offenders.append(name)
            if any(action in str(step.get("uses", "")) for action in REPO_CODE_ACTIONS):
                offenders.append(name)
        assert not offenders, f"Tag & Release steps that run repo code: {offenders}"

    def test_it_calls_only_upload_subcommands(self, job: dict[str, Any]) -> None:
        called = {
            call
            for step in job["steps"]
            for call in _hyperi_ci_calls(str(step.get("run", "")))
        }
        assert called, "the pattern no longer finds the hyperi-ci calls"
        assert called <= UPLOAD_ONLY_SUBCOMMANDS, called - UPLOAD_ONLY_SUBCOMMANDS

    def test_the_upload_and_the_commit_read_the_prepared_directory(
        self, job: dict[str, Any]
    ) -> None:
        for step in job["steps"]:
            run = str(step.get("run", ""))
            if "run release" in run or "release-commit" in run:
                assert "HYPERCI_RELEASE_PREPARED" in step.get("env", {}), step["name"]

    def test_uv_reads_no_repo_config(self, job: dict[str, Any]) -> None:
        """A repo's python-install-mirror picks the interpreter hyperi-ci runs on."""
        for step in job["steps"]:
            for line in str(step.get("run", "")).splitlines():
                if re.search(r"\buv (python|pip|sync|publish)\b", line):
                    assert "--no-config" in line, step["name"]

    def test_rustup_runs_before_the_checkout(self, job: dict[str, Any]) -> None:
        """rustup reads the working directory's rust-toolchain.toml."""
        steps = job["steps"]
        rust = next(
            i for i, s in enumerate(steps) if "rust-toolchain" in str(s.get("uses"))
        )
        checkout = next(
            i for i, s in enumerate(steps) if "actions/checkout" in str(s.get("uses"))
        )
        assert rust < checkout

    def test_the_tagger_exception_is_still_the_only_one(
        self, job: dict[str, Any]
    ) -> None:
        """Drop TAGGER_EXCEPTION when #413 lands, and this test goes with it."""
        tagger = [s for s in job["steps"] if s.get("name") == TAGGER_EXCEPTION[0]]
        assert len(tagger) == 1
        assert TAGGER_EXCEPTION[1] in tagger[0]["run"]


def test_only_the_publish_job_holds_a_publish_credential() -> None:
    holders = sorted(
        job_id
        for job_id, job in _jobs().items()
        if any(name in yaml.safe_dump(job) for name in PUBLISH_CREDENTIALS)
    )
    assert holders == ["tag-and-release"]
