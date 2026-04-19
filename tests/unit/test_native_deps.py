# Project:   HyperI CI
# File:      tests/unit/test_native_deps.py
# Purpose:   Unit tests for native dependency (apt) management
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from hyperi_ci import native_deps
from hyperi_ci.native_deps import (
    AptRepo,
    _add_apt_repo,
    _expand_template_vars,
    _load_dep_groups,
    _repo_already_configured,
)


def _seed_apt_tree(tmp_path: Path, files: dict[str, str]) -> tuple[Path, Path]:
    """Create a fake /etc/apt hierarchy under tmp_path.

    Returns (sources_list_path, sources_dir_path).
    Keys in `files` are filenames relative to sources.list.d/ except the
    special key "sources.list" which writes /etc/apt/sources.list.
    """
    apt = tmp_path / "etc" / "apt"
    sources_dir = apt / "sources.list.d"
    sources_dir.mkdir(parents=True)
    sources_list = apt / "sources.list"
    for name, content in files.items():
        if name == "sources.list":
            sources_list.write_text(content)
        else:
            (sources_dir / name).write_text(content)
    return sources_list, sources_dir


def _patch_apt_paths(
    monkeypatch: pytest.MonkeyPatch, sources_list: Path, sources_dir: Path
) -> None:
    """Redirect the two /etc/apt Path() constructions in native_deps.

    native_deps._repo_already_configured calls Path("/etc/apt/sources.list")
    and Path("/etc/apt/sources.list.d"). Monkey-patch the module-level
    Path symbol to route those specific strings to our tmp locations.
    """
    real_path = Path

    def fake_path(p):
        s = str(p)
        if s == "/etc/apt/sources.list":
            return sources_list
        if s == "/etc/apt/sources.list.d":
            return sources_dir
        return real_path(p)

    monkeypatch.setattr(native_deps, "Path", fake_path)


class TestTemplateExpansion:
    """${HYPERCI_LLVM_VERSION} expansion in native-deps YAML."""

    def test_default_version_is_22(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HYPERCI_LLVM_VERSION", raising=False)
        result = _expand_template_vars("bolt-${HYPERCI_LLVM_VERSION}")
        assert result == "bolt-22"

    def test_env_var_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HYPERCI_LLVM_VERSION", "19")
        result = _expand_template_vars("apt-${HYPERCI_LLVM_VERSION}")
        assert result == "apt-19"

    def test_unknown_placeholders_pass_through(self) -> None:
        # Unknown ${VAR} must NOT be silently replaced — surfaces as a
        # clear "package not found" error at apt-cache time.
        result = _expand_template_vars("pkg-${UNKNOWN_VAR}")
        assert result == "pkg-${UNKNOWN_VAR}"


class TestRepoAlreadyConfigured:
    """Cross-file duplicate-detection for pre-configured APT repos."""

    def test_returns_none_when_no_sources_match(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sources_list, sources_dir = _seed_apt_tree(tmp_path, {})
        _patch_apt_paths(monkeypatch, sources_list, sources_dir)
        assert (
            _repo_already_configured(
                "https://apt.llvm.org/noble/", "llvm-toolchain-noble-22"
            )
            is None
        )

    def test_matches_one_line_deb_entry_https(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sources_list, sources_dir = _seed_apt_tree(
            tmp_path,
            {
                "llvm-toolchain.list": (
                    "deb [signed-by=/usr/share/keyrings/llvm.gpg arch=amd64] "
                    "https://apt.llvm.org/noble/ llvm-toolchain-noble-22 main\n"
                )
            },
        )
        _patch_apt_paths(monkeypatch, sources_list, sources_dir)
        found = _repo_already_configured(
            "https://apt.llvm.org/noble/", "llvm-toolchain-noble-22"
        )
        assert found == sources_dir / "llvm-toolchain.list"

    def test_matches_http_when_searching_https(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Runner pre-provisioned with http://; our config uses https:// — must match."""
        sources_list, sources_dir = _seed_apt_tree(
            tmp_path,
            {
                "llvm.list": (
                    "deb [arch=amd64] http://apt.llvm.org/noble/ "
                    "llvm-toolchain-noble-22 main\n"
                )
            },
        )
        _patch_apt_paths(monkeypatch, sources_list, sources_dir)
        found = _repo_already_configured(
            "https://apt.llvm.org/noble/", "llvm-toolchain-noble-22"
        )
        assert found == sources_dir / "llvm.list"

    def test_matches_deb822_sources_format(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sources_list, sources_dir = _seed_apt_tree(
            tmp_path,
            {
                "llvm.sources": (
                    "Types: deb\n"
                    "URIs: https://apt.llvm.org/noble/\n"
                    "Suites: llvm-toolchain-noble-22\n"
                    "Components: main\n"
                )
            },
        )
        _patch_apt_paths(monkeypatch, sources_list, sources_dir)
        found = _repo_already_configured(
            "https://apt.llvm.org/noble/", "llvm-toolchain-noble-22"
        )
        assert found == sources_dir / "llvm.sources"

    def test_ignores_unrelated_repos(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sources_list, sources_dir = _seed_apt_tree(
            tmp_path,
            {
                "confluent.list": (
                    "deb https://packages.confluent.io/clients/deb noble main\n"
                ),
                "sources.list": "deb http://archive.ubuntu.com/ubuntu noble main\n",
            },
        )
        _patch_apt_paths(monkeypatch, sources_list, sources_dir)
        assert (
            _repo_already_configured(
                "https://apt.llvm.org/noble/", "llvm-toolchain-noble-22"
            )
            is None
        )

    def test_ignores_wrong_codename(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pre-existing LLVM repo for version 19 must NOT match our request for 22."""
        sources_list, sources_dir = _seed_apt_tree(
            tmp_path,
            {
                "llvm.list": (
                    "deb [arch=amd64] http://apt.llvm.org/noble/ "
                    "llvm-toolchain-noble-19 main\n"
                )
            },
        )
        _patch_apt_paths(monkeypatch, sources_list, sources_dir)
        assert (
            _repo_already_configured(
                "https://apt.llvm.org/noble/", "llvm-toolchain-noble-22"
            )
            is None
        )

    def test_handles_unreadable_files_gracefully(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        apt = tmp_path / "etc" / "apt"
        sources_dir = apt / "sources.list.d"
        sources_dir.mkdir(parents=True)
        (sources_dir / "corrupt.list").write_bytes(b"\xff\xfe\xfdnot-utf8")
        sources_list = apt / "sources.list"
        _patch_apt_paths(monkeypatch, sources_list, sources_dir)
        result = _repo_already_configured(
            "https://apt.llvm.org/noble/", "llvm-toolchain-noble-22"
        )
        assert result is None


class TestAddAptRepoIdempotency:
    """_add_apt_repo must not rewrite when the repo is already configured."""

    def test_skips_all_work_when_repo_pre_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pre-configured apt.llvm.org under a different filename → skip entirely.

        Exercise the full flow: keyring exists + different-named source
        file present. No subprocess calls should be made (no curl, no tee).
        """
        # Pre-existing keyring and source file (simulating runner-provisioned state)
        fake_keyring = tmp_path / "llvm.gpg"
        fake_keyring.write_bytes(b"fake-key")

        sources_list, sources_dir = _seed_apt_tree(
            tmp_path,
            {
                "llvm-toolchain.list": (
                    "deb [arch=amd64] http://apt.llvm.org/noble/ "
                    "llvm-toolchain-noble-22 main\n"
                )
            },
        )
        _patch_apt_paths(monkeypatch, sources_list, sources_dir)

        repo = AptRepo(
            key_url="https://apt.llvm.org/llvm-snapshot.gpg.key",
            keyring=str(fake_keyring),
            url="https://apt.llvm.org/noble/",
            codename="llvm-toolchain-noble-22",
            components="main",
        )

        with patch("hyperi_ci.native_deps.subprocess.run") as mock_run:
            rc = _add_apt_repo(repo)

        assert rc == 0
        # None of the "expensive/side-effecting" commands were invoked —
        # no key download, no gpg dearmor, no writing to sources.list.d.
        invoked = [
            call.args[0][0] if call.args and call.args[0] else None
            for call in mock_run.call_args_list
        ]
        assert "curl" not in invoked, f"unexpected curl call: {invoked}"
        assert "gpg" not in invoked, f"unexpected gpg call: {invoked}"
        assert "sudo" not in invoked, f"unexpected sudo (tee) call: {invoked}"
        # Our own filename (derived from keyring stem "llvm" + ".list") NOT created
        assert not (sources_dir / "llvm.list").exists()


class TestDepGroupLoading:
    """End-to-end: native-deps YAML loads with env-var templating."""

    def test_rust_yaml_bolt_version_defaults_to_22(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HYPERCI_LLVM_VERSION", raising=False)
        groups = _load_dep_groups("rust")
        bolt_groups = [g for g in groups if g.name == "llvm-bolt"]
        assert len(bolt_groups) == 1
        bolt = bolt_groups[0]
        assert bolt.dpkg_check == "bolt-22"
        assert "bolt-22" in bolt.apt_packages
        assert bolt.apt_repos[0].codename == "llvm-toolchain-noble-22"

    def test_rust_yaml_bolt_version_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HYPERCI_LLVM_VERSION", "19")
        groups = _load_dep_groups("rust")
        bolt = next(g for g in groups if g.name == "llvm-bolt")
        assert bolt.dpkg_check == "bolt-19"
        assert "bolt-19" in bolt.apt_packages
        assert bolt.apt_repos[0].codename == "llvm-toolchain-noble-19"
