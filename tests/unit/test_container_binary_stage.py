# Project:   HyperI CI
# File:      tests/unit/test_container_binary_stage.py
# Purpose:   Tests for binary-staging Dockerfile rewriter
#
# License:   FSL-1.1-ALv2
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for `hyperi_ci.container.binary_stage.stage_binary_dockerfile`.

Covers the bare-`COPY <app>` rewrite logic that fixes the Container
stage binary-placement bug. See:
docs/superpowers/specs/2026-05-01-container-stage-binary-placement-bug.md
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hyperi_ci.container.binary_stage import stage_binary_dockerfile


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
            "FROM ubuntu:24.04\nRUN apt-get update\n",
        )
        result = stage_binary_dockerfile(df)
        assert result == df

    def test_already_parameterised_copy(self, cwd_tmp: Path) -> None:
        # ci-test-rust-app / dfe-loader pattern — already uses TARGETARCH.
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
        # Multi-stage `COPY --from=builder` shouldn't be rewritten —
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
        # `COPY config.yaml /etc/app/` — config files, not binaries.
        # No dist/config.yaml-linux-amd64 → don't rewrite.
        df = _write_dockerfile(
            cwd_tmp,
            "FROM ubuntu:24.04\nCOPY config.yaml /etc/app/config.yaml\n",
        )
        result = stage_binary_dockerfile(df)
        assert result == df

    def test_copy_with_path_in_source(self, cwd_tmp: Path) -> None:
        # Path-form sources like `COPY src/ /app/` are skipped — only
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
        # fire — TARGETARCH substitution still resolves correctly when
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
        # possible — e.g. main app + sidecar), one ARG TARGETARCH suffices.
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
        assert "RUN apt-get update && apt-get install -y curl" in rewritten
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
        # buildx resolves Dockerfile-relative paths against cwd —
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


class TestRegressionFromBugSpec:
    """Reproduces the exact dfe-archiver pattern from the bug spec."""

    def test_dfe_archiver_dockerfile(self, cwd_tmp: Path) -> None:
        # Verbatim relevant lines from /projects/dfe-archiver/Dockerfile
        # — the cause of the production bug.
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
        # Bare COPY gone — that was the bug.
        assert "\nCOPY dfe-archiver " not in rewritten
