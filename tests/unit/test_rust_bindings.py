# Project:   HyperI CI
# File:      tests/unit/test_rust_bindings.py
# Purpose:   Tests for maturin binding discovery, wheel argv, stamping and publish
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from pathlib import Path
from typing import Any

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.languages.python import release as py_release
from hyperi_ci.languages.rust import bindings
from hyperi_ci.stamp import StampError
from hyperi_ci.versions import tool_version

MATURIN = '[build-system]\nbuild-backend = "maturin"\n'
BINDING_CARGO = '[package]\nname = "x-py"\nversion = "0.1.0"\n\n[workspace]\n'


def _config(python_bindings: dict[str, Any] | None = None) -> CIConfig:
    raw = {"build": {"python_bindings": python_bindings}} if python_bindings else {}
    return CIConfig(_raw=raw)


def _binding(root: Path, name: str, pyproject: str = MATURIN) -> Path:
    path = root / name
    path.mkdir()
    (path / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    (path / "Cargo.toml").write_text(BINDING_CARGO, encoding="utf-8")
    return path


class TestFindBindings:
    def test_finds_maturin_projects_one_level_down_only(self, tmp_path: Path) -> None:
        _binding(tmp_path, "b-py", MATURIN + '[project]\nrequires-python = ">=3.14"\n')
        _binding(tmp_path, "a-py")
        _binding(
            tmp_path, "docs", '[build-system]\nbuild-backend = "hatchling.build"\n'
        )
        (tmp_path / "deep" / "x-py").mkdir(parents=True)
        (tmp_path / "deep" / "x-py" / "pyproject.toml").write_text(MATURIN)
        (tmp_path / "broken").mkdir()
        (tmp_path / "broken" / "pyproject.toml").write_text("not = [toml")

        found = bindings.find_bindings(_config(), tmp_path)

        assert [(b.path.name, b.requires_python) for b in found] == [
            ("a-py", ""),
            ("b-py", ">=3.14"),
        ]

    @pytest.mark.parametrize(
        ("setting", "expected"),
        [
            ({"enabled": False}, []),
            ({"paths": ["b-py"]}, ["b-py"]),
            ({"paths": []}, ["a-py", "b-py"]),
        ],
    )
    def test_config_turns_off_or_names_bindings(
        self, tmp_path: Path, setting: dict[str, Any], expected: list[str]
    ) -> None:
        _binding(tmp_path, "a-py")
        _binding(tmp_path, "b-py")

        found = bindings.find_bindings(_config(setting), tmp_path)

        assert [b.path.name for b in found] == expected

    @pytest.mark.parametrize(
        ("paths", "message"),
        [
            (["docs"], "has no pyproject.toml built by maturin"),
            (["missing"], "has no pyproject.toml built by maturin"),
            (["../elsewhere"], "must be relative to the repo root"),
            ("a-py", "must be a list of paths"),
        ],
    )
    def test_a_bad_paths_entry_is_refused(
        self, tmp_path: Path, paths: object, message: str
    ) -> None:
        _binding(tmp_path, "a-py")
        _binding(
            tmp_path, "docs", '[build-system]\nbuild-backend = "hatchling.build"\n'
        )

        with pytest.raises(ValueError, match=message):
            bindings.find_bindings(_config({"paths": paths}), tmp_path)


class TestWheels:
    @pytest.mark.parametrize(
        ("target", "expected"),
        [
            (
                "x86_64-unknown-linux-gnu",
                [
                    ("x86_64-unknown-linux-gnu", "manylinux_2_28"),
                    ("x86_64-unknown-linux-musl", "musllinux_1_2"),
                ],
            ),
            (
                "aarch64-unknown-linux-gnu",
                [
                    ("aarch64-unknown-linux-gnu", "manylinux_2_28"),
                    ("aarch64-unknown-linux-musl", "musllinux_1_2"),
                ],
            ),
            ("aarch64-apple-darwin", [("aarch64-apple-darwin", "")]),
            ("x86_64-pc-windows-msvc", [("x86_64-pc-windows-msvc", "")]),
        ],
    )
    def test_each_leg_builds_its_own_arch(
        self, target: str, expected: list[tuple[str, str]]
    ) -> None:
        specs = bindings.wheel_specs(target)

        assert [(s.target, s.compatibility) for s in specs] == expected

    def test_a_linux_release_wheel_uses_zig_and_the_pinned_maturin(
        self, tmp_path: Path
    ) -> None:
        binding = bindings.Binding(tmp_path / "x-py", "")
        spec = bindings.WheelSpec("x86_64-unknown-linux-musl", "musllinux_1_2")

        argv = bindings.build_argv(binding, spec, tmp_path / "out", release=True)

        assert argv[:4] == [
            "uvx",
            "--from",
            f"maturin[zig]=={tool_version('maturin')}",
            "maturin",
        ]
        assert argv[argv.index("--out") + 1] == str(tmp_path / "out")
        assert argv[argv.index("--target") + 1] == "x86_64-unknown-linux-musl"
        assert argv[argv.index("--manifest-path") + 1] == str(
            tmp_path / "x-py" / "Cargo.toml"
        )
        assert argv[-4:] == ["--release", "--zig", "--compatibility", "musllinux_1_2"]

    def test_a_native_debug_wheel_has_no_zig_and_no_release(
        self, tmp_path: Path
    ) -> None:
        binding = bindings.Binding(tmp_path, "")
        spec = bindings.WheelSpec("aarch64-apple-darwin")

        argv = bindings.build_argv(binding, spec, tmp_path, release=False)

        assert "--zig" not in argv
        assert "--release" not in argv

    def test_no_bindings_builds_nothing(self, tmp_path: Path) -> None:
        rc = bindings.build_wheels(_config(), ["x86_64-unknown-linux-gnu"], tmp_path)

        assert rc == 0
        assert not (tmp_path / "dist").exists()


class TestStamp:
    def test_stamps_cargo_and_a_static_pyproject_version(self, tmp_path: Path) -> None:
        static = _binding(
            tmp_path, "a-py", MATURIN + '[project]\nname = "a"\nversion = "0.0.1"\n'
        )
        dynamic = _binding(
            tmp_path, "b-py", MATURIN + '[project]\nname = "b"\ndynamic = ["version"]\n'
        )
        before = (dynamic / "pyproject.toml").read_text()

        bindings.stamp(_config(), tmp_path, "2.3.0")

        assert 'version = "2.3.0"' in (static / "Cargo.toml").read_text()
        assert 'version = "2.3.0"' in (static / "pyproject.toml").read_text()
        assert 'version = "2.3.0"' in (dynamic / "Cargo.toml").read_text()
        assert (dynamic / "pyproject.toml").read_text() == before

    def test_an_unreadable_paths_list_fails_the_stamp(self, tmp_path: Path) -> None:
        with pytest.raises(StampError, match="must be a list of paths"):
            bindings.stamp(_config({"paths": "a-py"}), tmp_path, "2.3.0")


class TestPublish:
    @pytest.mark.parametrize(
        ("name", "version", "expected"),
        [
            ("grippo-2.3.0-cp314-abi3-manylinux_2_28_x86_64.whl", "2.3.0", True),
            ("grippo-2.3.0b1-cp314-abi3-macosx_11_0_arm64.whl", "2.3.0-beta.1", True),
            ("grippo-0.1.0-cp314-abi3-win_amd64.whl", "2.3.0", False),
            ("grippo-2.3.0-cp314-abi3-win_amd64.whl", "", False),
            ("not-a-wheel", "2.3.0", False),
        ],
    )
    def test_a_wheel_must_carry_the_release_version(
        self, name: str, version: str, expected: bool
    ) -> None:
        assert bindings.carries_version(name, version) is expected

    def _wheels(self, root: Path, *names: str) -> None:
        out = root / bindings.WHEEL_DIR
        out.mkdir(parents=True)
        for name in names:
            (out / name).write_bytes(b"")

    @pytest.mark.parametrize(
        ("setting", "version", "rc", "uploads"),
        [
            (None, "2.3.0", 0, False),
            ({"publish": False}, "2.3.0", 0, False),
            ({"publish": "yes"}, "2.3.0", 0, False),
            ({"publish": True}, "", 1, False),
            ({"publish": True}, "2.4.0", 1, False),
            ({"publish": True}, "2.3.0", 0, True),
        ],
    )
    def test_uploads_only_when_opted_in_and_stamped(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        setting: dict[str, Any] | None,
        version: str,
        rc: int,
        uploads: bool,
    ) -> None:
        self._wheels(
            tmp_path,
            "x-2.3.0-cp314-abi3-manylinux_2_28_x86_64.whl",
            "x-2.3.0-cp314-abi3-musllinux_1_2_x86_64.whl",
        )
        calls: list[list[str]] = []

        def upload(files: list[str]) -> int:
            calls.append(files)
            return 0

        monkeypatch.setattr(py_release, "publish_pypi", upload)

        assert bindings.publish(_config(setting), tmp_path, version) == rc
        assert bool(calls) is uploads
        if uploads:
            assert [Path(f).name for f in calls[0]] == [
                "x-2.3.0-cp314-abi3-manylinux_2_28_x86_64.whl",
                "x-2.3.0-cp314-abi3-musllinux_1_2_x86_64.whl",
            ]

    def test_no_wheels_is_nothing_to_do(self, tmp_path: Path) -> None:
        assert bindings.publish(_config({"publish": True}), tmp_path, "2.3.0") == 0

    def test_the_upload_names_the_files_and_keeps_the_token_out_of_argv(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[list[str], dict[str, str] | None]] = []

        class _Done:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(cmd: list[str], **kwargs: Any) -> _Done:
            seen.append((cmd, kwargs.get("env")))
            return _Done()

        monkeypatch.setenv("PYPI_TOKEN", "fake-pypi-token")
        monkeypatch.setattr(py_release, "run_cmd", fake_run)

        assert py_release.publish_pypi(["dist/wheels/a.whl"]) == 0
        assert seen == [
            (
                ["uv", "publish", "--no-config", "dist/wheels/a.whl"],
                {"UV_PUBLISH_TOKEN": "fake-pypi-token"},
            )
        ]
