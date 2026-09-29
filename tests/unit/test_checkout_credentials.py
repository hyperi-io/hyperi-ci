# Project:   HyperI CI
# File:      tests/unit/test_checkout_credentials.py
# Purpose:   Keep the job token out of the git config of jobs that run repo code
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""`persist-credentials` on every checkout, and the steps that still need git auth.

actions/checkout persists the job token by default: v7 writes the
`http.https://github.com/.extraheader` credential to a file under
`$RUNNER_TEMP` and includes it from the workspace `.git/config`, so any code a
later step runs can read it back with `git config` (issue #402). A job that
runs the repo's own code -- linters that execute build scripts, tests, a build
-- checks out with `persist-credentials: false`, and the one step that needs
to reach GitHub over git gets the token as environment-scoped git config.
"""

import base64
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
LANGUAGE_WORKFLOWS = ("rust-ci.yml", "python-ci.yml", "ts-ci.yml", "go-ci.yml")
SCANNED = (*LANGUAGE_WORKFLOWS, "_release-tail.yml")
JOB_TOKEN = "${{ github.token }}"
FAKE_TOKEN = "fake-job-token"
HEADER_KEY = "http.https://github.com/.extraheader"

# The step-scoped credential, shared with rust-ci.yml's sibling clone. It
# appends to GIT_CONFIG_COUNT rather than resetting it, so config the runner
# already set that way survives.
AUTH_PREAMBLE = """\
basic="$(printf 'x-access-token:%s' "$GH_TOKEN" | base64 | tr -d '\\n')"
echo "::add-mask::$basic"
n="${GIT_CONFIG_COUNT:-0}"
export GIT_CONFIG_COUNT=$((n + 1))
export "GIT_CONFIG_KEY_$n=http.https://github.com/.extraheader"
export "GIT_CONFIG_VALUE_$n=AUTHORIZATION: basic $basic"
"""

# Checkouts that keep the persisted credential, and why that is safe.
PERSISTS: dict[tuple[str, str], str] = {
    **{
        (name, "plan"): (
            "runs no repo build or test code; semantic-release there already "
            "holds the token in its own environment"
        )
        for name in LANGUAGE_WORKFLOWS
    },
    ("_release-tail.yml", "container"): (
        "release path, left as is: its only repo-controlled code is the "
        "Dockerfile, whose build context cannot reach $RUNNER_TEMP"
    ),
}

# The steps that reach GitHub over git after a credential-free checkout.
AUTHED_STEPS = {
    "quality": "Deepen history for secret scan",
    "test": "Init submodules",
}
_GIT_NETWORK = ("git fetch", "git submodule update", "git clone", "git pull")


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOW_DIR / name).read_text(encoding="utf-8"))


def _checkouts(name: str) -> list[tuple[str, dict]]:
    return [
        (job_id, step)
        for job_id, job in _load(name)["jobs"].items()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]


def _persists(step: dict) -> bool:
    # The action's own default is true.
    return (step.get("with") or {}).get("persist-credentials", True) is not False


@pytest.mark.parametrize("workflow_name", SCANNED)
def test_every_checkout_drops_its_credential_unless_listed(workflow_name: str) -> None:
    kept = [
        job_id
        for job_id, step in _checkouts(workflow_name)
        if _persists(step) and (workflow_name, job_id) not in PERSISTS
    ]
    assert not kept, (
        f"{workflow_name}: {kept} leave the job token in the workspace git "
        "config; set persist-credentials: false or list the job with a reason"
    )


def test_every_listed_exception_still_persists() -> None:
    stale = [
        key
        for key in PERSISTS
        if not any(
            job_id == key[1] and _persists(step) for job_id, step in _checkouts(key[0])
        )
    ]
    assert not stale, f"no longer persisting, drop from PERSISTS: {stale}"


@pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
@pytest.mark.parametrize("job_id", ["commit-check", "quality", "test", "build"])
def test_jobs_that_run_repo_code_drop_it(workflow_name: str, job_id: str) -> None:
    checkouts = [s for j, s in _checkouts(workflow_name) if j == job_id]
    assert len(checkouts) == 1
    assert checkouts[0]["with"]["persist-credentials"] is False


@pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
def test_git_network_steps_bring_their_own_token(workflow_name: str) -> None:
    jobs = _load(workflow_name)["jobs"]
    missing = []
    for job_id, job in jobs.items():
        steps = job.get("steps", [])
        if not any(
            str(s.get("uses", "")).startswith("actions/checkout@") and not _persists(s)
            for s in steps
        ):
            continue
        for step in steps:
            run = step.get("run", "")
            if any(cmd in run for cmd in _GIT_NETWORK):
                if (step.get("env") or {}).get("GH_TOKEN") != JOB_TOKEN:
                    missing.append(f"{job_id}: {step.get('name')}")
    assert not missing, f"{workflow_name}: git with no credential in {missing}"


def _authed_step(workflow_name: str, job_id: str) -> dict:
    steps = _load(workflow_name)["jobs"][job_id]["steps"]
    return next(s for s in steps if s.get("name") == AUTHED_STEPS[job_id])


@pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
@pytest.mark.parametrize("job_id", sorted(AUTHED_STEPS))
def test_the_token_goes_in_as_step_scoped_config(
    workflow_name: str, job_id: str
) -> None:
    step = _authed_step(workflow_name, job_id)
    assert step["env"]["GH_TOKEN"] == JOB_TOKEN
    assert step["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert step["shell"] == "bash"
    assert step["run"].startswith(AUTH_PREAMBLE)
    # The credential never reaches argv or a URL, where git would store it.
    assert "x-access-token:$" not in step["run"]


def test_every_step_scoped_credential_is_the_same_code() -> None:
    for name in LANGUAGE_WORKFLOWS:
        for job in _load(name)["jobs"].values():
            for step in job.get("steps", []):
                run = step.get("run", "")
                if "GIT_CONFIG_VALUE_" in run:
                    assert AUTH_PREAMBLE in run, f"{name}: {step.get('name')} drifted"


def _git(*args: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _commit(repo: Path, name: str, env: dict[str, str]) -> None:
    (repo / name).write_text(name, encoding="utf-8")
    _git("add", name, cwd=repo, env=env).check_returncode()
    _git("commit", "-qm", name, cwd=repo, env=env).check_returncode()


@pytest.fixture
def git_env(tmp_path: Path) -> dict[str, str]:
    """An env with no user or system git config, and file:// submodules allowed."""
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        # Pre-set entry the step must keep: without it the submodule clone
        # below is refused.
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "protocol.file.allow",
        "GIT_CONFIG_VALUE_0": "always",
    }


def _workspace(tmp_path: Path, env: dict[str, str]) -> Path:
    """Model a checkout: a shallow clone of a repo with a submodule."""
    sub = tmp_path / "sub"
    sub.mkdir()
    _git("init", "-q", "-b", "main", cwd=sub, env=env).check_returncode()
    _commit(sub, "sub-file", env)

    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-q", "-b", "main", cwd=origin, env=env).check_returncode()
    for name in ("one", "two", "three"):
        _commit(origin, name, env)
    _git(
        "submodule", "add", "-q", sub.as_uri(), "sub", cwd=origin, env=env
    ).check_returncode()
    _git("commit", "-qm", "sub", cwd=origin, env=env).check_returncode()

    workspace = tmp_path / "workspace"
    _git(
        "clone",
        "-q",
        "--depth",
        "1",
        origin.as_uri(),
        str(workspace),
        cwd=tmp_path,
        env=env,
    ).check_returncode()
    return workspace


def _run_step(
    step: dict, workspace: Path, env: dict[str, str]
) -> subprocess.CompletedProcess:
    # The trailing probe reads the header the way code inside the step would.
    script = step["run"] + f"git config --get {HEADER_KEY}\n"
    step_env = {**env, **step["env"], "GH_TOKEN": FAKE_TOKEN, "SUBMODULES": "sub"}
    return subprocess.run(
        ["bash", "-e", "-c", script],
        cwd=workspace,
        env=step_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


@pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None,
    reason="bash and git needed",
)
@pytest.mark.parametrize("job_id", sorted(AUTHED_STEPS))
def test_the_step_works_and_leaves_no_credential_behind(
    tmp_path: Path, git_env: dict[str, str], job_id: str
) -> None:
    workspace = _workspace(tmp_path, git_env)
    result = _run_step(_authed_step("rust-ci.yml", job_id), workspace, git_env)
    assert result.returncode == 0, result.stderr

    encoded = base64.b64encode(f"x-access-token:{FAKE_TOKEN}".encode()).decode()
    assert result.stdout.splitlines()[-1] == f"AUTHORIZATION: basic {encoded}"
    assert f"::add-mask::{encoded}" in result.stdout

    if job_id == "quality":
        count = _git("rev-list", "--count", "HEAD", cwd=workspace, env=git_env)
        assert count.stdout.strip() == "4"
    else:
        assert (workspace / "sub" / "sub-file").read_text(
            encoding="utf-8"
        ) == "sub-file"

    # A later step -- the tests -- starts from the job's env, not this step's.
    later = _git("config", "--get-regexp", "extraheader", cwd=workspace, env=git_env)
    assert later.returncode == 1 and not later.stdout
    configs = [workspace / ".git" / "config"]
    configs += (workspace / ".git" / "modules").glob("*/config")
    for config in configs:
        text = config.read_text(encoding="utf-8")
        for secret in (FAKE_TOKEN, encoded, "extraheader", "x-access-token"):
            assert secret not in text, f"{config} holds {secret!r}"
