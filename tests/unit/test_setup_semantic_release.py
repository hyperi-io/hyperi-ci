# Project:   HyperI CI
# File:      tests/unit/test_setup_semantic_release.py
# Purpose:   Run the setup-semantic-release config step against real workspaces
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""The config step runs in every consumer's release job, so its bash runs here.

The step is lifted out of ``action.yml`` verbatim and run under bash against a
real directory, so the test fails if the shipped script changes behaviour.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_ACTION_DIR = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "actions"
    / "setup-semantic-release"
)
_CENTRAL = json.loads((_ACTION_DIR / "default.releaserc.json").read_text("utf-8"))


def _steps() -> list[dict]:
    doc = yaml.safe_load((_ACTION_DIR / "action.yml").read_text(encoding="utf-8"))
    return doc["runs"]["steps"]


def _config_step() -> str:
    (step,) = [
        s for s in _steps() if s.get("name", "").startswith("Provide the release")
    ]
    return step["run"]


def _run(workspace: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GITHUB_WORKSPACE": str(workspace),
        "GITHUB_ACTION_PATH": str(_ACTION_DIR),
    }
    return subprocess.run(
        ["bash", "-e", "-c", _config_step()],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _installed(workspace: Path) -> dict:
    return json.loads((workspace / ".releaserc.json").read_text(encoding="utf-8"))


class TestTheCentralConfigAlwaysWins:
    def test_a_repo_with_no_config_gets_the_central_one(self, tmp_path: Path) -> None:
        assert _run(tmp_path).returncode == 0
        assert _installed(tmp_path) == _CENTRAL

    @pytest.mark.parametrize(
        "name",
        [
            ".releaserc",
            ".releaserc.json",
            ".releaserc.yaml",
            ".releaserc.yml",
            ".releaserc.js",
            ".releaserc.cjs",
            ".releaserc.mjs",
            "release.config.js",
            "release.config.cjs",
            "release.config.mjs",
        ],
    )
    def test_every_repo_config_file_is_removed(self, tmp_path: Path, name: str) -> None:
        # An exec-only config carries none of the #37 plugins and is still dropped.
        (tmp_path / name).write_text(
            '{"plugins": ["@semantic-release/exec"]}', encoding="utf-8"
        )
        result = _run(tmp_path)
        assert result.returncode == 0
        assert "::warning::" in result.stdout
        left = {p.name for p in tmp_path.iterdir()}
        assert left == {".releaserc.json"}
        assert _installed(tmp_path) == _CENTRAL


@pytest.mark.skipif(
    shutil.which("node") is None, reason="the step reads package.json with node"
)
class TestPackageJson:
    @pytest.mark.parametrize("key", ["release", "cosmiconfig"])
    def test_a_config_key_fails_the_run(self, tmp_path: Path, key: str) -> None:
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "x", key: {"branches": ["main"]}}),
            encoding="utf-8",
        )
        result = _run(tmp_path)
        assert result.returncode == 1
        assert "::error::" in result.stdout
        assert not (tmp_path / ".releaserc.json").exists()

    def test_a_symlink_to_code_is_refused_and_never_run(self, tmp_path: Path) -> None:
        marker = tmp_path / "ran"
        (tmp_path / "evil.cjs").write_text(
            f'require("fs").writeFileSync({json.dumps(str(marker))}, "x");'
            " module.exports = {};",
            encoding="utf-8",
        )
        (tmp_path / "package.json").symlink_to("evil.cjs")
        result = _run(tmp_path)
        assert result.returncode == 1
        assert "symlink" in result.stdout
        assert not marker.exists()

    @pytest.mark.parametrize("body", ["{not json", "", "[]", "null"])
    def test_a_package_json_that_is_not_an_object(
        self, tmp_path: Path, body: str
    ) -> None:
        (tmp_path / "package.json").write_text(body, encoding="utf-8")
        result = _run(tmp_path)
        # Invalid JSON fails closed. Valid JSON that is not an object holds no key.
        assert result.returncode == (1 if body in ("{not json", "") else 0)

    def test_a_package_json_without_one_is_left_alone(self, tmp_path: Path) -> None:
        body = json.dumps({"name": "x", "version": "1.0.0"})
        (tmp_path / "package.json").write_text(body, encoding="utf-8")
        assert _run(tmp_path).returncode == 0
        assert (tmp_path / "package.json").read_text(encoding="utf-8") == body
        assert _installed(tmp_path) == _CENTRAL


class TestReleaseNotesSupplement:
    """The Tag job prints NEXT.md into the notes, so only a real file inside the repo counts."""

    @staticmethod
    def _notes(workspace: Path) -> subprocess.CompletedProcess[str]:
        (exec_plugin,) = [
            p for p in _CENTRAL["plugins"] if p[0] == "@semantic-release/exec"
        ]
        return subprocess.run(
            ["sh", "-c", exec_plugin[1]["generateNotesCmd"]],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    @staticmethod
    def _secret(tmp_path: Path) -> Path:
        secret = tmp_path / "outside" / "secret"
        secret.parent.mkdir()
        secret.write_text("TOKEN=hunter2\n", encoding="utf-8")
        return secret

    @pytest.mark.parametrize(
        "layout", ["file", "missing", "file-symlink", "dir-symlink", "directory"]
    )
    def test_only_a_regular_file_in_the_repo_is_printed(
        self, tmp_path: Path, layout: str
    ) -> None:
        repo = tmp_path / "repo"
        notes_dir = repo / ".github" / "release-notes"
        secret = self._secret(tmp_path)
        if layout == "dir-symlink":
            (repo / ".github").mkdir(parents=True)
            (secret.parent / "NEXT.md").write_text("TOKEN=hunter2\n", encoding="utf-8")
            notes_dir.symlink_to(secret.parent)
        else:
            notes_dir.mkdir(parents=True)
        if layout == "file":
            (notes_dir / "NEXT.md").write_text("Upgrade notes\n", encoding="utf-8")
        elif layout == "file-symlink":
            (notes_dir / "NEXT.md").symlink_to(secret)
        elif layout == "directory":
            (notes_dir / "NEXT.md").mkdir()
        result = self._notes(repo)
        assert result.returncode == 0
        assert result.stdout == ("Upgrade notes\n" if layout == "file" else "")
        assert "hunter2" not in result.stdout


class TestTheConfigDirectory:
    def test_a_config_dir_releaserc_is_removed(self, tmp_path: Path) -> None:
        (tmp_path / ".config").mkdir()
        (tmp_path / ".config" / "releaserc.json").write_text("{}", encoding="utf-8")
        assert _run(tmp_path).returncode == 0
        assert not (tmp_path / ".config" / "releaserc.json").exists()
        assert _installed(tmp_path) == _CENTRAL

    @pytest.mark.parametrize("name", [".config/config.js", "package.yaml"])
    def test_a_meta_config_fails_the_run(self, tmp_path: Path, name: str) -> None:
        # cosmiconfig reads these before any normal search, and runs the JS one.
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("release: {}\n", encoding="utf-8")
        result = _run(tmp_path)
        assert result.returncode == 1
        assert "::error::" in result.stdout


class TestNothingRunsFromTheCheckout:
    def test_the_install_runs_outside_the_checkout(self) -> None:
        # A repo .npmrc in the checkout would otherwise pick the registry.
        (step,) = [s for s in _steps() if s.get("name", "").startswith("Install")]
        assert step["working-directory"] == "${{ runner.temp }}"

    @pytest.mark.parametrize(
        "path",
        [
            Path(".github/actions/predict-version/action.yml"),
            Path(".github/workflows/_release-tail.yml"),
        ],
    )
    def test_callers_run_the_global_binary(self, path: Path) -> None:
        # npx prefers a checkout's node_modules/.bin and applies its .npmrc.
        text = (Path(__file__).resolve().parents[2] / path).read_text(encoding="utf-8")
        assert "npx semantic-release" not in text

    @pytest.mark.parametrize(
        "path",
        [
            Path(".github/actions/predict-version/action.yml"),
            Path(".github/workflows/_release-tail.yml"),
        ],
    )
    def test_callers_pin_the_repository_url(self, path: Path) -> None:
        # Otherwise package.json `repository` picks where the token is sent.
        text = (Path(__file__).resolve().parents[2] / path).read_text(encoding="utf-8")
        assert '--repository-url "https://github.com/${GITHUB_REPOSITORY}"' in text
