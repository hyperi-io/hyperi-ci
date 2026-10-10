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
    image: dict[str, Any] | None = None,
    children: dict[str, dict[str, Any]] | None = None,
) -> list[list[str]]:
    """Fake the registry; ``image`` answers ``.Image`` for ``_REF``."""
    calls: list[list[str]] = []

    def fake(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[-1] == "{{json .Image}}":
            ref = cmd[-3]
            found = (children or {}).get(ref, image if ref == _REF else None)
            return subprocess.CompletedProcess(cmd, 0, json.dumps(found), "")
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    monkeypatch.setattr(build, "run_cmd", fake)
    return calls


def _config(revision: str | None, arch: str = "amd64") -> dict[str, Any]:
    """The ``.Image`` shape buildx prints for one image."""
    labels = {"org.opencontainers.image.revision": revision} if revision else {}
    return {
        "architecture": arch,
        "os": "linux",
        "config": {"Labels": labels},
    }


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
        _inspect(
            monkeypatch,
            returncode=0,
            stdout=json.dumps({"digest": _DIGEST}),
            image=_config(None),
        )
        assert published_image(_REF) == PublishedImage(
            _DIGEST, None, frozenset({"linux/amd64"})
        )

    def test_a_single_image_reads_its_revision_from_the_config_labels(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A single-platform push has no index, so no index annotations."""
        manifest = {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": _DIGEST,
            "size": 669,
        }
        calls = _inspect(
            monkeypatch,
            returncode=0,
            stdout=json.dumps(manifest),
            image=_config(_REVISION, "arm64"),
        )
        assert published_image(_REF) == PublishedImage(
            _DIGEST, _REVISION, frozenset({"linux/arm64"})
        )
        assert len(calls) == 2

    def test_an_index_costs_one_lookup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manifest = {
            "digest": _DIGEST,
            "manifests": [_entry("linux", "amd64"), _entry("linux", "arm64")],
            "annotations": {"org.opencontainers.image.revision": _REVISION},
        }
        calls = _inspect(monkeypatch, returncode=0, stdout=json.dumps(manifest))
        published_image(_REF)
        assert len(calls) == 1

    def test_an_attested_single_platform_index_reads_its_image_labels(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """buildx pushes an unannotated index of one image plus attestations."""
        child = "sha256:" + "b" * 64
        manifest = {
            "digest": _DIGEST,
            "manifests": [_entry("linux", "amd64"), _entry("unknown", "unknown")],
        }
        _inspect(
            monkeypatch,
            returncode=0,
            stdout=json.dumps(manifest),
            children={
                f"ghcr.io/hyperi-io/thing@{child}": _config(_REVISION),
            },
        )
        assert published_image(_REF) == PublishedImage(
            _DIGEST, _REVISION, frozenset({"linux/amd64"})
        )

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
        self.writes: list[list[str]] = []


def _answer_for(ref: str, digest: str, revision: str) -> Any:
    """Fake registry holding one single-platform image at ``ref``."""

    def fake(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[-1] == "{{json .Image}}":
            body = _config(revision)
        else:
            body = {"mediaType": "application/vnd.oci.image.manifest.v1+json"}
            body["digest"] = digest
        assert cmd[-3] == ref
        return subprocess.CompletedProcess(cmd, 0, json.dumps(body), "")

    return fake


def _dispatch(
    monkeypatch: pytest.MonkeyPatch,
    existing: PublishedImage | None,
    push_mode: str = "release",
    repoint_rc: int = 0,
    raw: dict[str, Any] | None = None,
    real_lookup: bool = False,
) -> tuple[int, _Run]:
    run = _Run()

    def registry_write(
        cmd: list[str], **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        run.writes.append(cmd)
        return subprocess.CompletedProcess(cmd, repoint_rc, "", "denied")

    def lookup(ref: str) -> PublishedImage | None:
        run.asked.append(ref)
        return existing

    def build_and_push(**_kwargs: Any) -> int:
        run.built = True
        return 0

    if not real_lookup:
        monkeypatch.setattr(stage, "published_image", lookup)
    else:
        monkeypatch.setattr(stage, "published_image", published_image)
    monkeypatch.setattr(stage, "build_and_push", build_and_push)
    monkeypatch.setattr(stage, "run_cmd", registry_write)
    monkeypatch.setattr(stage, "set_github_output", run.outputs.update)
    rc = stage._dispatch_build(
        dockerfile_path=Path("Dockerfile"),
        config=CIConfig(_raw=raw or {}),
        org=OrgConfig(),
        registry_bases=["ghcr.io/hyperi-io"],
        push_mode=push_mode,
        binary_backed=False,
    )
    return rc, run


class TestATagDispatch:
    """A tag dispatch runs on the default branch but checks out the tag."""

    def test_the_checked_out_commit_decides_the_reuse(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for args in (
            ["init", "-q"],
            ["-c", "user.email=ci@example.invalid", "-c", "user.name=CI", "commit"]
            + ["--allow-empty", "-q", "-m", "fix: tagged"],
        ):
            subprocess.run(["git", *args], cwd=project, check=True)
        tagged = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        monkeypatch.setenv("GITHUB_SHA", "f" * 40)

        rc, run = _dispatch(monkeypatch, PublishedImage(_DIGEST, tagged, _BOTH))
        assert rc == 0
        assert not run.built


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

    def test_the_other_tags_are_pointed_at_the_reused_digest(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An earlier attempt may have died before pushing :latest and :sha-*."""
        repo = f"ghcr.io/hyperi-io/{project.name}"
        _rc, run = _dispatch(monkeypatch, PublishedImage(_DIGEST, _REVISION, _BOTH))
        assert len(run.writes) == 1
        cmd = run.writes[0]
        assert cmd[:4] == ["docker", "buildx", "imagetools", "create"]
        assert cmd[-1] == f"{repo}@{_DIGEST}"
        tagged = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--tag"]
        assert tagged
        assert f"{repo}:v1.2.3" not in tagged
        assert all(t.startswith(f"{repo}:") for t in tagged)

    def test_a_failed_repoint_fails_the_stage(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rc, run = _dispatch(
            monkeypatch, PublishedImage(_DIGEST, _REVISION, _BOTH), repoint_rc=1
        )
        assert rc == 1
        assert not run.built
        assert run.outputs == {}

    @pytest.mark.parametrize(
        ("revision", "reused"), [(_REVISION, True), ("0" * 40, False)]
    )
    def test_a_single_platform_image_is_reused_by_its_label_revision(
        self,
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
        revision: str,
        reused: bool,
    ) -> None:
        """The whole path: the real lookup against a single-image manifest."""
        monkeypatch.setattr(
            build,
            "run_cmd",
            _answer_for(f"ghcr.io/hyperi-io/{project.name}:v1.2.3", _DIGEST, revision),
        )
        rc, run = _dispatch(
            monkeypatch,
            None,
            raw={"release": {"container": {"platforms": ["linux/amd64"]}}},
            real_lookup=True,
        )
        assert rc == 0
        assert run.built is not reused

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
