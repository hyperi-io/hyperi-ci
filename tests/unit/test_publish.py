# Project:   HyperI CI
# File:      tests/unit/test_publish.py
# Purpose:   Tests for publish handlers (subprocess calls mocked)
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from unittest.mock import patch

from hyperi_ci.config import CIConfig


def _make_config(
    publish_target: str = "internal",
) -> CIConfig:
    """Create a CIConfig with publish destinations."""
    raw = {
        "publish": {
            "target": publish_target,
            "destinations_internal": {
                "python": "jfrog-pypi",
                "npm": "jfrog-npm",
                "cargo": "jfrog-cargo",
                "go": "jfrog-go",
                "binaries": "jfrog-generic",
            },
            "destinations_oss": {
                "python": "pypi",
                "npm": "npmjs",
                "cargo": "crates-io",
                "go": "go-proxy",
                "binaries": "github-releases",
            },
        },
    }
    return CIConfig(
        publish_target=publish_target,
        _raw=raw,
    )


class TestPythonPublish:
    """Python publish handler tests."""

    def test_publish_pypi(self) -> None:
        from hyperi_ci.languages.python.publish import run

        config = _make_config("oss")
        with (
            patch("hyperi_ci.languages.python.publish.subprocess.run") as mock_run,
            patch.dict("os.environ", {"PYPI_TOKEN": "tok-123"}),
        ):
            mock_run.return_value.returncode = 0
            rc = run(config)
            assert rc == 0
            cmd = mock_run.call_args[0][0]
            assert cmd[0] == "uv"
            assert "publish" in cmd

    def test_publish_jfrog(self) -> None:
        from hyperi_ci.languages.python.publish import run

        config = _make_config("internal")
        with (
            patch("hyperi_ci.languages.python.publish.subprocess.run") as mock_run,
            patch.dict("os.environ", {"JFROG_TOKEN": "jf-tok"}),
        ):
            mock_run.return_value.returncode = 0
            rc = run(config)
            assert rc == 0
            cmd = mock_run.call_args[0][0]
            assert "--publish-url" in cmd

    def test_jfrog_requires_token(self) -> None:
        from hyperi_ci.languages.python.publish import run

        config = _make_config("internal")
        with patch.dict("os.environ", {}, clear=True):
            rc = run(config)
            assert rc == 1

    def test_no_destinations(self) -> None:
        from hyperi_ci.languages.python.publish import run

        config = CIConfig(publish_target="internal", _raw={})
        rc = run(config)
        assert rc == 0


class TestRustPublish:
    """Rust publish handler tests."""

    def test_publish_crates_io(self) -> None:
        from hyperi_ci.languages.rust.publish import run

        config = _make_config("oss")
        with (
            patch("hyperi_ci.languages.rust.publish.subprocess.run") as mock_run,
            patch.dict("os.environ", {"CARGO_REGISTRY_TOKEN": "crt-tok"}),
        ):
            mock_run.return_value.returncode = 0
            rc = run(config)
            assert rc == 0
            cmd = mock_run.call_args[0][0]
            assert cmd[0] == "cargo"
            assert "publish" in cmd

    def test_crates_io_requires_token(self) -> None:
        from hyperi_ci.languages.rust.publish import run

        config = _make_config("oss")
        with patch.dict("os.environ", {}, clear=True):
            rc = run(config)
            assert rc == 1


class TestTypescriptPublish:
    """TypeScript publish handler tests."""

    def test_publish_npm(self) -> None:
        from hyperi_ci.languages.typescript.publish import run

        config = _make_config("oss")
        with (
            patch(
                "hyperi_ci.languages.typescript.publish.subprocess.run",
            ) as mock_run,
            patch.dict("os.environ", {"NPM_TOKEN": "npm-tok"}),
        ):
            mock_run.return_value.returncode = 0
            rc = run(config)
            assert rc == 0
            cmd = mock_run.call_args[0][0]
            assert cmd[0] == "npm"
            assert "publish" in cmd

    def test_npm_requires_token(self) -> None:
        from hyperi_ci.languages.typescript.publish import run

        config = _make_config("oss")
        with patch.dict("os.environ", {}, clear=True):
            rc = run(config)
            assert rc == 1


class TestGolangPublish:
    """Golang publish handler tests."""

    def test_publish_go_proxy(self) -> None:
        from hyperi_ci.languages.golang.publish import run

        config = _make_config("oss")
        with patch(
            "hyperi_ci.languages.golang.publish.subprocess.run",
        ) as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "github.com/example/pkg\n"
            rc = run(config)
            assert rc == 0

    def test_no_destinations(self) -> None:
        from hyperi_ci.languages.golang.publish import run

        config = CIConfig(publish_target="internal", _raw={})
        rc = run(config)
        assert rc == 0
