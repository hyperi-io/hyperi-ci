# Project:   HyperI CI
# File:      tests/unit/test_docker_credentials.py
# Purpose:   Keep Docker registry credentials out of jobs that run repo code
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Which jobs may log in to a Docker registry, and which may not.

docker/login-action writes the credential to `~/.docker/config.json`, where it
stays for the rest of the job, so any code a later step runs can read it
(issue #406). The language workflows run the repo's own code in every job that
checks it out -- build scripts under clippy, dependency installs, the tests --
so they log in to no registry at all. A step-scoped login cannot cover the Test
job either: testcontainers resolves its credential from `DOCKER_AUTH_CONFIG`,
`DOCKER_CONFIG` or `~/.docker/config.json` at the moment it pulls, inside the
test process. Test pulls from Docker Hub are anonymous.
"""

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"
LANGUAGE_WORKFLOWS = ("rust-ci.yml", "python-ci.yml", "ts-ci.yml", "go-ci.yml")
SCANNED = (*LANGUAGE_WORKFLOWS, "_release-tail.yml")
DOCKER_HUB = {"docker.io", "index.docker.io", "registry-1.docker.io"}

# Jobs allowed a Docker Hub login, and why no repo code can read it there.
DOCKER_HUB_LOGINS: dict[tuple[str, str], str] = {
    ("_release-tail.yml", "container"): (
        "authenticates the Dockerfile's base-image pulls; the only repo code "
        "after it is the Dockerfile, whose RUN steps execute inside BuildKit "
        "and never see the client's config.json"
    ),
}


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _is_login(step: dict) -> bool:
    return (
        str(step.get("uses", "")).startswith("docker/login-action@")
        or "docker login" in str(step.get("run", ""))
        or "DOCKER_AUTH_CONFIG" in (step.get("env") or {})
    )


def _is_docker_hub(step: dict) -> bool:
    # docker/login-action defaults to Docker Hub. A shell `docker login` or a
    # DOCKER_AUTH_CONFIG counts as Docker Hub too: its registry is not parsed.
    registry = (step.get("with") or {}).get("registry", "")
    return _is_login(step) and (not registry or registry in DOCKER_HUB)


def _logins(name: str) -> list[tuple[str, dict]]:
    return [
        (job_id, step)
        for job_id, job in _load(WORKFLOW_DIR / name)["jobs"].items()
        for step in job.get("steps", [])
        if _is_login(step)
    ]


@pytest.mark.parametrize("workflow_name", LANGUAGE_WORKFLOWS)
def test_language_workflows_log_in_to_no_registry(workflow_name: str) -> None:
    found = [f"{job_id}: {step.get('name')}" for job_id, step in _logins(workflow_name)]
    assert not found, (
        f"{workflow_name}: {found} leave a registry credential in "
        "~/.docker/config.json for the repo code later steps run (issue #406)"
    )


def test_composite_actions_log_in_to_no_registry() -> None:
    # A composite runs inside whichever job calls it, repo code included.
    found = []
    for action in sorted(ACTIONS_DIR.glob("*/action.y*ml")):
        steps = (_load(action).get("runs") or {}).get("steps") or []
        found += [
            f"{action.parent.name}: {s.get('name')}" for s in steps if _is_login(s)
        ]
    assert not found, f"composite actions log in to a registry: {found}"


@pytest.mark.parametrize("workflow_name", SCANNED)
def test_docker_hub_logins_only_where_listed(workflow_name: str) -> None:
    unlisted = [
        job_id
        for job_id, step in _logins(workflow_name)
        if _is_docker_hub(step) and (workflow_name, job_id) not in DOCKER_HUB_LOGINS
    ]
    assert not unlisted, (
        f"{workflow_name}: {unlisted} log in to Docker Hub; drop the login or "
        "list the job with the reason no repo code can read the credential"
    )


def test_every_listed_docker_hub_login_still_exists() -> None:
    stale = [
        key
        for key in DOCKER_HUB_LOGINS
        if not any(
            job_id == key[1] and _is_docker_hub(step)
            for job_id, step in _logins(key[0])
        )
    ]
    assert not stale, (
        f"no Docker Hub login any more, drop from DOCKER_HUB_LOGINS: {stale}"
    )


@pytest.mark.parametrize("key", sorted(DOCKER_HUB_LOGINS))
def test_docker_hub_login_skips_a_fork_pr(key: tuple[str, str]) -> None:
    # A fork PR reads `vars` but not `secrets`, so a login gated only on
    # vars.DOCKERHUB_USERNAME sends an empty password and fails the job.
    workflow_name, job_id = key
    job = _load(WORKFLOW_DIR / workflow_name)["jobs"][job_id]
    for step in job["steps"]:
        if _is_docker_hub(step):
            gates = f"{job.get('if', '')} {step.get('if', '')}"
            assert "head.repo.fork != true" in gates, f"{job_id}: {step.get('name')}"
