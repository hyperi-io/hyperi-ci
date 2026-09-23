# Project:   HyperI CI
# File:      tests/unit/test_compose_pins.py
# Purpose:   Tests for the compose image-pin gate (Path C)
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for hyperi_ci.quality.compose_pins - the compose image-pin GATE.

Real compose files in tmp_path throughout: the whole check is text-in,
findings-out, so a mock would only test the mock.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.quality import compose_pins

_DIGEST = "@sha256:" + "a" * 64


def _cfg(**quality: object) -> CIConfig:
    return CIConfig(_raw={"quality": quality} if quality else {})


def _compose(tmp_path: Path, *images: str) -> Path:
    body = ["services:"]
    for index, image in enumerate(images):
        body += [f"  svc{index}:", f"    image: {image}"]
    path = tmp_path / "docker-compose.yml"
    path.write_text("\n".join(body) + "\n", encoding="utf-8", newline="\n")
    return path


class TestClassify:
    @pytest.mark.parametrize(
        "reference",
        [
            "nginx",
            "nginx:latest",
            "ghcr.io/org/app:${TAG:-latest}",
            "ghcr.io/org/app:${TAG}",
            "registry.example.io:8443/org/app",
        ],
    )
    def test_floating_is_an_error(self, reference: str) -> None:
        verdict = compose_pins.classify(reference)
        assert verdict is not None
        assert verdict[0] == "error"

    @pytest.mark.parametrize(
        "reference",
        [
            f"nginx{_DIGEST}",
            f"nginx:1.27{_DIGEST}",
            "ghcr.io/org/app:${TAG:?set TAG from the release}",
            "${APP_IMAGE:?pin the image}",
            "ghcr.io/org/app:${TAG:-1.2.3" + _DIGEST + "}",
        ],
    )
    def test_pinned_is_clean(self, reference: str) -> None:
        assert compose_pins.classify(reference) is None

    @pytest.mark.parametrize(
        "reference",
        [
            "nginx:1.27",
            "registry.example.io:8443/org/app:1.2.3",
            "ghcr.io/org/app:${TAG:-1.2.3}",
        ],
    )
    def test_tag_without_digest_is_a_notice(self, reference: str) -> None:
        verdict = compose_pins.classify(reference)
        assert verdict is not None
        assert verdict[0] == "notice"


class TestResolveUnset:
    def test_mandatory_key_aborts(self) -> None:
        assert compose_pins.resolve_unset("app:${TAG:?pin me}") is None

    def test_default_is_substituted(self) -> None:
        assert compose_pins.resolve_unset("app:${TAG:-1.2}") == "app:1.2"

    def test_bare_key_resolves_to_nothing(self) -> None:
        assert compose_pins.resolve_unset("app:${TAG}") == "app:"


class TestScan:
    def test_trailing_comment_is_not_part_of_the_reference(
        self, tmp_path: Path
    ) -> None:
        path = _compose(tmp_path, "clickhouse/clickhouse-server:24.8 # pin an LTS")
        found = compose_pins.scan(path)
        assert [f.level for f in found] == ["notice"]
        assert "24.8" in found[0].message
        assert "pin an LTS" not in found[0].message

    def test_line_number_points_at_the_image(self, tmp_path: Path) -> None:
        path = _compose(tmp_path, "nginx:1.27" + _DIGEST, "redis:latest")
        found = compose_pins.scan(path)
        assert [(f.level, f.line) for f in found] == [("error", 5)]

    def test_quoted_reference_is_read(self, tmp_path: Path) -> None:
        path = _compose(tmp_path, '"redis:latest"')
        assert [f.level for f in compose_pins.scan(path)] == ["error"]


class TestRun:
    def test_unpinned_stack_fails(self, tmp_path: Path) -> None:
        path = _compose(tmp_path, "nginx")
        assert compose_pins.run([path], _cfg()) == 1

    def test_pinned_stack_passes(self, tmp_path: Path) -> None:
        path = _compose(tmp_path, "nginx" + _DIGEST)
        assert compose_pins.run([path], _cfg()) == 0

    def test_notices_alone_do_not_gate(self, tmp_path: Path) -> None:
        path = _compose(tmp_path, "nginx:1.27")
        assert compose_pins.run([path], _cfg()) == 0

    def test_warn_mode_does_not_gate(self, tmp_path: Path) -> None:
        path = _compose(tmp_path, "nginx")
        assert compose_pins.run([path], _cfg(compose_pins="warn")) == 0

    def test_disabled_skips(self, tmp_path: Path) -> None:
        path = _compose(tmp_path, "nginx")
        assert compose_pins.run([path], _cfg(compose_pins="disabled")) == 0

    def test_no_files_is_not_a_failure(self) -> None:
        assert compose_pins.run([], _cfg()) == 0
