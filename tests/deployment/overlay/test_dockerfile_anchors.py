# Project:   HyperI CI
# File:      tests/deployment/overlay/test_dockerfile_anchors.py
# Purpose:   Unit tests for DockerfileAnchorResolver
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Unit tests for ``DockerfileAnchorResolver``.

Synthetic base Dockerfiles cover each anchor:
  * after-base-image / after-base-deps / after-app-binary
  * before-user / before-healthcheck / before-entrypoint / end-of-image
  * single overlay, multiple overlays at same anchor, cross-anchor
  * missing-anchor errors with candidate list
  * binary-name disambiguation for after-app-binary
  * logical-instruction boundaries: continuations, heredocs, and the
    Dockerfiles hyperi-ci's own generators emit
"""

import pytest

from hyperi_ci.apt_retry import apt_update_sh
from hyperi_ci.container.compose import compose_contract_dockerfile
from hyperi_ci.container.manifest import ContainerManifest
from hyperi_ci.container.templates import render_python_template
from hyperi_ci.deployment.overlay.anchors.dockerfile import (
    DockerfileAnchorResolver,
)
from hyperi_ci.deployment.overlay.errors import AnchorNotFound
from hyperi_ci.deployment.overlay.model import Overlay

_KEYWORDS = frozenset(
    {
        "ADD",
        "ARG",
        "CMD",
        "COPY",
        "ENTRYPOINT",
        "ENV",
        "EXPOSE",
        "FROM",
        "HEALTHCHECK",
        "LABEL",
        "ONBUILD",
        "RUN",
        "SHELL",
        "STOPSIGNAL",
        "USER",
        "VOLUME",
        "WORKDIR",
    }
)


def _assert_whole_instructions(text: str) -> None:
    """Fail unless every line starts an instruction or continues one ending in ``\\``.

    Blank and comment lines are skipped the way the Dockerfile parser skips
    them, including inside a continuation.
    """
    continuing = False
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not continuing:
            keyword = stripped.split(maxsplit=1)[0].upper()
            assert keyword in _KEYWORDS, (
                f"line {number} is not an instruction: {line!r}"
            )
        continuing = stripped.endswith("\\")


def _previous_code_line(lines: list[str], idx: int) -> str:
    """Return the nearest line above ``idx`` that is neither blank nor a comment."""
    for line in reversed(lines[:idx]):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return line
    return ""


def _assert_starts_instruction(out: str, marker: str) -> list[str]:
    """Assert the ``marker`` line is not swallowed by a continuation above it."""
    lines = out.splitlines()
    idx = lines.index(marker)
    assert not _previous_code_line(lines, idx).rstrip().endswith("\\"), out
    _assert_whole_instructions(out)
    return lines


def _manifest(*, runtime_packages: list[str]) -> ContainerManifest:
    return ContainerManifest(
        base_image="ubuntu:24.04",
        binary_name="myapp",
        runtime_packages=runtime_packages,
        expose_ports=[8080],
        health_check={"path": "/healthz", "port": 8080},
        entrypoint=["myapp"],
        cmd=["run"],
    )


# A representative base Dockerfile in the shape scalo-rs/scalo-py generators emit.
_BASE = """\
FROM ubuntu:24.04

LABEL io.hyperi.profile="production"

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl \\
    && rm -rf /var/lib/apt/lists/*

COPY myapp /usr/local/bin/myapp
RUN chmod +x /usr/local/bin/myapp

RUN userdel -r ubuntu && useradd --create-home --uid 1000 appuser

USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s CMD curl -sf http://localhost:8080/healthz || exit 1

ENTRYPOINT ["myapp"]
CMD ["--config", "/etc/cfg.yaml"]
"""


class TestSimpleAnchors:
    def test_before_user_inserts_before_user_line(self) -> None:
        resolver = DockerfileAnchorResolver(binary_name="myapp")
        overlay = Overlay(
            anchor="before-user", content="# overlay-here\nRUN echo overlay\n"
        )
        out = resolver.splice(_BASE, [overlay])
        # Find both lines and confirm overlay precedes USER.
        idx_overlay = out.index("# overlay-here")
        idx_user = out.index("USER appuser")
        assert idx_overlay < idx_user

    def test_after_base_image_lands_just_after_from_directive(self) -> None:
        resolver = DockerfileAnchorResolver()
        overlay = Overlay(anchor="after-base-image", content="# right-after-FROM\n")
        out = resolver.splice(_BASE, [overlay])
        lines = out.splitlines()
        # FROM should be line 0; overlay should be one of the next few lines.
        from_idx = next(i for i, line in enumerate(lines) if line.startswith("FROM"))
        ovl_idx = next(
            i for i, line in enumerate(lines) if line.startswith("# right-after-FROM")
        )
        assert ovl_idx == from_idx + 1

    def test_before_healthcheck(self) -> None:
        resolver = DockerfileAnchorResolver()
        overlay = Overlay(anchor="before-healthcheck", content="# pre-hc\n")
        out = resolver.splice(_BASE, [overlay])
        assert out.index("# pre-hc") < out.index("HEALTHCHECK")

    def test_before_entrypoint(self) -> None:
        resolver = DockerfileAnchorResolver()
        overlay = Overlay(anchor="before-entrypoint", content="# right-before-ep\n")
        out = resolver.splice(_BASE, [overlay])
        assert out.index("# right-before-ep") < out.index("ENTRYPOINT")

    @pytest.mark.parametrize(
        "base",
        [
            pytest.param(render_python_template(), id="python-template"),
            pytest.param(
                compose_contract_dockerfile(_manifest(runtime_packages=[])),
                id="contract",
            ),
        ],
    )
    def test_before_entrypoint_skips_healthcheck_cmd_continuation(
        self, base: str
    ) -> None:
        assert "\\\n    CMD curl" in base
        resolver = DockerfileAnchorResolver()
        overlay = Overlay(anchor="before-entrypoint", content="RUN echo pre-ep\n")
        out = resolver.splice(base, [overlay])
        lines = _assert_starts_instruction(out, "RUN echo pre-ep")
        assert lines[lines.index("RUN echo pre-ep") + 1].startswith("ENTRYPOINT")

    def test_end_of_image_alias_of_before_entrypoint(self) -> None:
        resolver = DockerfileAnchorResolver()
        overlay = Overlay(anchor="end-of-image", content="# eoi\n")
        out = resolver.splice(_BASE, [overlay])
        assert out.index("# eoi") < out.index("ENTRYPOINT")


class TestPackageManagerAnchor:
    def test_after_base_deps_lands_after_whole_multiline_run(self) -> None:
        resolver = DockerfileAnchorResolver()
        overlay = Overlay(anchor="after-base-deps", content="RUN echo post-apt\n")
        out = resolver.splice(_BASE, [overlay])
        lines = _assert_starts_instruction(out, "RUN echo post-apt")
        idx = lines.index("RUN echo post-apt")
        assert lines[idx - 1] == "    && rm -rf /var/lib/apt/lists/*"
        assert out.index("RUN echo post-apt") < out.index("COPY myapp")

    def test_contract_runtime_apt_retry_loop_is_anchored(self) -> None:
        base = compose_contract_dockerfile(_manifest(runtime_packages=["libssl3"]))
        assert f"RUN {apt_update_sh()} \\" in base.splitlines()
        resolver = DockerfileAnchorResolver()
        overlay = Overlay(anchor="after-base-deps", content="RUN echo post-deps\n")
        out = resolver.splice(base, [overlay])
        lines = _assert_starts_instruction(out, "RUN echo post-deps")
        idx = lines.index("RUN echo post-deps")
        assert lines[idx - 1].endswith("libssl3 && rm -rf /var/lib/apt/lists/*")
        assert out.index("AS runtime") < out.index("RUN echo post-deps")
        assert out.index("RUN echo post-deps") < out.index("WORKDIR /app\nCOPY")

    def test_python_template_apt_retry_loop_is_anchored(self) -> None:
        base = render_python_template()
        assert f"RUN {apt_update_sh()} \\" in base.splitlines()
        resolver = DockerfileAnchorResolver()
        overlay = Overlay(anchor="after-base-deps", content="RUN echo post-deps\n")
        out = resolver.splice(base, [overlay])
        lines = _assert_starts_instruction(out, "RUN echo post-deps")
        idx = lines.index("RUN echo post-deps")
        assert (
            lines[idx - 1] == "    ca-certificates curl && rm -rf /var/lib/apt/lists/*"
        )

    def test_apt_in_an_earlier_build_stage_does_not_count(self) -> None:
        # The chef stage installs curl with apt; the runtime stage installs nothing.
        base = compose_contract_dockerfile(_manifest(runtime_packages=[]))
        assert "apt-get" in base
        resolver = DockerfileAnchorResolver()
        overlay = Overlay(anchor="after-base-deps", content="RUN echo x\n")
        with pytest.raises(AnchorNotFound):
            resolver.splice(base, [overlay])

    def test_apt_path_without_package_manager_call_does_not_count(self) -> None:
        base = "FROM ubuntu:24.04\nRUN rm -rf /var/lib/apt/lists/*\nUSER app\n"
        resolver = DockerfileAnchorResolver()
        overlay = Overlay(anchor="after-base-deps", content="RUN echo x\n")
        with pytest.raises(AnchorNotFound):
            resolver.splice(base, [overlay])

    def test_heredoc_run_is_anchored_after_its_delimiter(self) -> None:
        base = (
            "FROM ubuntu:24.04\n"
            "RUN <<EOF\n"
            "apt-get update\n"
            "apt-get install -y curl\n"
            "EOF\n"
            "USER app\n"
        )
        resolver = DockerfileAnchorResolver()
        overlay = Overlay(anchor="after-base-deps", content="RUN echo post-deps\n")
        out = resolver.splice(base, [overlay])
        assert out == base.replace("\nEOF\n", "\nEOF\nRUN echo post-deps\n")

    def test_comment_inside_continuation_does_not_end_the_run(self) -> None:
        base = (
            "FROM ubuntu:24.04\n"
            "RUN apt-get update \\\n"
            "    # the install follows\n"
            "    && apt-get install -y curl\n"
            "USER app\n"
        )
        resolver = DockerfileAnchorResolver()
        overlay = Overlay(anchor="after-base-deps", content="RUN echo post-deps\n")
        out = resolver.splice(base, [overlay])
        lines = _assert_starts_instruction(out, "RUN echo post-deps")
        assert lines[lines.index("RUN echo post-deps") - 1].endswith("curl")

    def test_after_anchor_on_last_line_without_trailing_newline(self) -> None:
        base = "FROM alpine:3.20\nRUN apk add curl"
        resolver = DockerfileAnchorResolver()
        overlay = Overlay(anchor="after-base-deps", content="RUN echo x\n")
        out = resolver.splice(base, [overlay])
        assert out == "FROM alpine:3.20\nRUN apk add curl\nRUN echo x\n"

    def test_after_base_deps_works_with_dnf(self) -> None:
        base = "FROM rockylinux:9\nRUN dnf install -y curl\nUSER appuser\n"
        resolver = DockerfileAnchorResolver()
        overlay = Overlay(anchor="after-base-deps", content="# post-dnf\n")
        out = resolver.splice(base, [overlay])
        assert out.index("# post-dnf") > out.index("dnf install")
        assert out.index("# post-dnf") < out.index("USER appuser")

    def test_after_base_deps_works_with_apk(self) -> None:
        base = "FROM alpine:3.20\nRUN apk add curl\nUSER appuser\n"
        resolver = DockerfileAnchorResolver()
        overlay = Overlay(anchor="after-base-deps", content="# post-apk\n")
        out = resolver.splice(base, [overlay])
        assert out.index("# post-apk") > out.index("apk add")

    def test_missing_pkg_manager_raises(self) -> None:
        base = "FROM scratch\nUSER appuser\n"
        resolver = DockerfileAnchorResolver()
        overlay = Overlay(anchor="after-base-deps", content="# x\n")
        with pytest.raises(AnchorNotFound) as exc:
            resolver.splice(base, [overlay])
        assert exc.value.anchor == "after-base-deps"


class TestAfterAppBinary:
    def test_after_app_binary_finds_named_copy(self) -> None:
        resolver = DockerfileAnchorResolver(binary_name="myapp")
        overlay = Overlay(anchor="after-app-binary", content="# post-app\n")
        out = resolver.splice(_BASE, [overlay])
        assert out.index("# post-app") > out.index("COPY myapp")

    def test_unknown_binary_name_raises(self) -> None:
        resolver = DockerfileAnchorResolver(binary_name="not-here")
        overlay = Overlay(anchor="after-app-binary", content="# x\n")
        with pytest.raises(AnchorNotFound):
            resolver.splice(_BASE, [overlay])

    def test_no_binary_name_means_anchor_unavailable(self) -> None:
        resolver = DockerfileAnchorResolver()  # no name set
        overlay = Overlay(anchor="after-app-binary", content="# x\n")
        with pytest.raises(AnchorNotFound):
            resolver.splice(_BASE, [overlay])


class TestMultipleOverlaysAtSameAnchor:
    def test_declaration_order_preserved(self) -> None:
        resolver = DockerfileAnchorResolver()
        a = Overlay(anchor="before-user", content="# overlay-A\nRUN A\n")
        b = Overlay(anchor="before-user", content="# overlay-B\nRUN B\n")
        c = Overlay(anchor="before-user", content="# overlay-C\nRUN C\n")
        out = resolver.splice(_BASE, [a, b, c])
        # All three appear, in declared order, before USER.
        idx_a = out.index("# overlay-A")
        idx_b = out.index("# overlay-B")
        idx_c = out.index("# overlay-C")
        idx_user = out.index("USER appuser")
        assert idx_a < idx_b < idx_c < idx_user

    def test_cross_anchor_independent_splices(self) -> None:
        resolver = DockerfileAnchorResolver()
        before_user = Overlay(anchor="before-user", content="# bu\n")
        after_image = Overlay(anchor="after-base-image", content="# abi\n")
        out = resolver.splice(_BASE, [before_user, after_image])
        # after-base-image lands near top, before-user lands near bottom
        assert out.index("# abi") < out.index("# bu")

    def test_after_and_before_at_the_same_point_keep_file_order(self) -> None:
        base = "FROM ubuntu:24.04\nUSER app\n"
        resolver = DockerfileAnchorResolver()
        before_user = Overlay(anchor="before-user", content="# bu\n")
        after_image = Overlay(anchor="after-base-image", content="# abi\n")
        out = resolver.splice(base, [before_user, after_image])
        assert out == "FROM ubuntu:24.04\n# abi\n# bu\nUSER app\n"


class TestErrorReporting:
    def test_missing_anchor_lists_candidates(self) -> None:
        resolver = DockerfileAnchorResolver(binary_name="myapp")
        overlay = Overlay(anchor="not-a-real-anchor", content="# x\n")
        with pytest.raises(AnchorNotFound) as exc:
            resolver.splice(_BASE, [overlay])
        assert exc.value.anchor == "not-a-real-anchor"
        assert "before-user" in exc.value.candidates
        # known_anchors is sorted, deterministic
        assert exc.value.candidates == resolver.known_anchors

    def test_known_anchors_includes_after_app_binary_when_named(self) -> None:
        resolver = DockerfileAnchorResolver(binary_name="myapp")
        assert "after-app-binary" in resolver.known_anchors

    def test_known_anchors_excludes_after_app_binary_when_unnamed(self) -> None:
        resolver = DockerfileAnchorResolver()
        assert "after-app-binary" not in resolver.known_anchors


class TestEmptyOverlayList:
    def test_empty_input_returns_base_unchanged(self) -> None:
        resolver = DockerfileAnchorResolver()
        assert resolver.splice(_BASE, []) == _BASE
