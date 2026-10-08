# Project:   HyperI CI
# File:      tests/unit/test_container_binary_stage.py
# Purpose:   Tests for binary-staging Dockerfile rewriter
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for `hyperi_ci.container.binary_stage.stage_binary_dockerfile`.

Covers the bare-`COPY <app>` rewrite logic that fixes the Container
stage binary-placement bug.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from hyperi_ci.config import CIConfig, OrgConfig
from hyperi_ci.container import binary_stage, build, stage
from hyperi_ci.container.binary_stage import (
    LICENCE_DEST,
    find_licence_file,
    stage_binary_dockerfile,
)

# This repository's own root: a real build context that carries a LICENSE.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_dist_artefact(dist_dir: Path, name: str, arch: str) -> Path:
    dist_dir.mkdir(parents=True, exist_ok=True)
    path = dist_dir / f"{name}-linux-{arch}"
    path.write_bytes(b"\x7fELF\x02\x01\x01\x00fake binary")
    return path


def _write_dockerfile(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "Dockerfile"
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def cwd_tmp(tmp_path: Path, monkeypatch):
    """Run tests with cwd set to tmp_path so dist/ resolves correctly."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestNoRewriteCases:
    """Returns the original path unchanged when nothing needs rewriting."""

    def test_no_dockerfile_copies(self, cwd_tmp: Path) -> None:
        df = _write_dockerfile(
            cwd_tmp,
            "FROM ubuntu:24.04\nRUN echo hello\n",
        )
        result = stage_binary_dockerfile(df)
        assert result == df

    def test_already_parameterised_copy(self, cwd_tmp: Path) -> None:
        # ci-test-rust-app / dfe-loader pattern -- already uses TARGETARCH.
        # Should NOT be touched.
        _write_dist_artefact(cwd_tmp / "dist", "demo", "amd64")
        df = _write_dockerfile(
            cwd_tmp,
            "FROM ubuntu:24.04\n"
            "ARG TARGETARCH\n"
            "COPY dist/demo-linux-${TARGETARCH} /usr/local/bin/demo\n",
        )
        result = stage_binary_dockerfile(df)
        assert result == df

    def test_copy_from_other_stage(self, cwd_tmp: Path) -> None:
        # Multi-stage `COPY --from=builder` shouldn't be rewritten --
        # it copies from another build stage, not the build context.
        _write_dist_artefact(cwd_tmp / "dist", "demo", "amd64")
        df = _write_dockerfile(
            cwd_tmp,
            "FROM rust:1.84 AS builder\nRUN cargo build\n"
            "FROM ubuntu:24.04\n"
            "COPY --from=builder /app/target/release/demo /usr/local/bin/demo\n",
        )
        result = stage_binary_dockerfile(df)
        assert result == df

    def test_copy_with_no_matching_dist_artefact(self, cwd_tmp: Path) -> None:
        # `COPY config.yaml /etc/app/` -- config files, not binaries.
        # No dist/config.yaml-linux-amd64 → don't rewrite.
        df = _write_dockerfile(
            cwd_tmp,
            "FROM ubuntu:24.04\nCOPY config.yaml /etc/app/config.yaml\n",
        )
        result = stage_binary_dockerfile(df)
        assert result == df

    def test_copy_with_path_in_source(self, cwd_tmp: Path) -> None:
        # Path-form sources like `COPY src/ /app/` are skipped -- only
        # bare names are rewrite candidates.
        _write_dist_artefact(cwd_tmp / "dist", "demo", "amd64")
        df = _write_dockerfile(
            cwd_tmp,
            "FROM ubuntu:24.04\nCOPY ./demo /usr/local/bin/demo\n",
        )
        result = stage_binary_dockerfile(df)
        assert result == df


class TestRewrite:
    """Bare COPY of a name matching dist/<name>-linux-<arch> gets rewritten."""

    def test_basic_rewrite(self, cwd_tmp: Path) -> None:
        _write_dist_artefact(cwd_tmp / "dist", "dfe-archiver", "amd64")
        _write_dist_artefact(cwd_tmp / "dist", "dfe-archiver", "arm64")
        df = _write_dockerfile(
            cwd_tmp,
            "FROM ubuntu:24.04\nCOPY dfe-archiver /usr/local/bin/dfe-archiver\n",
        )
        result = stage_binary_dockerfile(df)
        # New temp file (not the original).
        assert result != df

        rewritten = result.read_text(encoding="utf-8")
        assert "ARG TARGETARCH" in rewritten
        assert (
            "COPY dist/dfe-archiver-linux-${TARGETARCH} /usr/local/bin/dfe-archiver"
            in rewritten
        )
        # Original line gone.
        assert "COPY dfe-archiver " not in rewritten

        # Caller is expected to clean up; remove for test hygiene.
        result.unlink()

    def test_rewrite_with_only_amd64_artefact(self, cwd_tmp: Path) -> None:
        # Push-to-main produces only amd64. The rewrite should still
        # fire -- TARGETARCH substitution still resolves correctly when
        # buildx is invoked with `--platform linux/amd64` only (the
        # platform filter handles arm64 absence upstream).
        _write_dist_artefact(cwd_tmp / "dist", "dfe-receiver", "amd64")
        df = _write_dockerfile(
            cwd_tmp,
            "FROM ubuntu:24.04\nCOPY dfe-receiver /usr/local/bin/dfe-receiver\n",
        )
        result = stage_binary_dockerfile(df)
        assert result != df
        result.unlink()

    def test_arg_targetarch_inserted_after_from(self, cwd_tmp: Path) -> None:
        _write_dist_artefact(cwd_tmp / "dist", "demo", "amd64")
        df = _write_dockerfile(
            cwd_tmp,
            "FROM ubuntu:24.04\nRUN apt-get update\nCOPY demo /usr/local/bin/demo\n",
        )
        result = stage_binary_dockerfile(df)
        rewritten = result.read_text(encoding="utf-8")
        result.unlink()

        # ARG TARGETARCH must come after FROM and before the COPY.
        from_idx = rewritten.find("FROM ubuntu")
        arg_idx = rewritten.find("ARG TARGETARCH")
        copy_idx = rewritten.find("COPY dist/demo")
        assert from_idx < arg_idx < copy_idx

    def test_no_duplicate_arg_when_already_present(self, cwd_tmp: Path) -> None:
        _write_dist_artefact(cwd_tmp / "dist", "demo", "amd64")
        df = _write_dockerfile(
            cwd_tmp,
            "FROM ubuntu:24.04\nARG TARGETARCH\nCOPY demo /usr/local/bin/demo\n",
        )
        result = stage_binary_dockerfile(df)
        rewritten = result.read_text(encoding="utf-8")
        result.unlink()

        assert rewritten.count("ARG TARGETARCH") == 1

    def test_multiple_bare_copies_share_one_arg(self, cwd_tmp: Path) -> None:
        # If a Dockerfile has multiple bare COPY-binary lines (rare but
        # possible -- e.g. main app + sidecar), one ARG TARGETARCH suffices.
        _write_dist_artefact(cwd_tmp / "dist", "main-app", "amd64")
        _write_dist_artefact(cwd_tmp / "dist", "sidecar", "amd64")
        df = _write_dockerfile(
            cwd_tmp,
            "FROM ubuntu:24.04\n"
            "COPY main-app /usr/local/bin/main-app\n"
            "COPY sidecar /usr/local/bin/sidecar\n",
        )
        result = stage_binary_dockerfile(df)
        rewritten = result.read_text(encoding="utf-8")
        result.unlink()

        assert rewritten.count("ARG TARGETARCH") == 1
        assert (
            "COPY dist/main-app-linux-${TARGETARCH} /usr/local/bin/main-app"
            in rewritten
        )
        assert (
            "COPY dist/sidecar-linux-${TARGETARCH} /usr/local/bin/sidecar" in rewritten
        )


class TestPreservesNonRewriteContent:
    """Surrounding Dockerfile content survives the rewrite intact."""

    def test_preserves_run_lines(self, cwd_tmp: Path) -> None:
        _write_dist_artefact(cwd_tmp / "dist", "demo", "amd64")
        original = (
            "FROM ubuntu:24.04\n"
            "RUN apt-get update && apt-get install -y curl\n"
            "COPY demo /usr/local/bin/demo\n"
            "RUN chmod +x /usr/local/bin/demo\n"
            'ENTRYPOINT ["demo"]\n'
        )
        df = _write_dockerfile(cwd_tmp, original)
        result = stage_binary_dockerfile(df)
        rewritten = result.read_text(encoding="utf-8")
        result.unlink()

        # Surrounding lines preserved verbatim.
        assert "RUN apt-get -o Acquire::Retries=5 update" in rewritten
        assert "RUN chmod +x /usr/local/bin/demo" in rewritten
        assert 'ENTRYPOINT ["demo"]' in rewritten

    def test_preserves_trailing_newline(self, cwd_tmp: Path) -> None:
        _write_dist_artefact(cwd_tmp / "dist", "demo", "amd64")
        df = _write_dockerfile(
            cwd_tmp,
            "FROM ubuntu:24.04\nCOPY demo /usr/local/bin/demo\n",
        )
        result = stage_binary_dockerfile(df)
        rewritten = result.read_text(encoding="utf-8")
        result.unlink()
        assert rewritten.endswith("\n")

    def test_temp_file_lives_under_cwd(self, cwd_tmp: Path) -> None:
        # buildx resolves Dockerfile-relative paths against cwd --
        # the temp file must be inside cwd.
        _write_dist_artefact(cwd_tmp / "dist", "demo", "amd64")
        df = _write_dockerfile(
            cwd_tmp,
            "FROM ubuntu:24.04\nCOPY demo /usr/local/bin/demo\n",
        )
        result = stage_binary_dockerfile(df)
        try:
            assert Path(os.path.commonpath([str(result), str(cwd_tmp)])) == cwd_tmp
        finally:
            result.unlink()


class TestFromFlagsAndCase:
    """A FROM with flags starts a stage, and instruction case does not matter."""

    def test_platform_flag_from_starts_a_new_stage(self, cwd_tmp: Path) -> None:
        _write_dist_artefact(cwd_tmp / "dist", "app", "amd64")
        df = _write_dockerfile(
            cwd_tmp,
            "FROM alpine AS fetch\n"
            "ARG TARGETARCH\n"
            "FROM --platform=$TARGETPLATFORM debian:stable AS final\n"
            "COPY app /usr/local/bin/app\n",
        )
        result = stage_binary_dockerfile(df)
        lines = result.read_text(encoding="utf-8").splitlines()
        result.unlink()
        assert lines[3:] == [
            "ARG TARGETARCH",
            "COPY dist/app-linux-${TARGETARCH} /usr/local/bin/app",
        ]

    def test_bare_platform_flag_without_alias(self, cwd_tmp: Path) -> None:
        _write_dist_artefact(cwd_tmp / "dist", "app", "amd64")
        df = _write_dockerfile(
            cwd_tmp,
            "ARG TARGETARCH\n"
            "FROM --platform=linux/amd64 debian:stable\n"
            "COPY app /usr/local/bin/app\n",
        )
        result = stage_binary_dockerfile(df)
        text = result.read_text(encoding="utf-8")
        result.unlink()
        assert text.count("ARG TARGETARCH") == 2

    def test_lowercase_copy_is_rewritten(self, cwd_tmp: Path) -> None:
        _write_dist_artefact(cwd_tmp / "dist", "app", "amd64")
        df = _write_dockerfile(
            cwd_tmp, "from debian:stable\ncopy app /usr/local/bin/app\n"
        )
        result = stage_binary_dockerfile(df)
        text = result.read_text(encoding="utf-8")
        result.unlink()
        assert "COPY dist/app-linux-${TARGETARCH} /usr/local/bin/app" in text


class TestRegressionFromBugSpec:
    """Reproduces the exact dfe-archiver pattern from the bug spec."""

    def test_dfe_archiver_dockerfile(self, cwd_tmp: Path) -> None:
        # Verbatim relevant lines from /projects/dfe-archiver/Dockerfile
        # -- the cause of the production bug.
        _write_dist_artefact(cwd_tmp / "dist", "dfe-archiver", "amd64")
        _write_dist_artefact(cwd_tmp / "dist", "dfe-archiver", "arm64")
        df = _write_dockerfile(
            cwd_tmp,
            "FROM ubuntu:24.04\n"
            "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
            "        ca-certificates curl \\\n"
            "    && rm -rf /var/lib/apt/lists/*\n"
            "COPY dfe-archiver /usr/local/bin/dfe-archiver\n"
            "RUN chmod +x /usr/local/bin/dfe-archiver\n"
            'ENTRYPOINT ["dfe-archiver"]\n',
        )
        result = stage_binary_dockerfile(df)
        rewritten = result.read_text(encoding="utf-8")
        result.unlink()

        # The COPY now resolves a real `dist/` artefact via TARGETARCH.
        assert (
            "COPY dist/dfe-archiver-linux-${TARGETARCH} /usr/local/bin/dfe-archiver"
            in rewritten
        )
        assert "ARG TARGETARCH" in rewritten
        # Bare COPY gone -- that was the bug.
        assert "\nCOPY dfe-archiver " not in rewritten


class TestAptRetries:
    """Every apt-get in the built Dockerfile retries transient fetch errors."""

    def test_every_apt_get_gets_retries(self, cwd_tmp: Path) -> None:
        # The ci-test-rust-app shape: two apt-get updates and two installs.
        df = _write_dockerfile(
            cwd_tmp,
            "FROM ubuntu:24.04\n"
            "RUN apt-get update \\\n"
            "    && apt-get install -y --no-install-recommends curl \\\n"
            "    && apt-get update \\\n"
            "    && apt-get install -y librdkafka1\n",
        )
        result = stage_binary_dockerfile(df)
        assert result != df
        text = result.read_text(encoding="utf-8")
        result.unlink()
        assert text.count("apt-get -o Acquire::Retries=5 ") == 4
        assert text.count("apt-get ") == 4

    def test_retries_reach_the_buildx_dockerfile(self, cwd_tmp: Path) -> None:
        df = _write_dockerfile(cwd_tmp, "FROM ubuntu:24.04\nRUN apt-get update\n")
        built = stage_binary_dockerfile(df)
        cmd = build.buildx_command(
            dockerfile_path=built,
            context=".",
            tags=["img:1"],
            platforms=["linux/arm64"],
            labels={},
            build_args=None,
            push=False,
            metadata_file=None,
        )
        passed = Path(cmd[cmd.index("--file") + 1])
        text = passed.read_text(encoding="utf-8")
        built.unlink()
        assert "Acquire::Retries=5" in text

    def test_existing_retries_setting_is_kept(self, cwd_tmp: Path) -> None:
        df = _write_dockerfile(
            cwd_tmp,
            "FROM ubuntu:24.04\nRUN apt-get -o Acquire::Retries=9 update\n",
        )
        assert stage_binary_dockerfile(df) == df

    def test_comment_lines_are_left_alone(self, cwd_tmp: Path) -> None:
        df = _write_dockerfile(
            cwd_tmp, "FROM ubuntu:24.04\n# apt-get update is slow\nRUN echo hi\n"
        )
        assert stage_binary_dockerfile(df) == df

    def test_dockerfile_without_apt_is_untouched(self, cwd_tmp: Path) -> None:
        df = _write_dockerfile(cwd_tmp, "FROM alpine\nRUN apk add curl\n")
        assert stage_binary_dockerfile(df) == df


class TestLicenceCopy:
    """Issue #345: every image carries the licence text at /licenses/LICENSE."""

    def test_repo_licence_is_appended_to_final_stage(self, cwd_tmp: Path) -> None:
        df = _write_dockerfile(
            cwd_tmp,
            "FROM rust:1.84 AS builder\nRUN cargo build\n"
            "FROM ubuntu:24.04\n"
            "COPY --from=builder /app/demo /usr/local/bin/demo\n"
            'ENTRYPOINT ["demo"]',
        )
        result = stage_binary_dockerfile(df, context=_REPO_ROOT)
        rewritten = result.read_text(encoding="utf-8")
        result.unlink()

        assert result != df
        lines = rewritten.splitlines()
        assert lines[-2] == 'ENTRYPOINT ["demo"]'
        assert lines[-1] == f"COPY LICENSE {LICENCE_DEST}"
        assert LICENCE_DEST == "/licenses/LICENSE"

    def test_licence_copy_rides_along_with_binary_rewrite(self, cwd_tmp: Path) -> None:
        _write_dist_artefact(cwd_tmp / "dist", "demo", "amd64")
        df = _write_dockerfile(
            cwd_tmp,
            "FROM ubuntu:24.04\nCOPY demo /usr/local/bin/demo\n",
        )
        result = stage_binary_dockerfile(df, context=_REPO_ROOT)
        rewritten = result.read_text(encoding="utf-8")
        result.unlink()

        assert rewritten == (
            "FROM ubuntu:24.04\n"
            "ARG TARGETARCH\n"
            "COPY dist/demo-linux-${TARGETARCH} /usr/local/bin/demo\n"
            "COPY LICENSE /licenses/LICENSE\n"
        )

    def test_no_licence_leaves_dockerfile_alone(self, cwd_tmp: Path) -> None:
        df = _write_dockerfile(cwd_tmp, "FROM scratch\n")
        assert find_licence_file(cwd_tmp) is None
        assert stage_binary_dockerfile(df, context=cwd_tmp) == df

    def test_fallback_order_and_dockerignore_skip(
        self, cwd_tmp: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Stand-in names: the lookup order and the ignore check are name-agnostic.
        monkeypatch.setattr(binary_stage, "_LICENCE_NAMES", ("first", "second.md"))
        (cwd_tmp / "second.md").write_text("text\n", encoding="utf-8")
        assert find_licence_file(cwd_tmp) == "second.md"

        (cwd_tmp / ".dockerignore").write_text("*.md\n", encoding="utf-8")
        with patch.object(binary_stage, "warn") as warned:
            assert find_licence_file(cwd_tmp) is None
        assert "second.md" in warned.call_args.args[0]

        (cwd_tmp / ".dockerignore").write_text("*.md\n!second.md\n", encoding="utf-8")
        assert find_licence_file(cwd_tmp) == "second.md"


class TestDockerignoreMatch:
    """A root-level file against Docker's .dockerignore rules."""

    @pytest.mark.parametrize(
        ("ignore", "excluded"),
        [
            ("", False),
            ("# LICENSE\n", False),
            ("LICENSE\n", True),
            ("/LICENSE\n", True),
            ("./LICENSE\n", True),
            ("LICEN?E\n", True),
            ("*\n", True),
            ("**\n", True),
            ("**/LICENSE\n", True),
            ("*\n!LICENSE\n", False),
            ("!LICENSE\nLICENSE\n", True),
            ("docs/LICENSE\n", False),
            ("*.md\n", False),
            ("target/\n.git/\n", False),
        ],
    )
    def test_patterns(self, tmp_path: Path, ignore: str, excluded: bool) -> None:
        (tmp_path / ".dockerignore").write_text(ignore, encoding="utf-8")
        assert binary_stage._dockerignore_excludes(tmp_path, "LICENSE") is excluded

    def test_no_dockerignore(self, tmp_path: Path) -> None:
        assert binary_stage._dockerignore_excludes(tmp_path, "LICENSE") is False


def test_dispatch_build_hands_buildx_the_licence_copy(
    cwd_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The container stage passes its build context to the rewrite."""
    seen: dict[str, str] = {}

    def record(**kwargs: object) -> int:
        dockerfile = kwargs["dockerfile_path"]
        assert isinstance(dockerfile, Path)
        seen["dockerfile"] = dockerfile.read_text(encoding="utf-8")
        seen["context"] = str(kwargs["context"])
        return 0

    df = _write_dockerfile(cwd_tmp, "FROM scratch\n")
    monkeypatch.setattr(stage, "build_and_push", record)
    monkeypatch.setattr(stage, "_read_version", lambda: "1.2.3")
    monkeypatch.setattr(stage, "_read_sha", lambda: "deadbeef")
    with patch("hyperi_ci.description_source.github_description", return_value=None):
        rc = stage._dispatch_build(
            dockerfile_path=df,
            container_cfg={"context": str(_REPO_ROOT)},
            config=CIConfig(_raw={}),
            org=OrgConfig(),
            registry_bases=["ghcr.io/hyperi-io"],
            push_mode="release",
            binary_backed=False,
        )

    assert rc == 0
    assert seen["context"] == str(_REPO_ROOT)
    assert seen["dockerfile"] == "FROM scratch\nCOPY LICENSE /licenses/LICENSE\n"
    # The rewritten Dockerfile is a temp file the stage removes afterwards.
    assert list(cwd_tmp.glob("*.Dockerfile")) == []
