# Project:   HyperI CI
# File:      tests/unit/test_release_split.py
# Purpose:   The job that holds publish credentials runs none of the repo's code
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""The release tail's prepare/publish split (issue #409), read off the YAML.

Tag & Release holds the crates.io, npm, PyPI and R2 credentials, the release
App key and a token that writes to the repo. Code the repo controls -- a build
script under cargo-semver-checks, an npm lifecycle script,
``release.stamp_cmd`` -- could read every one of them, and a step in the same
job can leave a process or a ``$GITHUB_ENV`` line behind for a later step. So
that code runs in ``prepare``, which holds no secret.

Both jobs are held to ALLOWLISTS of the actions they use and the shell lines
they run, because a denylist of known repo-code commands misses the next one.
Adding a step means adding its shape here, on purpose.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from hyperi_ci.stamp import SKIP_STAMP_CMD_ENV

WORKFLOW = (
    Path(__file__).parent.parent.parent / ".github" / "workflows" / "_release-tail.yml"
)
PUBLISH_CREDENTIALS = (
    "CARGO_REGISTRY_TOKEN",
    "NPM_TOKEN",
    "PYPI_TOKEN",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "GH_APP_PRIVATE_KEY",
)

_INSTALL = r"\$\{\{ env\.HYPERCI_INSTALL \}\}"
_VERSION = r"\$RELEASE_VERSION"
_BRACED_VERSION = r"\$\{RELEASE_VERSION\}"
_RUN_URL = (
    r'--run-url "\$\{\{ github\.server_url \}\}/\$\{\{ github\.repository \}\}'
    r'/actions/runs/\$\{\{ github\.run_id \}\}"'
)
# The placement of dist/ and ci-tmp/ out of the downloaded build artefact.
_PLACE = [
    r"for tree in dist ci-tmp; do",
    r'if \[ -d "\$RUNNER_TEMP/build-dist/\$tree" \]; then '
    r'cp -RL "\$RUNNER_TEMP/build-dist/\$tree" \.; fi',
    r"done",
]

ALLOWED_USES = {
    "tag-and-release": (
        "astral-sh/setup-uv@",
        "dtolnay/rust-toolchain@",
        "actions/checkout@",
        "actions/download-artifact@",
        "actions/create-github-app-token@",
        "actions/setup-node@",
        "hyperi-io/hyperi-ci/.github/actions/setup-semantic-release@main",
    ),
    "prepare": (
        "astral-sh/setup-uv@",
        "actions/checkout@",
        "actions/download-artifact@",
        "actions/upload-artifact@",
        "dtolnay/rust-toolchain@",
        "hyperi-io/hyperi-ci/.github/actions/setup-rust-tools@main",
        "actions/setup-node@",
    ),
    "prepare-failed": ("astral-sh/setup-uv@",),
}

ALLOWED_RUN_LINES = {
    "tag-and-release": [
        *_PLACE,
        r'uv python install --no-config "\$PYTHON_VERSION"',
        r'rm -rf "\$RUNNER_TEMP/release-prepared/stamped"',
        rf"{_INSTALL} release-verify",
        r'echo "has=\$\{\{ secrets\.GH_APP_PRIVATE_KEY != \'\' \}\}" >> "\$GITHUB_OUTPUT"',
        r'if \[ "\$\{\{ steps\.bot\.outcome \}\}" = "success" \]; then',
        r'echo "Tagging and pushing as hypersec-ci-bot\."',
        r"else",
        r"fi",
        r'echo "::warning::GH_APP_PRIVATE_KEY is not visible to this repo [^"$`;|&]*"',
        rf'predicted="v{_BRACED_VERSION}"',
        r'if git rev-parse -q --verify "refs/tags/\$\{predicted\}\^\{commit\}" '
        r">/dev/null 2>&1",
        r"then",
        r'existing=\$\(git rev-parse "refs/tags/\$\{predicted\}\^\{commit\}"\)',
        r'head=\$\(git rev-parse "HEAD\^\{commit\}"\)',
        r'if \[ "\$existing" != "\$head" \]',
        r'echo "::error::Predicted \$\{predicted\} already exists [^"`;|&]*"',
        r"exit 1",
        r'echo "\$\{predicted\} already exists at HEAD [^"$`;|&]*"',
        r"if \[ -z \"\$\(git tag --list 'v\[0-9\]\*'\)\" \]; then",
        rf'echo "Tag-less repo [^"$`;|&]*v{_BRACED_VERSION} from the plan\'s prediction"',
        rf'{_INSTALL} tag-head --bump "{_VERSION}"',
        # Known gap, tracked in #413: the tagger loads a repo-controlled config.
        r"npx semantic-release",
        rf"{_INSTALL} run release",
        rf'{_INSTALL} release-commit --branch "\$RELEASE_BRANCH" "{_VERSION}"',
        rf'{_INSTALL} release-notify "{_VERSION}" --outcome success',
        rf'{_INSTALL} release-notify "{_VERSION}" --outcome failure {_RUN_URL}',
        rf'{_INSTALL} release-notify "{_VERSION}" --outcome commit-back-failed '
        rf"{_RUN_URL}",
    ],
    "prepare": [
        *_PLACE,
        r'uv python install "\$PYTHON_VERSION"',
        rf'{_INSTALL} release-prepare "\$RELEASE_VERSION" --out "\$STAMPED_DIR" '
        r"--phase stamp",
        rf'{_INSTALL} release-prepare "\$RELEASE_VERSION" --out "\$PREPARED_DIR" '
        r"--phase package",
    ],
    "prepare-failed": [
        r'uv python install --no-config "\$PYTHON_VERSION"',
        rf'{_INSTALL} release-notify "{_VERSION}" --outcome failure {_RUN_URL}',
    ],
}


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _jobs() -> dict[str, Any]:
    return _workflow()["jobs"]


def _uses_outside(job: dict[str, Any], allowed: tuple[str, ...]) -> list[str]:
    return [
        str(step["uses"])
        for step in job["steps"]
        if "uses" in step and not str(step["uses"]).startswith(allowed)
    ]


def _run_lines_outside(job: dict[str, Any], allowed: list[str]) -> list[str]:
    patterns = [re.compile(rf"{p}\Z") for p in allowed]
    return [
        line.strip()
        for step in job["steps"]
        for line in str(step.get("run", "")).splitlines()
        if line.strip() and not any(p.match(line.strip()) for p in patterns)
    ]


def test_the_workflow_env_carries_no_secret() -> None:
    """Workflow-level env reaches every job, prepare included."""
    env = yaml.safe_dump(_workflow().get("env", {}))
    assert "secrets." not in env
    assert "github.token" not in env


@pytest.mark.parametrize("job_id", sorted(ALLOWED_USES))
def test_only_allowlisted_actions_run(job_id: str) -> None:
    """A local `./` action is repo code; an unknown one is unreviewed."""
    stray = _uses_outside(_jobs()[job_id], ALLOWED_USES[job_id])
    assert not stray, f"{job_id} uses actions outside its allowlist: {stray}"


@pytest.mark.parametrize("job_id", sorted(ALLOWED_RUN_LINES))
def test_only_allowlisted_shell_lines_run(job_id: str) -> None:
    stray = _run_lines_outside(_jobs()[job_id], ALLOWED_RUN_LINES[job_id])
    assert not stray, f"{job_id} runs lines outside its allowlist: {stray}"


@pytest.mark.parametrize("job_id", sorted(ALLOWED_RUN_LINES))
def test_no_step_changes_its_shell_or_directory(job_id: str) -> None:
    """A `shell:` swaps the interpreter the allowlisted lines run under."""
    for step in _jobs()[job_id]["steps"]:
        assert "shell" not in step, step.get("name")
        assert "working-directory" not in step, step.get("name")


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

    def test_the_stamp_outputs_leave_before_the_packaging_runs(
        self, job: dict[str, Any]
    ) -> None:
        names = [s.get("name") for s in job["steps"]]
        assert (
            names.index("Stamp the release")
            < names.index("Upload the stamp outputs")
            < names.index("Check and package the release")
        )


class TestPrepareFailureIsRecorded:
    def test_a_failed_prepare_opens_the_issue_without_repo_code(self) -> None:
        job = _jobs()["prepare-failed"]
        assert job["needs"] == ["prepare"]
        assert "needs.prepare.result == 'failure'" in job["if"]
        assert job["permissions"] == {"contents": "read", "issues": "write"}
        assert not any("actions/checkout" in str(s.get("uses")) for s in job["steps"])


class TestPublishRunsNoRepoCode:
    @pytest.fixture
    def job(self) -> dict[str, Any]:
        return _jobs()["tag-and-release"]

    def test_it_waits_for_a_successful_prepare(self, job: dict[str, Any]) -> None:
        assert "prepare" in job["needs"]
        assert "needs.prepare.result == 'success'" in job["if"]

    def test_the_app_key_reaches_only_the_mint_step(self, job: dict[str, Any]) -> None:
        assert "GH_APP_PRIVATE_KEY" not in yaml.safe_dump(job.get("env", {}))
        holders = [
            step.get("name")
            for step in job["steps"]
            if "secrets.GH_APP_PRIVATE_KEY" in yaml.safe_dump(step)
        ]
        assert holders == [
            "Check for the release App key",
            "Mint the release bot token",
        ]
        mint = next(s for s in job["steps"] if s.get("id") == "bot")
        assert mint["with"]["private-key"] == "${{ secrets.GH_APP_PRIVATE_KEY }}"

    def test_the_prepared_release_is_verified_before_any_tag(
        self, job: dict[str, Any]
    ) -> None:
        names = [s.get("name") for s in job["steps"]]
        verify = names.index("Verify the prepared release")
        for before in (
            "Download build artifacts",
            "Download the prepared release",
            "Download the stamp outputs",
        ):
            assert names.index(before) < verify
        for after in (
            "Guard -- predicted tag must not already exist off-HEAD",
            "Tag (semantic-release)",
            "Tag HEAD (forced bump / explicit version)",
        ):
            assert verify < names.index(after)

    def test_only_the_stamp_artefact_fills_stamped(self, job: dict[str, Any]) -> None:
        """The package phase ran repo code and could plant stamped/<name>."""
        steps = job["steps"]
        names = [s.get("name") for s in steps]
        prepared = names.index("Download the prepared release")
        stamped = names.index("Download the stamp outputs")
        drops = [
            i
            for i, s in enumerate(steps)
            if str(s.get("run", "")).strip()
            == 'rm -rf "$RUNNER_TEMP/release-prepared/stamped"'
        ]
        assert drops, "nothing clears stamped/ between the two downloads"
        assert prepared < drops[0] < stamped
        assert steps[stamped]["with"]["path"] == (
            "${{ runner.temp }}/release-prepared/stamped"
        )
        assert "continue-on-error" not in steps[drops[0]]

    def test_build_artefacts_never_land_on_the_checkout(
        self, job: dict[str, Any]
    ) -> None:
        for step in job["steps"]:
            if "actions/download-artifact" in str(step.get("uses")):
                assert "runner.temp" in step["with"]["path"], step["name"]

    def test_the_upload_and_the_commit_read_the_prepared_directory(
        self, job: dict[str, Any]
    ) -> None:
        for step in job["steps"]:
            run = str(step.get("run", ""))
            if "run release" in run or "release-commit" in run:
                assert "HYPERCI_RELEASE_PREPARED" in step.get("env", {}), step["name"]

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

    def test_typescript_gets_node_before_any_checkout(
        self, job: dict[str, Any]
    ) -> None:
        """`npm publish` needs Node on every path, not only where the tagger runs.

        setup-semantic-release is the only other Node, and it is skipped on a
        tag dispatch and a forced bump, where the publish runner has none.
        """
        steps = job["steps"]
        node = [
            i
            for i, s in enumerate(steps)
            if str(s.get("uses", "")).startswith("actions/setup-node@")
        ]
        checkouts = [
            i
            for i, s in enumerate(steps)
            if str(s.get("uses", "")).startswith("actions/checkout@")
        ]
        assert len(node) == 1
        assert node[0] < min(checkouts)
        step = steps[node[0]]
        assert step["if"] == "inputs.language == 'typescript'"
        assert step["with"] == {"node-version": "${{ inputs.node-version }}"}
        prepare_node = next(
            s["uses"]
            for s in _jobs()["prepare"]["steps"]
            if str(s.get("uses", "")).startswith("actions/setup-node@")
        )
        assert step["uses"] == prepare_node

    def test_there_is_no_registry_login(self, job: dict[str, Any]) -> None:
        """Nothing in the upload uses docker, so no step writes a docker config."""
        assert not any("login-action" in str(s.get("uses")) for s in job["steps"])


class TestCommitBackFailureIsRecorded:
    """A refused commit-back leaves the job green, so it must open an issue."""

    @pytest.fixture
    def steps(self) -> list[dict[str, Any]]:
        return _jobs()["tag-and-release"]["steps"]

    def test_the_commit_back_stays_non_fatal(self, steps: list[dict[str, Any]]) -> None:
        commit = next(s for s in steps if s.get("id") == "releasecommit")
        assert "release-commit" in str(commit["run"])
        assert commit["continue-on-error"] is True

    def test_its_failure_opens_the_commit_back_issue(
        self, steps: list[dict[str, Any]]
    ) -> None:
        names = [s.get("name") for s in steps]
        record = steps[names.index("Record a failed commit-back")]
        assert record["if"] == "steps.releasecommit.outcome == 'failure'"
        assert "--outcome commit-back-failed" in str(record["run"])
        assert record["continue-on-error"] is True
        assert record["env"] == {
            "GH_TOKEN": "${{ secrets.GITHUB_TOKEN }}",
            "RELEASE_VERSION": "${{ inputs.next-version }}",
        }

    def test_it_runs_after_the_commit_back(self, steps: list[dict[str, Any]]) -> None:
        commit = next(i for i, s in enumerate(steps) if s.get("id") == "releasecommit")
        record = next(
            i
            for i, s in enumerate(steps)
            if "--outcome commit-back-failed" in str(s.get("run", ""))
        )
        assert commit < record

    def test_the_job_token_can_open_issues(self) -> None:
        assert _jobs()["tag-and-release"]["permissions"]["issues"] == "write"


class TestContainerRunsNoRepoCode:
    """The Container job logs in to Docker Hub and GHCR with a packages:write token.

    These tests hold the workflow side: the stamp passes the switch that skips
    ``release.stamp_cmd``, the checkout keeps no token, uv and Python come up
    without reading the repo's uv config, and the build artefact places only
    ``dist/`` and ``ci-tmp/``. The skip itself is the CLI's (``test_stamp.py``)
    and only takes effect on a CLI release that carries it. Running the stamp
    before the logins is ordering, not isolation: a ``stamp_cmd`` that does run
    can still leave a ``$GITHUB_ENV`` line or a process for a later step.
    """

    @pytest.fixture
    def steps(self) -> list[dict[str, Any]]:
        return _jobs()["container"]["steps"]

    @staticmethod
    def _index(steps: list[dict[str, Any]], prefix: str) -> list[int]:
        return [
            i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith(prefix)
        ]

    def test_uv_is_installed_before_the_checkout(
        self, steps: list[dict[str, Any]]
    ) -> None:
        """setup-uv reads a checked-out repo's `required-version`."""
        uv = self._index(steps, "astral-sh/setup-uv@")
        checkout = self._index(steps, "actions/checkout@")
        assert len(uv) == 1 and len(checkout) == 1
        assert uv[0] < checkout[0]

    def test_python_is_installed_without_the_repos_uv_config(
        self, steps: list[dict[str, Any]]
    ) -> None:
        """`[tool.uv]` can pick where the interpreter that runs hyperi-ci comes from."""
        python = next(s for s in steps if s.get("name") == "Set up Python")
        assert python["run"] == 'uv python install --no-config "$PYTHON_VERSION"'

    def test_build_artefacts_never_land_on_the_checkout(
        self, steps: list[dict[str, Any]]
    ) -> None:
        downloads = self._index(steps, "actions/download-artifact@")
        assert downloads
        for i in downloads:
            assert "runner.temp" in steps[i]["with"]["path"], steps[i]["name"]
        place = [
            i for i, s in enumerate(steps) if s.get("name") == "Place dist/ and ci-tmp/"
        ]
        assert len(place) == 1
        assert not _run_lines_outside({"steps": [steps[place[0]]]}, _PLACE)
        names = [s.get("name") for s in steps]
        assert max(downloads) < place[0] < names.index("Build container")

    @staticmethod
    def _stamp(steps: list[dict[str, Any]]) -> tuple[int, dict[str, Any]]:
        names = [s.get("name") for s in steps]
        at = names.index("Stamp predicted version")
        return at, steps[at]

    def test_the_stamp_skips_the_repo_command(
        self, steps: list[dict[str, Any]]
    ) -> None:
        _, stamp = self._stamp(steps)
        assert stamp["env"][SKIP_STAMP_CMD_ENV] == "1"
        assert (
            stamp["run"]
            == '${{ env.HYPERCI_INSTALL }} stamp-version "$RELEASE_VERSION"'
        )

    def test_the_stamp_runs_before_every_login(
        self, steps: list[dict[str, Any]]
    ) -> None:
        at, _ = self._stamp(steps)
        logins = [
            i
            for i, s in enumerate(steps)
            if str(s.get("uses", "")).startswith("docker/login-action@")
        ]
        assert len(logins) == 2
        assert at < min(logins)

    def test_its_checkout_keeps_no_credential(
        self, steps: list[dict[str, Any]]
    ) -> None:
        checkouts = [
            s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@")
        ]
        assert len(checkouts) == 1
        assert checkouts[0]["with"]["persist-credentials"] is False

    def test_the_job_token_reaches_only_ghcr_and_the_submodules(
        self, steps: list[dict[str, Any]]
    ) -> None:
        holders = [
            s.get("name")
            for s in steps
            if "secrets.GITHUB_TOKEN" in yaml.safe_dump(s)
            or "github.token" in yaml.safe_dump(s)
        ]
        assert holders == ["Init submodules", "GHCR login"]


def test_only_the_publish_job_holds_a_publish_credential() -> None:
    holders = sorted(
        job_id
        for job_id, job in _jobs().items()
        if any(name in yaml.safe_dump(job) for name in PUBLISH_CREDENTIALS)
    )
    assert holders == ["tag-and-release"]
