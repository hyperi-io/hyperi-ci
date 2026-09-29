# Project:   HyperI CI
# File:      tests/unit/test_sibling_checkouts.py
# Purpose:   Keep the job token out of every clone's URL and git config
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""rust-ci.yml's `Clone sibling checkouts` step, and credentials in clone URLs.

git stores a clone's URL as its `origin`, so a token written into that URL
lands in the sibling's `.git/config`, where the Test job's test code can read
it whether or not the repo opted into `test-github-token` (issue #398).
"""

import base64
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent.parent
FAKE_TOKEN = "ghs_notarealtokenbutshapedlikeone0123456789"

# A URL with userinfo: https://user@host or https://user:secret@host.
_URL_WITH_CREDENTIAL = re.compile(r"https?://[^\s/\"'@]+@")


def _scanned_files() -> list[Path]:
    patterns = (
        ".github/workflows/*.yml",
        ".github/actions/**/*.yml",
        ".github/actions/**/*.py",
        "src/**/*.py",
        "scripts/**/*.py",
    )
    return sorted(p for pattern in patterns for p in REPO_ROOT.glob(pattern))


def test_no_clone_url_carries_a_credential() -> None:
    hits = [
        f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}"
        for path in _scanned_files()
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        )
        if _URL_WITH_CREDENTIAL.search(line) or "x-access-token:$" in line
    ]
    assert not hits, "credential in a URL:\n" + "\n".join(hits)


def _sibling_step() -> dict:
    wf = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "rust-ci.yml").read_text(
            encoding="utf-8"
        )
    )
    steps = wf["jobs"]["test"]["steps"]
    return next(s for s in steps if s.get("name") == "Clone sibling checkouts")


def _git(*args: str, cwd: Path, env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.stdout.strip()


def _bare_remote(remotes: Path, repo: str, branch: str | None) -> None:
    """Create ``remotes/<repo>.git`` holding one commit, optionally on ``branch``."""
    work = remotes / "work" / repo
    work.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=work)
    (work / "README").write_text(repo, encoding="utf-8")
    _git("add", "README", cwd=work)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c", cwd=work)
    if branch:
        _git("branch", branch, cwd=work)
    _git("clone", "-q", "--bare", str(work), str(remotes / f"{repo}.git"), cwd=remotes)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")
def test_the_sibling_step_leaves_no_credential_in_git_config(tmp_path: Path) -> None:
    remotes = tmp_path / "remotes"
    _bare_remote(remotes, "acme/lib", branch=None)
    _bare_remote(remotes, "acme/other", branch="v1")
    workspace = tmp_path / "ws" / "workspace"
    workspace.mkdir(parents=True)

    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "SIBLINGS": "acme/lib acme/other@v1",
        "GH_TOKEN": FAKE_TOKEN,
        "GIT_TERMINAL_PROMPT": "0",
        # Pre-set entries the step must keep: they route github.com, with or
        # without a token in the URL, to the local remotes.
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": f"url.{remotes.as_uri()}/.insteadOf",
        "GIT_CONFIG_VALUE_0": "https://github.com/",
        "GIT_CONFIG_KEY_1": f"url.{remotes.as_uri()}/.insteadOf",
        "GIT_CONFIG_VALUE_1": f"https://x-access-token:{FAKE_TOKEN}@github.com/",
    }
    subprocess.run(
        ["bash", "-e", "-c", _sibling_step()["run"]],
        cwd=workspace,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    encoded = base64.b64encode(f"x-access-token:{FAKE_TOKEN}".encode()).decode()
    for name, repo in (("lib", "acme/lib"), ("other", "acme/other")):
        sibling = workspace.parent / name
        assert (sibling / "README").read_text(encoding="utf-8") == repo
        config = (sibling / ".git" / "config").read_text(encoding="utf-8")
        for secret in (FAKE_TOKEN, encoded, "extraheader", "x-access-token"):
            assert secret not in config, f"{name}/.git/config holds {secret!r}"
        origin = _git("config", "--get", "remote.origin.url", cwd=sibling, env=env)
        assert origin == f"https://github.com/{repo}.git"
    assert _git("branch", "--show-current", cwd=workspace.parent / "other") == "v1"
