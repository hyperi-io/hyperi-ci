# Project:   HyperI CI
# File:      tests/unit/test_container_description_label.py
# Purpose:   The description label is populated by the pipeline, not just accepted
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

"""`org.opencontainers.image.description` must arrive with something in it.

`build_oci_labels` has always taken a `description` argument, and
`test_container_labels.py` has always proved it honours one. The only
production caller never passed it, so every image shipped a blank label and
GHCR's package page showed no description -- green suite throughout.

These tests exercise the caller instead of the callee: they assert what
`_dispatch_build` puts on the image, which is the thing that was wrong.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from hyperi_ci.config import CIConfig, OrgConfig
from hyperi_ci.container import stage


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "thing"\ndescription = "Ships the thing"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _labels_from_dispatch(config: CIConfig) -> dict[str, str]:
    """Run _dispatch_build far enough to capture the labels it builds."""
    org = OrgConfig()
    dockerfile = Path("Dockerfile")
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    with patch.object(stage, "build_and_push", return_value=0) as pushed:
        with patch.object(stage, "_read_version", return_value="1.2.3"):
            with patch.object(stage, "_read_sha", return_value="deadbeef"):
                stage._dispatch_build(
                    dockerfile_path=dockerfile,
                    container_cfg={},
                    config=config,
                    org=org,
                    registry_bases=["ghcr.io/hyperi-io"],
                    push_mode="publish",
                )
    return pushed.call_args.kwargs["labels"]


class TestTheLabelIsPopulated:
    def test_description_comes_from_the_manifest(self, project: Path) -> None:
        labels = _labels_from_dispatch(CIConfig(_raw={}))
        assert labels["org.opencontainers.image.description"] == "Ships the thing"

    def test_config_overrides_the_manifest(self, project: Path) -> None:
        labels = _labels_from_dispatch(CIConfig(_raw={"description": "Override text"}))
        assert labels["org.opencontainers.image.description"] == "Override text"

    def test_a_project_with_no_description_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Blank is still possible, but it is reported rather than silent."""
        (tmp_path / "go.mod").write_text("module example.com/t\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        with patch.object(stage, "warn") as warned:
            with patch(
                "hyperi_ci.description_source.github_description", return_value=None
            ):
                labels = _labels_from_dispatch(CIConfig(_raw={}))
        assert labels["org.opencontainers.image.description"] == ""
        assert warned.called

    def test_the_label_is_never_silently_blank(self, project: Path) -> None:
        """The regression itself: a resolvable description must reach the image."""
        labels = _labels_from_dispatch(CIConfig(_raw={}))
        assert labels["org.opencontainers.image.description"].strip()
