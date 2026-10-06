# Project:   HyperI CI
# File:      tests/unit/test_llvm_version.py
# Purpose:   Unit tests for the designated LLVM version resolver
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from pathlib import Path

import pytest

from hyperi_ci import llvm_version
from hyperi_ci.llvm_version import (
    LLVM_VERSION_ENV,
    DesignatedLLVM,
    LLVMVersionError,
    default_llvm_major,
    designated_llvm_version,
)
from hyperi_ci.versions import runtime_version


def _project(tmp_path: Path, body: str) -> Path:
    (tmp_path / ".hyperi-ci.yaml").write_text(body, encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def no_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(LLVM_VERSION_ENV, raising=False)


class TestPrecedence:
    """Env beats .hyperi-ci.yaml beats versions.yaml."""

    def test_versions_yaml_is_the_default(self, tmp_path: Path) -> None:
        assert designated_llvm_version(tmp_path) == DesignatedLLVM(
            int(runtime_version("llvm")), "versions.yaml"
        )

    def test_project_config_beats_versions_yaml(self, tmp_path: Path) -> None:
        root = _project(tmp_path, "build:\n  rust:\n    llvm_version: 21\n")
        assert designated_llvm_version(root) == DesignatedLLVM(21, ".hyperi-ci.yaml")

    def test_a_quoted_config_value_is_accepted(self, tmp_path: Path) -> None:
        root = _project(tmp_path, 'build:\n  rust:\n    llvm_version: "22"\n')
        assert designated_llvm_version(root).major == 22

    def test_env_beats_project_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _project(tmp_path, "build:\n  rust:\n    llvm_version: 21\n")
        monkeypatch.setenv(LLVM_VERSION_ENV, "20")
        assert designated_llvm_version(root) == DesignatedLLVM(20, LLVM_VERSION_ENV)

    def test_an_empty_env_value_counts_as_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _project(tmp_path, "build:\n  rust:\n    llvm_version: 21\n")
        monkeypatch.setenv(LLVM_VERSION_ENV, "")
        assert designated_llvm_version(root).major == 21

    def test_a_null_config_value_falls_through(self, tmp_path: Path) -> None:
        root = _project(tmp_path, "build:\n  rust:\n    llvm_version:\n")
        assert designated_llvm_version(root).source == "versions.yaml"

    def test_other_build_rust_keys_do_not_matter(self, tmp_path: Path) -> None:
        root = _project(tmp_path, "build:\n  rust:\n    jobs: 4\n")
        assert designated_llvm_version(root).source == "versions.yaml"

    def test_defaults_to_the_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _project(tmp_path, "build:\n  rust:\n    llvm_version: 21\n")
        monkeypatch.chdir(root)
        assert designated_llvm_version().major == 21


class TestDefaultMajor:
    """The default is versions.yaml alone; nothing designated can move it."""

    def test_is_versions_yaml(self) -> None:
        assert default_llvm_major() == int(runtime_version("llvm"))

    def test_ignores_env_and_project_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _project(tmp_path, "build:\n  rust:\n    llvm_version: 21\n")
        monkeypatch.chdir(root)
        monkeypatch.setenv(LLVM_VERSION_ENV, "20")
        assert default_llvm_major() == int(runtime_version("llvm"))

    def test_a_bad_versions_yaml_value_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(llvm_version, "runtime_version", lambda _name: "23.1")
        with pytest.raises(LLVMVersionError, match=r"runtimes\.llvm"):
            default_llvm_major()

    def test_a_missing_versions_yaml_key_is_an_llvm_version_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Callers catch LLVMVersionError, so a KeyError would be a traceback."""

        def missing(_name: str) -> str:
            raise KeyError("`runtimes.llvm` is missing from versions.yaml")

        monkeypatch.setattr(llvm_version, "runtime_version", missing)
        with pytest.raises(LLVMVersionError, match=r"runtimes\.llvm.*missing"):
            default_llvm_major()


class TestValidation:
    """A value that is not a whole-number major fails, naming its source."""

    # Fullwidth digits pass str.isdigit() but are not an apt package suffix.
    _FULLWIDTH_23 = chr(0xFF12) + chr(0xFF13)

    @pytest.mark.parametrize("value", ["abc", "23.1", "0", "-1", "23a", _FULLWIDTH_23])
    def test_bad_env_value_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv(LLVM_VERSION_ENV, value)
        with pytest.raises(LLVMVersionError, match=LLVM_VERSION_ENV):
            designated_llvm_version(tmp_path)

    @pytest.mark.parametrize("value", ["true", "23.0", "[23]", "nineteen"])
    def test_bad_config_value_is_refused(self, tmp_path: Path, value: str) -> None:
        root = _project(tmp_path, f"build:\n  rust:\n    llvm_version: {value}\n")
        with pytest.raises(LLVMVersionError, match=r"build\.rust\.llvm_version"):
            designated_llvm_version(root)

    def test_the_error_is_a_value_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(LLVM_VERSION_ENV, "latest")
        with pytest.raises(ValueError, match="'latest'"):
            designated_llvm_version()

    def test_whitespace_around_a_good_value_is_tolerated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(LLVM_VERSION_ENV, " 23 ")
        assert designated_llvm_version(tmp_path).major == 23
