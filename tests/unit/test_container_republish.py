# Project:   HyperI CI
# File:      tests/unit/test_container_republish.py
# Purpose:   A release whose version tag is already published reuses that image
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from hyperi_ci.config import CIConfig, OrgConfig
from hyperi_ci.container import build, stage
from hyperi_ci.container.build import PublishedImage, published_image

_REF = "ghcr.io/hyperi-io/thing:v1.2.3"
_DIGEST = "sha256:" + "a" * 64
_REVISION = "feedfacecafebeef0123456789abcdef01234567"
_BOTH = frozenset({"linux/amd64", "linux/arm64"})


def _entry(os_name: str, arch: str) -> dict[str, Any]:
    return {
        "digest": "sha256:" + "b" * 64,
        "platform": {"os": os_name, "architecture": arch},
    }


def _inspect(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int,
    stdout: str = "",
    stderr: str = "",
) -> None:
    def fake(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    monkeypatch.setattr(build, "run_cmd", fake)


class TestPublishedImage:
    def test_reads_the_index_digest_revision_and_platforms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The shape buildx prints for a two-arch release with attestations."""
        manifest = {
            "digest": _DIGEST,
            "manifests": [
                _entry("linux", "amd64"),
                _entry("linux", "arm64"),
                _entry("unknown", "unknown"),
                _entry("unknown", "unknown"),
            ],
            "annotations": {"org.opencontainers.image.revision": _REVISION},
        }
        _inspect(monkeypatch, returncode=0, stdout=json.dumps(manifest))
        assert published_image(_REF) == PublishedImage(_DIGEST, _REVISION, _BOTH)

    def test_an_image_with_no_revision_or_index_still_counts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _inspect(monkeypatch, returncode=0, stdout=json.dumps({"digest": _DIGEST}))
        assert published_image(_REF) == PublishedImage(_DIGEST, None, frozenset())

    @pytest.mark.parametrize(
        "stderr", [f"ERROR: {_REF}: not found", "ERROR: unauthorized"]
    )
    def test_a_failed_lookup_is_not_published(
        self, monkeypatch: pytest.MonkeyPatch, stderr: str
    ) -> None:
        _inspect(monkeypatch, returncode=1, stderr=stderr)
        assert published_image(_REF) is None

    def test_a_tag_not_in_the_registry_is_logged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The run log has to show the lookup ran, or the first reuse proves nothing."""
        lines: list[str] = []
        monkeypatch.setattr(build, "info", lines.append)
        _inspect(monkeypatch, returncode=1, stderr=f"ERROR: {_REF}: not found")
        published_image(_REF)
        assert lines == [f"{_REF} is not in the registry yet, so building it"]

    def test_no_docker_is_not_published(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def missing(_cmd: list[str], **_kwargs: Any) -> None:
            raise FileNotFoundError("docker")

        monkeypatch.setattr(build, "run_cmd", missing)
        assert published_image(_REF) is None

    @pytest.mark.parametrize("stdout", ["", "not json", "[]", '{"digest": "md5:x"}'])
    def test_output_without_a_digest_is_not_published(
        self, monkeypatch: pytest.MonkeyPatch, stdout: str
    ) -> None:
        _inspect(monkeypatch, returncode=0, stdout=stdout)
        assert published_image(_REF) is None


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "thing"\ndescription = "Ships the thing"\n',
        encoding="utf-8",
    )
    (tmp_path / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HYPERCI_VERSION", "1.2.3")
    monkeypatch.setenv("GITHUB_SHA", _REVISION)
    return tmp_path


class _Run:
    def __init__(self) -> None:
        self.built = False
        self.asked: list[str] = []
        self.outputs: dict[str, str] = {}


def _dispatch(
    monkeypatch: pytest.MonkeyPatch,
    existing: PublishedImage | None,
    push_mode: str = "release",
) -> tuple[int, _Run]:
    run = _Run()

    def lookup(ref: str) -> PublishedImage | None:
        run.asked.append(ref)
        return existing

    def build_and_push(**_kwargs: Any) -> int:
        run.built = True
        return 0

    monkeypatch.setattr(stage, "published_image", lookup)
    monkeypatch.setattr(stage, "build_and_push", build_and_push)
    monkeypatch.setattr(stage, "set_github_output", run.outputs.update)
    rc = stage._dispatch_build(
        dockerfile_path=Path("Dockerfile"),
        config=CIConfig(_raw={}),
        org=OrgConfig(),
        registry_bases=["ghcr.io/hyperi-io"],
        push_mode=push_mode,
        binary_backed=False,
    )
    return rc, run


class TestARepublishedVersion:
    """A re-dispatched release must not move a published version tag.

    The rebuild got a new digest, the version tag moved to it, and the chart
    already published for that version was left pinned to an untagged image.
    """

    def test_the_published_image_is_reused_and_nothing_built(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rc, run = _dispatch(monkeypatch, PublishedImage(_DIGEST, _REVISION, _BOTH))
        assert rc == 0
        assert not run.built
        assert run.asked == [f"ghcr.io/hyperi-io/{project.name}:v1.2.3"]
        assert run.outputs == {
            "digest": _DIGEST,
            "image": f"ghcr.io/hyperi-io/{project.name}:v1.2.3@{_DIGEST}",
        }

    @pytest.mark.parametrize("revision", ["0" * 40, None])
    def test_an_image_not_from_this_commit_is_rebuilt(
        self, project: Path, monkeypatch: pytest.MonkeyPatch, revision: str | None
    ) -> None:
        """A failed release that never tagged leaves its image behind.

        The next release of that version is new code, so reusing the leftover
        would ship the old build under it.
        """
        rc, run = _dispatch(monkeypatch, PublishedImage(_DIGEST, revision, _BOTH))
        assert rc == 0
        assert run.built

    def test_an_index_missing_a_platform_is_rebuilt(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A single-arch index would ship a release without its arm64 image."""
        amd64_only = PublishedImage(_DIGEST, _REVISION, frozenset({"linux/amd64"}))
        rc, run = _dispatch(monkeypatch, amd64_only)
        assert rc == 0
        assert run.built

    def test_an_unpublished_version_is_built(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rc, run = _dispatch(monkeypatch, None)
        assert rc == 0
        assert run.built

    @pytest.mark.parametrize("push_mode", ["validate", "dev"])
    def test_only_a_release_asks_the_registry(
        self, project: Path, monkeypatch: pytest.MonkeyPatch, push_mode: str
    ) -> None:
        _rc, run = _dispatch(
            monkeypatch, PublishedImage(_DIGEST, _REVISION, _BOTH), push_mode=push_mode
        )
        assert run.asked == []
        assert run.built
