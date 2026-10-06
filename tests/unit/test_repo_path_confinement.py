# Project:   HyperI CI
# File:      tests/unit/test_repo_path_confinement.py
# Purpose:   Paths a repo's config names stay inside the project root
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""The repo-named paths hyperi-ci confines to the checkout, call site by call site.

Covered: ``release.container.dockerfile`` and ``.context``,
``[tool.hatch.version] path`` and the ``VERSION`` file the release version
falls back to. The Container job
reads them after ``docker/login-action`` has written the Docker Hub token and
the GHCR login to ``~/.docker/config.json``, so a path naming that file would
put it into an image the job pushes, or into a build arg. Each is held to the
same three escapes -- an absolute path, a ``..`` and a symlink -- and must
still take an ordinary relative path.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hyperi_ci import config as config_module
from hyperi_ci.config import CIConfig
from hyperi_ci.container import stage as stage_module
from hyperi_ci.repo_path import RepoPathError, confine
from hyperi_ci.stamp import stamp_version

SECRET = '{"auths": {"ghcr.io": {"auth": "c2VjcmV0"}}}\n'


@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, Path]:
    """A checkout at ``repo/`` and a credential file beside it, outside the root."""
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "home"
    outside.mkdir()
    (outside / "config.json").write_text(SECRET, encoding="utf-8")
    (repo / "escape").symlink_to(outside)
    return repo, outside


def _escapes(repo: Path, outside: Path, name: str) -> list[str]:
    """The three ways out: absolute, `..`, and a symlink inside the repo."""
    del repo
    return [str(outside / name), f"../{outside.name}/{name}", f"escape/{name}"]


ESCAPE_IDS = ["absolute", "dotdot", "symlink"]


# --- container: build context and Dockerfile ------------------------------


def _container_config(container: dict) -> CIConfig:
    return CIConfig(language="rust", _raw={"publish": {"container": container}})


class TestContainerPaths:
    @pytest.fixture(autouse=True)
    def _in_repo(
        self, layout: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, _ = layout
        monkeypatch.chdir(repo)
        (repo / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        # A validate build constrains platforms to the binaries Build left.
        (repo / "dist").mkdir()
        (repo / "dist" / f"{repo.name}-linux-amd64").write_bytes(b"\x7fELF")
        monkeypatch.delenv("HYPERCI_RELEASE_MODE", raising=False)
        monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
        monkeypatch.setenv("GITHUB_OUTPUT", str(repo.parent / "gh_output"))
        monkeypatch.setenv("GITHUB_SHA", "abc12345abc12345abc")
        monkeypatch.delenv("HYPERCI_CONTAINER_RESOLVE_ONLY", raising=False)
        # Validate pushes to main and dev-push PRs carry no predicted version.
        monkeypatch.delenv("HYPERCI_VERSION", raising=False)
        self.build = MagicMock(return_value=0)
        monkeypatch.setattr(stage_module, "build_and_push", self.build)

    @pytest.mark.parametrize("key", ["context", "dockerfile"])
    @pytest.mark.parametrize("which", range(3), ids=ESCAPE_IDS)
    def test_the_resolve_step_refuses_an_escape(
        self,
        layout: tuple[Path, Path],
        monkeypatch: pytest.MonkeyPatch,
        key: str,
        which: int,
    ) -> None:
        """Refused before either login, so nothing is in ~/.docker yet."""
        repo, outside = layout
        monkeypatch.setenv("HYPERCI_CONTAINER_RESOLVE_ONLY", "1")
        name = "config.json" if key == "dockerfile" else ""
        value = _escapes(repo, outside, name)[which]
        rc = stage_module.run(
            _container_config({"enabled": True, key: value}), language="rust"
        )
        assert rc == 1

    @pytest.mark.parametrize("key", ["context", "dockerfile"])
    @pytest.mark.parametrize("which", range(3), ids=ESCAPE_IDS)
    def test_the_build_refuses_an_escape(
        self, layout: tuple[Path, Path], key: str, which: int
    ) -> None:
        repo, outside = layout
        name = "config.json" if key == "dockerfile" else ""
        value = _escapes(repo, outside, name)[which]
        rc = stage_module.run(
            _container_config({"enabled": True, key: value}), language="rust"
        )
        assert rc == 1
        self.build.assert_not_called()

    @pytest.mark.parametrize("context", [".", "", "docker"])
    def test_a_context_inside_the_repo_builds(
        self, layout: tuple[Path, Path], context: str
    ) -> None:
        repo, _ = layout
        (repo / "docker").mkdir()
        rc = stage_module.run(
            _container_config({"enabled": True, "context": context}), language="rust"
        )
        assert rc == 0
        assert self.build.call_args.kwargs["context"] == context

    def test_a_symlinked_version_never_reaches_a_build_arg(
        self, layout: tuple[Path, Path]
    ) -> None:
        repo, outside = layout
        (repo / "VERSION").symlink_to(outside / "config.json")
        rc = stage_module.run(
            _container_config({"enabled": True, "build_args": {"C": "{version}"}}),
            language="rust",
        )
        assert rc == 1
        self.build.assert_not_called()

    @pytest.mark.parametrize(
        "content",
        ['1.2.3"; wget x\n', SECRET, "latest\n", "1.2\n", "1.2.3 4\n"],
    )
    def test_a_version_file_that_is_not_a_version_is_refused(
        self, layout: tuple[Path, Path], content: str
    ) -> None:
        repo, _ = layout
        (repo / "VERSION").write_text(content, encoding="utf-8")
        rc = stage_module.run(
            _container_config({"enabled": True, "build_args": {"C": "{version}"}}),
            language="rust",
        )
        assert rc == 1
        self.build.assert_not_called()

    def test_a_version_file_feeds_the_build_arg(
        self, layout: tuple[Path, Path]
    ) -> None:
        repo, _ = layout
        (repo / "VERSION").write_text("1.2.3-rc.1\n", encoding="utf-8")
        rc = stage_module.run(
            _container_config({"enabled": True, "build_args": {"C": "{version}"}}),
            language="rust",
        )
        assert rc == 0
        assert self.build.call_args.kwargs["build_args"] == {"C": "1.2.3-rc.1"}

    def test_a_dockerfile_inside_the_repo_builds(
        self, layout: tuple[Path, Path]
    ) -> None:
        repo, _ = layout
        (repo / "docker").mkdir()
        (repo / "docker" / "app.Dockerfile").write_text(
            "FROM scratch\n", encoding="utf-8"
        )
        rc = stage_module.run(
            _container_config({"enabled": True, "dockerfile": "docker/app.Dockerfile"}),
            language="rust",
        )
        assert rc == 0
        self.build.assert_called_once()


# --- the hatch version file stamp-version writes ---------------------------


_HATCH = """\
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "hatch-lib"
dynamic = ["version"]

[tool.hatch.version]
path = {path}
"""


class TestHatchVersionPath:
    @pytest.fixture(autouse=True)
    def _fresh_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config_module, "_config_cache", None)

    @staticmethod
    def _project(repo: Path, path: str) -> None:
        (repo / "pyproject.toml").write_text(
            _HATCH.format(path=json.dumps(path)),
            encoding="utf-8",
        )

    @pytest.mark.parametrize("which", range(3), ids=ESCAPE_IDS)
    def test_an_escape_is_refused_and_left_alone(
        self, layout: tuple[Path, Path], which: int
    ) -> None:
        repo, outside = layout
        target = outside / "version.py"
        target.write_text('__version__ = "1.0.0"\n', encoding="utf-8")
        self._project(repo, _escapes(repo, outside, "version.py")[which])
        assert stamp_version("2.0.0", project_dir=repo) == 1
        assert target.read_text(encoding="utf-8") == '__version__ = "1.0.0"\n'

    def test_a_path_inside_the_repo_is_stamped(self, layout: tuple[Path, Path]) -> None:
        repo, _ = layout
        init = repo / "src" / "hatch_lib" / "__init__.py"
        init.parent.mkdir(parents=True)
        init.write_text('__version__ = "1.0.0"\n', encoding="utf-8")
        self._project(repo, "src/hatch_lib/__init__.py")
        assert stamp_version("2.0.0", project_dir=repo) == 0
        assert init.read_text(encoding="utf-8") == '__version__ = "2.0.0"\n'


# --- the helper itself ------------------------------------------------------


class TestConfine:
    def test_a_sibling_sharing_the_roots_name_is_outside(
        self, layout: tuple[Path, Path]
    ) -> None:
        """`/x/repo-evil` starts with the string `/x/repo` but is not inside it."""
        repo, _ = layout
        sibling = repo.parent / f"{repo.name}-evil"
        sibling.mkdir()
        (sibling / "config.json").write_text(SECRET, encoding="utf-8")
        for path in (f"../{repo.name}-evil/config.json", str(sibling / "config.json")):
            with pytest.raises(RepoPathError, match="resolves outside"):
                confine(path, repo, key="test")

    @pytest.mark.parametrize("path", [".", "", "a/../b", "sub/file"])
    def test_paths_that_stay_inside_resolve_under_the_root(
        self, layout: tuple[Path, Path], path: str
    ) -> None:
        repo, _ = layout
        assert confine(path, repo, key="test").is_relative_to(repo.resolve())
