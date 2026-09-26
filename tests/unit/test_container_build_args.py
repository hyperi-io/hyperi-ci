# Project:   HyperI CI
# File:      tests/unit/test_container_build_args.py
# Purpose:   release.container.build_args placeholder substitution
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED

"""A Dockerfile ``ARG`` can receive the release version (issue #342).

``release.container.build_args`` used to reach ``--build-arg`` verbatim, so the
version only ever landed on the OCI label, which a Dockerfile cannot read.
"""

from pathlib import Path
from typing import Any

import pytest

from hyperi_ci.config import CIConfig, OrgConfig
from hyperi_ci.container import stage
from hyperi_ci.container.build import BuildArgError, render_build_args


class TestRenderBuildArgs:
    def test_version_and_sha_are_substituted(self) -> None:
        rendered = render_build_args(
            {"CODE_VERSION": "{version}", "CODE_SHA": "{sha}"},
            version="1.4.0",
            sha="0123456789abcdef",
        )
        assert rendered == {"CODE_VERSION": "1.4.0", "CODE_SHA": "0123456789abcdef"}

    def test_placeholders_sit_inside_other_text(self) -> None:
        rendered = render_build_args(
            {"BUILD_ID": "v{version}+{sha}.{version}"}, version="2.0.1", sha="abc"
        )
        assert rendered == {"BUILD_ID": "v2.0.1+abc.2.0.1"}

    def test_a_value_without_placeholders_passes_through_unchanged(self) -> None:
        raw = {"BASE": "debian:bookworm-slim", "FLAGS": "--opt=a,b c"}
        assert render_build_args(raw, version="1.0.0", sha="abc") == raw

    def test_a_non_string_value_renders_as_before(self) -> None:
        # docker got f"{key}={value}" before, so a YAML int or bool keeps its text.
        rendered = render_build_args(
            {"PORT": 8080, "DEBUG": True}, version="1.0.0", sha="abc"
        )
        assert rendered == {"PORT": "8080", "DEBUG": "True"}

    def test_keys_are_never_substituted(self) -> None:
        rendered = render_build_args({"{version}": "x"}, version="1.0.0", sha="abc")
        assert rendered == {"{version}": "x"}

    def test_doubled_braces_are_literal(self) -> None:
        rendered = render_build_args(
            {"TEMPLATE": "{{name}}-{version}", "JSON": '{{"a": 1}}'},
            version="3.2.1",
            sha="abc",
        )
        assert rendered == {"TEMPLATE": "{name}-3.2.1", "JSON": '{"a": 1}'}

    def test_none_and_empty_render_to_nothing(self) -> None:
        assert render_build_args(None, version="1.0.0", sha="abc") == {}
        assert render_build_args({}, version="1.0.0", sha="abc") == {}

    def test_an_unknown_placeholder_fails_and_names_itself(self) -> None:
        with pytest.raises(BuildArgError) as caught:
            render_build_args({"CODE_VERSION": "{verison}"}, version="1.0.0", sha="abc")
        message = str(caught.value)
        assert "{verison}" in message
        assert "CODE_VERSION" in message
        assert "{version}" in message and "{sha}" in message

    @pytest.mark.parametrize("value", ["{}", "{0}", "{version!r}", "{version:>9}"])
    def test_anything_but_the_bare_placeholders_fails(self, value: str) -> None:
        with pytest.raises(BuildArgError):
            render_build_args({"ARG": value}, version="1.0.0", sha="abc")

    @pytest.mark.parametrize("value", ["{version", "version}", "a { b"])
    def test_an_unbalanced_brace_fails_with_the_escape_hint(self, value: str) -> None:
        with pytest.raises(BuildArgError) as caught:
            render_build_args({"ARG": value}, version="1.0.0", sha="abc")
        message = str(caught.value)
        assert "ARG" in message
        assert "{{" in message

    def test_a_non_mapping_fails(self) -> None:
        not_a_mapping: Any = ["CODE_VERSION={version}"]
        with pytest.raises(BuildArgError):
            render_build_args(not_a_mapping, version="1.0.0", sha="abc")


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "thing"\ndescription = "Ships the thing"\n',
        encoding="utf-8",
    )
    (tmp_path / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for name in ("HYPERCI_VERSION", "GITHUB_SHA", "GITHUB_REF_NAME"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _dispatch(
    monkeypatch: pytest.MonkeyPatch, build_args: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """Run ``_dispatch_build`` up to docker, returning its rc and buildx kwargs."""
    seen: dict[str, Any] = {}

    def record_build(**kwargs: Any) -> int:
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(stage, "build_and_push", record_build)
    rc = stage._dispatch_build(
        dockerfile_path=Path("Dockerfile"),
        container_cfg={"build_args": build_args},
        config=CIConfig(_raw={}),
        org=OrgConfig(),
        registry_bases=["ghcr.io/hyperi-io"],
        push_mode="release",
    )
    return rc, seen


class TestStageWiring:
    def test_build_args_carry_the_label_version_and_revision(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HYPERCI_VERSION", "v4.5.6")
        monkeypatch.setenv("GITHUB_SHA", "feedfacecafebeef0123456789abcdef01234567")

        rc, seen = _dispatch(
            monkeypatch,
            {"CODE_VERSION": "{version}", "CODE_SHA": "{sha}", "PLAIN": "as-is"},
        )

        assert rc == 0
        labels = seen["labels"]
        args = seen["build_args"]
        assert args["CODE_VERSION"] == labels["org.opencontainers.image.version"]
        assert args["CODE_SHA"] == labels["org.opencontainers.image.revision"]
        assert args["CODE_VERSION"] == "4.5.6"
        assert args["CODE_SHA"] == "feedfacecafebeef0123456789abcdef01234567"
        assert args["PLAIN"] == "as-is"

    def test_with_no_version_known_the_arg_matches_the_label_fallback(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No HYPERCI_VERSION, no VERSION file, not a git repo, no ref name.
        rc, seen = _dispatch(monkeypatch, {"CODE_VERSION": "{version}"})

        assert rc == 0
        label = seen["labels"]["org.opencontainers.image.version"]
        assert seen["build_args"]["CODE_VERSION"] == label == "0.0.0"

    def test_an_unknown_placeholder_fails_the_stage_before_docker(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rc, seen = _dispatch(monkeypatch, {"CODE_VERSION": "{verison}"})

        assert rc == 1
        assert seen == {}

    def test_no_build_args_still_passes_none(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rc, seen = _dispatch(monkeypatch, {})

        assert rc == 0
        assert seen["build_args"] is None
