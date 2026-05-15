# Project:   HyperI CI
# File:      tests/deployment/overlay/test_dockerfile_anchors.py
# Purpose:   Unit tests for DockerfileAnchorResolver
#
# License:   Proprietary - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Unit tests for ``DockerfileAnchorResolver``.

Synthetic base Dockerfiles cover each anchor:
  * after-base-image / after-base-deps / after-app-binary
  * before-user / before-healthcheck / before-entrypoint / end-of-image
  * single overlay, multiple overlays at same anchor, cross-anchor
  * missing-anchor errors with candidate list
  * binary-name disambiguation for after-app-binary
"""

from __future__ import annotations

import pytest

from hyperi_ci.deployment.overlay.anchors.dockerfile import (
    DockerfileAnchorResolver,
)
from hyperi_ci.deployment.overlay.errors import AnchorNotFound
from hyperi_ci.deployment.overlay.model import Overlay


# A representative base Dockerfile in the shape rustlib/pylib generators emit.
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

    def test_after_base_image_lands_just_after_FROM(self) -> None:
        resolver = DockerfileAnchorResolver()
        overlay = Overlay(
            anchor="after-base-image", content="# right-after-FROM\n"
        )
        out = resolver.splice(_BASE, [overlay])
        lines = out.splitlines()
        # FROM should be line 0; overlay should be one of the next few lines.
        from_idx = next(i for i, l in enumerate(lines) if l.startswith("FROM"))
        ovl_idx = next(
            i for i, l in enumerate(lines) if l.startswith("# right-after-FROM")
        )
        assert ovl_idx == from_idx + 1

    def test_before_healthcheck(self) -> None:
        resolver = DockerfileAnchorResolver()
        overlay = Overlay(anchor="before-healthcheck", content="# pre-hc\n")
        out = resolver.splice(_BASE, [overlay])
        assert out.index("# pre-hc") < out.index("HEALTHCHECK")

    def test_before_entrypoint(self) -> None:
        resolver = DockerfileAnchorResolver()
        overlay = Overlay(
            anchor="before-entrypoint", content="# right-before-ep\n"
        )
        out = resolver.splice(_BASE, [overlay])
        assert out.index("# right-before-ep") < out.index("ENTRYPOINT")

    def test_end_of_image_alias_of_before_entrypoint(self) -> None:
        resolver = DockerfileAnchorResolver()
        overlay = Overlay(anchor="end-of-image", content="# eoi\n")
        out = resolver.splice(_BASE, [overlay])
        assert out.index("# eoi") < out.index("ENTRYPOINT")


class TestPackageManagerAnchor:
    def test_after_base_deps_lands_after_apt_get(self) -> None:
        resolver = DockerfileAnchorResolver()
        overlay = Overlay(
            anchor="after-base-deps", content="# post-apt\n"
        )
        out = resolver.splice(_BASE, [overlay])
        # Overlay must be after the apt-get line and before COPY.
        assert out.index("# post-apt") > out.index("apt-get update")
        assert out.index("# post-apt") < out.index("COPY myapp")

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
