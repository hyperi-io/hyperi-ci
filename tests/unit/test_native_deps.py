# Project:   HyperI CI
# File:      tests/unit/test_native_deps.py
# Purpose:   Unit tests for native dependency (apt) management
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import io
import shutil
import subprocess
import zipfile
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
    _sudo_prefix,
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

    def test_default_version_is_23(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HYPERCI_LLVM_VERSION", raising=False)
        result = _expand_template_vars("bolt-${HYPERCI_LLVM_VERSION}")
        assert result == "bolt-23"

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

    def test_multi_version_writes_append_not_overwrite(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Multiple _add_apt_repo calls sharing a keyring must accumulate.

        Multi-version toolchains (LLVM 19/20/21/22) reuse one keyring file,
        which derives one sources filename. Before v1.11.1 the writer used
        `tee` (overwrite), so only the last version's `deb` line survived.
        Regression check: after four calls the file contains four lines.
        """
        fake_keyring = tmp_path / "llvm.gpg"
        fake_keyring.write_bytes(b"fake-key")
        sources_list, sources_dir = _seed_apt_tree(tmp_path, {})
        _patch_apt_paths(monkeypatch, sources_list, sources_dir)
        # Force ``["sudo"]`` prefix regardless of platform/uid — the macOS
        # CI runner is non-Linux (so _sudo_prefix returns []), Linux CI is
        # non-root. Both cases land in this test; pin to the sudo path.
        monkeypatch.setattr(native_deps, "_sudo_prefix", lambda: ["sudo"])

        target_file = sources_dir / "llvm.list"

        def fake_sudo_run(cmd, **kwargs):
            # dpkg arch probe — return amd64
            if cmd[:2] == ["dpkg", "--print-architecture"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="amd64\n", stderr="")
            # Simulate sudo tee -a: append stdin to the target file
            if cmd[:3] == ["sudo", "tee", "-a"]:
                path = Path(cmd[3])
                mode = "ab" if path.exists() else "wb"
                with open(path, mode) as f:
                    f.write(kwargs.get("input", b""))
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
            # Legacy `sudo tee` (no -a) would land here; kept for regression detection.
            if cmd[:2] == ["sudo", "tee"]:
                path = Path(cmd[2])
                with open(path, "wb") as f:
                    f.write(kwargs.get("input", b""))
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(native_deps.subprocess, "run", fake_sudo_run)

        for version in ("19", "20", "21", "22"):
            repo = AptRepo(
                key_url="https://apt.llvm.org/llvm-snapshot.gpg.key",
                keyring=str(fake_keyring),
                url="https://apt.llvm.org/noble/",
                codename=f"llvm-toolchain-noble-{version}",
                components="main",
            )
            rc = _add_apt_repo(repo)
            assert rc == 0, f"failed adding v{version}"

        content = target_file.read_text()
        for v in ("19", "20", "21", "22"):
            assert f"llvm-toolchain-noble-{v}" in content, (
                f"v{v} entry missing — writer clobbered earlier versions"
            )

    def test_idempotent_when_exact_line_already_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Second call with identical repo is a no-op (not a duplicate line)."""
        fake_keyring = tmp_path / "llvm.gpg"
        fake_keyring.write_bytes(b"fake-key")
        sources_list, sources_dir = _seed_apt_tree(tmp_path, {})
        _patch_apt_paths(monkeypatch, sources_list, sources_dir)
        monkeypatch.setattr(native_deps, "_sudo_prefix", lambda: ["sudo"])
        target_file = sources_dir / "llvm.list"

        writes = {"count": 0}

        def fake_sudo_run(cmd, **kwargs):
            if cmd[:2] == ["dpkg", "--print-architecture"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="amd64\n", stderr="")
            if cmd[:3] == ["sudo", "tee", "-a"]:
                writes["count"] += 1
                path = Path(cmd[3])
                mode = "ab" if path.exists() else "wb"
                with open(path, mode) as f:
                    f.write(kwargs.get("input", b""))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(native_deps.subprocess, "run", fake_sudo_run)

        repo = AptRepo(
            key_url="https://apt.llvm.org/llvm-snapshot.gpg.key",
            keyring=str(fake_keyring),
            url="https://apt.llvm.org/noble/",
            codename="llvm-toolchain-noble-22",
            components="main",
        )
        assert _add_apt_repo(repo) == 0
        assert _add_apt_repo(repo) == 0  # no-op
        assert writes["count"] == 1, "second identical add triggered a write"
        assert target_file.read_text().count("llvm-toolchain-noble-22") == 1


class TestAptKeyFingerprint:
    """A declared `key_fingerprint` gates the key before it reaches the keyring.

    HTTPS says who served the key, not which key it is. Without the check a
    swapped apt.llvm.org key installs silently and signs every package after.
    """

    # gpg --with-colons output: primary key first, then its encryption subkey.
    _COLONS = (
        b"pub:-:4096:1:15CF4D18AF4F7421:1362990124:::-:::scESC::::::23::0:\n"
        b"fpr:::::::::6084F3CF814B57C1CF12EFD515CF4D18AF4F7421:\n"
        b"sub:-:4096:1:5B3D8846AF9463CE:1362990124::::::e::::::23:\n"
        b"fpr:::::::::56E1477F57BAB5ECF37818045B3D8846AF9463CE:\n"
    )
    _PRIMARY = "6084F3CF814B57C1CF12EFD515CF4D18AF4F7421"
    _SUBKEY = "56E1477F57BAB5ECF37818045B3D8846AF9463CE"

    def _install(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fingerprint: str,
    ) -> tuple[int, list[list[str]]]:
        """Run _add_apt_repo against a fake gpg; return (rc, commands invoked)."""
        sources_list, sources_dir = _seed_apt_tree(tmp_path, {})
        _patch_apt_paths(monkeypatch, sources_list, sources_dir)
        monkeypatch.setattr(native_deps, "_sudo_prefix", lambda: [])

        invoked: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            invoked.append(list(cmd))
            if cmd[0] == "curl":
                return subprocess.CompletedProcess(cmd, 0, stdout=b"ARMOURED-KEY")
            if cmd[:2] == ["gpg", "--show-keys"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=self._COLONS)
            if cmd[:2] == ["dpkg", "--print-architecture"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="amd64\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

        monkeypatch.setattr(native_deps.subprocess, "run", fake_run)

        repo = AptRepo(
            key_url="https://apt.llvm.org/llvm-snapshot.gpg.key",
            keyring=str(tmp_path / "llvm.gpg"),
            url="https://apt.llvm.org/noble/",
            codename="llvm-toolchain-noble-23",
            key_fingerprint=fingerprint,
        )
        return _add_apt_repo(repo), invoked

    def test_matching_fingerprint_installs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rc, invoked = self._install(tmp_path, monkeypatch, self._PRIMARY)
        assert rc == 0
        assert any("--dearmor" in cmd for cmd in invoked)

    def test_spaced_lowercase_fingerprint_still_matches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The value is copied off apt.llvm.org, which prints it spaced."""
        spaced = "6084 f3cf 814b 57c1 cf12 efd5 15cf 4d18 af4f 7421"
        rc, _ = self._install(tmp_path, monkeypatch, spaced)
        assert rc == 0

    def test_mismatch_refuses_before_the_keyring_is_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rc, invoked = self._install(tmp_path, monkeypatch, "DEADBEEF" * 5)
        assert rc != 0
        assert not any("--dearmor" in cmd for cmd in invoked)

    def test_subkey_fingerprint_is_not_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the primary key counts — a subkey match would weaken the pin."""
        rc, invoked = self._install(tmp_path, monkeypatch, self._SUBKEY)
        assert rc != 0
        assert not any("--dearmor" in cmd for cmd in invoked)

    def test_no_fingerprint_declared_skips_the_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rc, invoked = self._install(tmp_path, monkeypatch, "")
        assert rc == 0
        assert not any(cmd[:2] == ["gpg", "--show-keys"] for cmd in invoked)


class TestSudoPrefix:
    """`_sudo_prefix()` skips sudo when already root.

    Runner image bake (Dockerfile ``RUN``) runs as root where sudo isn't
    configured; the previous 'sudo apt-get install' produced
    'root is not in the sudoers file' and failed the build.
    """

    def test_non_root_prepends_sudo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(native_deps.platform, "system", lambda: "Linux")
        monkeypatch.setattr(native_deps.os, "geteuid", lambda: 1000)
        assert _sudo_prefix() == ["sudo"]

    def test_root_skips_sudo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(native_deps.platform, "system", lambda: "Linux")
        monkeypatch.setattr(native_deps.os, "geteuid", lambda: 0)
        assert _sudo_prefix() == []

    def test_non_linux_skips_sudo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # macOS dev path — apt isn't used, but the helper must be safe to call
        monkeypatch.setattr(native_deps.platform, "system", lambda: "Darwin")
        assert _sudo_prefix() == []


class TestDepGroupLoading:
    """End-to-end: native-deps YAML loads with env-var templating."""

    def test_rust_yaml_bolt_version_defaults_to_23(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HYPERCI_LLVM_VERSION", raising=False)
        monkeypatch.setenv("OS_CODENAME", "noble")
        groups = _load_dep_groups("rust")
        bolt_groups = [g for g in groups if g.name == "llvm-bolt"]
        assert len(bolt_groups) == 1
        bolt = bolt_groups[0]
        assert bolt.dpkg_check == "bolt-23"
        assert "bolt-23" in bolt.apt_packages
        assert bolt.apt_repos[0].codename == "llvm-toolchain-noble-23"
        assert (
            bolt.apt_repos[0].key_fingerprint
            == "6084F3CF814B57C1CF12EFD515CF4D18AF4F7421"
        )

    def test_rust_yaml_bolt_version_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HYPERCI_LLVM_VERSION", "19")
        monkeypatch.setenv("OS_CODENAME", "noble")
        groups = _load_dep_groups("rust")
        bolt = next(g for g in groups if g.name == "llvm-bolt")
        assert bolt.dpkg_check == "bolt-19"
        assert "bolt-19" in bolt.apt_packages
        assert bolt.apt_repos[0].codename == "llvm-toolchain-noble-19"

    # The three groups every Rust project gets regardless of its dependencies.
    _ALWAYS_ON = ("mold linker", "clang linker", "llvm-bolt")

    @pytest.mark.parametrize("group_name", _ALWAYS_ON)
    def test_rust_yaml_always_on_group_matches_a_plain_package_root(
        self, group_name: str
    ) -> None:
        group = next(g for g in _load_dep_groups("rust") if g.name == group_name)
        manifest = '[package]\nname = "app"\nversion = "1.0.0"\n'
        assert native_deps._patterns_match(manifest, group.patterns)

    @pytest.mark.parametrize("group_name", _ALWAYS_ON)
    def test_rust_yaml_always_on_group_matches_a_virtual_workspace_root(
        self, group_name: str
    ) -> None:
        """A virtual workspace root has no [package] and still needs all three."""
        group = next(g for g in _load_dep_groups("rust") if g.name == group_name)
        manifest = '[workspace]\nmembers = ["crates/archiver"]\nresolver = "2"\n'
        assert native_deps._patterns_match(manifest, group.patterns)


class TestMultiVersionToolchains:
    """Toolchains category expands `versions:` list into N DepGroups.

    One YAML entry with `versions: [19, 20, 21, 22]` becomes four DepGroups
    with `{V}` substituted in `dpkg_check`, `apt_repos[*].codename`, and
    every `apt_packages[*]`. The `name` is suffixed " vN" so log lines
    distinguish versions.
    """

    def test_llvm_yaml_expands_to_one_group_per_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OS_CODENAME", "noble")
        groups = _load_dep_groups("llvm", category="toolchains")
        # LLVM YAML: versions [19,20,21,22,23] expand to 5 coinstallable
        # groups plus 1 non-coinstallable singleton (bake: false) = 6 total.
        multi_names = [g.name for g in groups if g.name.startswith("llvm-toolchain v")]
        singleton_names = [g.name for g in groups if g.name == "llvm-non-coinstallable"]
        assert multi_names == [
            "llvm-toolchain v19",
            "llvm-toolchain v20",
            "llvm-toolchain v21",
            "llvm-toolchain v22",
            "llvm-toolchain v23",
        ]
        assert singleton_names == ["llvm-non-coinstallable"]
        assert len(groups) == 6

    def test_llvm_non_coinstallable_entry_is_install_on_demand(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The non-coinstallable entry bundles the one-version-only packages.

        Marked bake: false — skipped in --all mode (runner image), installed
        conditionally at CI job time when manifest patterns match.
        """
        monkeypatch.setenv("OS_CODENAME", "noble")
        groups = _load_dep_groups("llvm", category="toolchains")
        non_coinst = next(g for g in groups if g.name == "llvm-non-coinstallable")
        # These all declare Conflicts: <pkg>-x.y on apt.llvm.org
        assert "lldb-22" in non_coinst.apt_packages
        assert "libc++-22-dev" in non_coinst.apt_packages
        assert "libc++abi-22-dev" in non_coinst.apt_packages
        assert "libomp-22-dev" in non_coinst.apt_packages
        assert "libunwind-22-dev" in non_coinst.apt_packages
        # Entry must NOT include the coinstallable multi-version packages
        assert "clang-22" not in non_coinst.apt_packages
        # Most importantly: marked install-on-demand
        assert non_coinst.bake is False


class TestBakeFlag:
    """`bake: false` skips an entry in --all mode regardless of category.

    Standard pattern for toolsets that only support one version at a time
    (libc++-N-dev, lldb-N, etc. declare Conflicts:x.y on apt.llvm.org).
    Applies to ANY YAML entry in any category.
    """

    def test_bake_defaults_to_true_when_key_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Existing YAMLs without a `bake` key stay unconditionally baked."""
        monkeypatch.setenv("OS_CODENAME", "noble")
        groups = _load_dep_groups("llvm", category="toolchains")
        multi = next(g for g in groups if g.name == "llvm-toolchain v22")
        assert multi.bake is True

    def test_all_mode_skips_bake_false_entries(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """--all mode installs bake:true entries, skips bake:false."""
        bogus_dir = tmp_path / "toolchains"
        bogus_dir.mkdir()
        (bogus_dir / "pair.yaml").write_text(
            "- name: always-bake\n"
            "  patterns: []\n"
            "  manifest_files: []\n"
            "  dpkg_check: 'clang-22'\n"
            "  apt_packages:\n"
            "    - 'clang-22'\n"
            "- name: never-bake\n"
            "  bake: false\n"
            "  patterns: []\n"
            "  manifest_files: []\n"
            "  dpkg_check: 'lldb-22'\n"
            "  apt_packages:\n"
            "    - 'lldb-22'\n"
        )
        monkeypatch.setitem(native_deps._CATEGORY_DIRS, "toolchains", bogus_dir)
        monkeypatch.setattr(native_deps.platform, "system", lambda: "Linux")

        installed: list[str] = []

        def fake_apt_install(packages: list[str]) -> int:
            installed.extend(packages)
            return 0

        monkeypatch.setattr(native_deps, "_apt_install", fake_apt_install)
        monkeypatch.setattr(
            native_deps, "_is_dpkg_installed", lambda pkg, min_v="": False
        )
        monkeypatch.setattr(native_deps, "_add_apt_repo", lambda repo: 0)

        rc = native_deps.install_native_deps(
            "pair", project_dir=tmp_path, category="toolchains", all_mode=True
        )
        assert rc == 0
        assert "clang-22" in installed  # bake: true default
        assert "lldb-22" not in installed  # bake: false skipped

    def test_conditional_mode_ignores_bake_flag(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Conditional mode installs based on patterns regardless of bake flag."""
        bogus_dir = tmp_path / "toolchains"
        bogus_dir.mkdir()
        (bogus_dir / "pair.yaml").write_text(
            "- name: never-bake\n"
            "  bake: false\n"
            "  patterns:\n"
            "    - 'trigger-me'\n"
            "  manifest_files:\n"
            "    - 'Trigger.toml'\n"
            "  dpkg_check: 'lldb-22'\n"
            "  apt_packages:\n"
            "    - 'lldb-22'\n"
        )
        (tmp_path / "Trigger.toml").write_text("trigger-me here\n")
        monkeypatch.setitem(native_deps._CATEGORY_DIRS, "toolchains", bogus_dir)
        monkeypatch.setattr(native_deps.platform, "system", lambda: "Linux")

        installed: list[str] = []

        def fake_apt_install(packages: list[str]) -> int:
            installed.extend(packages)
            return 0

        monkeypatch.setattr(native_deps, "_apt_install", fake_apt_install)
        monkeypatch.setattr(
            native_deps, "_is_dpkg_installed", lambda pkg, min_v="": False
        )
        monkeypatch.setattr(native_deps, "_add_apt_repo", lambda repo: 0)

        # all_mode=False (conditional) — bake flag does NOT affect the decision
        rc = native_deps.install_native_deps(
            "pair", project_dir=tmp_path, category="toolchains", all_mode=False
        )
        assert rc == 0
        assert "lldb-22" in installed  # pattern matched → installed regardless of bake

    def test_substitutes_version_in_dpkg_check_and_packages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OS_CODENAME", "noble")
        groups = _load_dep_groups("llvm", category="toolchains")
        v22 = next(g for g in groups if g.name.endswith("v22"))
        assert v22.dpkg_check == "clang-22"
        assert "clang-22" in v22.apt_packages
        assert "bolt-22" in v22.apt_packages
        assert "libclang-rt-22-dev" in v22.apt_packages
        # {V} must not leak into the final packages
        assert not any("{V}" in p for p in v22.apt_packages)

    def test_substitutes_os_codename_and_version_in_repo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repo URL takes OS codename; repo codename takes both OS + version."""
        monkeypatch.setenv("OS_CODENAME", "resolute")
        groups = _load_dep_groups("llvm", category="toolchains")
        v21 = next(g for g in groups if g.name.endswith("v21"))
        assert v21.apt_repos[0].url == "https://apt.llvm.org/resolute/"
        assert v21.apt_repos[0].codename == "llvm-toolchain-resolute-21"

    def test_gcc_expansion_no_repos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GCC uses distro repos — empty apt_repos after expansion."""
        monkeypatch.setenv("OS_CODENAME", "trixie")
        groups = _load_dep_groups("gcc", category="toolchains")
        assert len(groups) == 2
        for g in groups:
            assert g.apt_repos == []

    def test_unknown_category_returns_empty(self) -> None:
        assert _load_dep_groups("llvm", category="bogus") == []

    def test_empty_versions_list_is_skipped_with_warning(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An empty `versions: []` is a config bug — warn and skip."""
        # Redirect the toolchains dir to a tmp location we control
        bogus_dir = tmp_path / "toolchains"
        bogus_dir.mkdir()
        (bogus_dir / "broken.yaml").write_text(
            "- name: broken-entry\n"
            "  versions: []\n"
            "  patterns: []\n"
            "  manifest_files: []\n"
            "  dpkg_check: 'clang-{V}'\n"
            "  apt_packages:\n"
            "    - 'clang-{V}'\n"
        )
        monkeypatch.setitem(native_deps._CATEGORY_DIRS, "toolchains", bogus_dir)
        # Loguru bypasses stdlib logging / capsys — capture via .warning patch
        warnings: list[str] = []
        monkeypatch.setattr(
            native_deps.logger, "warning", lambda msg: warnings.append(msg)
        )
        groups = _load_dep_groups("broken", category="toolchains")
        assert groups == []
        assert len(warnings) == 1
        assert "empty `versions:`" in warnings[0]
        assert "broken-entry" in warnings[0]


class TestExpandTemplateVarsOsCodename:
    """`${OS_CODENAME}` expansion resolves from env var or lsb_release."""

    def test_env_override_wins_over_lsb_release(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OS_CODENAME", "resolute")
        assert _expand_template_vars("url: ${OS_CODENAME}/") == "url: resolute/"

    def test_env_empty_falls_back_to_lsb_release(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OS_CODENAME", raising=False)
        with patch.object(native_deps, "_get_os_codename", return_value="noble"):
            assert _expand_template_vars("${OS_CODENAME}") == "noble"

    def test_lsb_release_missing_falls_back_to_noble(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OS_CODENAME", raising=False)
        with patch.object(native_deps, "_get_os_codename", return_value=""):
            assert _expand_template_vars("${OS_CODENAME}") == "noble"

    def test_get_os_codename_tolerates_missing_lsb_release(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """macOS CI runners have no lsb_release — must return "" not raise."""

        def _raise_not_found(*_args, **_kwargs):
            raise FileNotFoundError(2, "No such file", "lsb_release")

        monkeypatch.setattr(native_deps.subprocess, "run", _raise_not_found)
        assert native_deps._get_os_codename() == ""


class TestAllModeBypass:
    """`all_mode=True` bypasses pattern matching for runner image bake."""

    def test_all_mode_ignores_patterns(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A group whose patterns don't match should still install in --all mode."""
        monkeypatch.setenv("OS_CODENAME", "noble")
        # Stub platform.system to "Linux" so install logic runs
        monkeypatch.setattr(native_deps.platform, "system", lambda: "Linux")
        # Record which groups reach the install step
        installed_packages: list[str] = []

        def fake_apt_install(packages: list[str]) -> int:
            installed_packages.extend(packages)
            return 0

        monkeypatch.setattr(native_deps, "_apt_install", fake_apt_install)
        monkeypatch.setattr(
            native_deps, "_is_dpkg_installed", lambda pkg, min_v="": False
        )
        monkeypatch.setattr(native_deps, "_add_apt_repo", lambda repo: 0)

        # Empty project dir → no manifests → patterns would normally match nothing
        rc = native_deps.install_native_deps(
            "gcc",
            project_dir=tmp_path,
            category="toolchains",
            all_mode=True,
        )
        assert rc == 0
        # All GCC versions should have been queued regardless of patterns
        assert "gcc-13" in installed_packages
        assert "gcc-14" in installed_packages

    def test_conditional_mode_skips_when_no_manifest_match(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Default (conditional) mode must skip when manifest patterns miss."""
        monkeypatch.setenv("OS_CODENAME", "noble")
        monkeypatch.setattr(native_deps.platform, "system", lambda: "Linux")
        installed_packages: list[str] = []

        def fake_apt_install(packages: list[str]) -> int:
            installed_packages.extend(packages)
            return 0

        monkeypatch.setattr(native_deps, "_apt_install", fake_apt_install)
        monkeypatch.setattr(
            native_deps, "_is_dpkg_installed", lambda pkg, min_v="": False
        )
        monkeypatch.setattr(native_deps, "_add_apt_repo", lambda repo: 0)

        # Empty project dir → no patterns match → nothing installed
        rc = native_deps.install_native_deps(
            "gcc",
            project_dir=tmp_path,
            category="toolchains",
            all_mode=False,
        )
        assert rc == 0
        assert installed_packages == []


class TestLanguageToolInstallerAbsent:
    """issue #91: a missing installer degrades, it does not crash.

    `install_language_tools` documents itself as non-fatal, but
    `subprocess.run(..., check=False)` only suppresses a non-zero EXIT --
    a missing executable raises FileNotFoundError before the process starts.
    Rust reaches this before the workflow installs rustup.
    """

    @pytest.fixture(autouse=True)
    def _empty_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # The probe also checks ~/<bin_dir>/<binary>, so a host that already
        # has the tool would skip the install and never reach these paths.
        monkeypatch.setenv("HOME", str(tmp_path))

    def test_missing_installer_binary_warns_and_continues(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(native_deps.platform, "system", lambda: "Linux")
        # cargo absent, and the tool it would install is absent too.
        monkeypatch.setattr(native_deps.shutil, "which", lambda _cmd: None)

        def explode(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("must not invoke a missing installer")

        monkeypatch.setattr(native_deps.subprocess, "run", explode)

        assert native_deps._install_language_tools("rust") == 0

    def test_present_installer_is_still_invoked(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The skip stays narrow -- a real cargo still installs the tool."""
        monkeypatch.setattr(native_deps.platform, "system", lambda: "Linux")
        monkeypatch.setattr(
            native_deps.shutil,
            "which",
            lambda cmd: "/usr/bin/cargo" if cmd == "cargo" else None,
        )
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(native_deps.subprocess, "run", fake_run)

        assert native_deps._install_language_tools("rust") == 0
        assert calls, "a present cargo must still be invoked"
        assert calls[0][0] == "cargo"


class TestEnsureAwsCli:
    """AWS CLI v2 arrives as a signed zip, because apt cannot supply it."""

    @pytest.mark.skipif(shutil.which("gpg") is None, reason="gpg not installed")
    def test_shipped_key_carries_the_pinned_fingerprint(self) -> None:
        """The .asc in the package is the key the code claims to trust.

        A transcription slip or an unannounced rotation both land here rather
        than as a signature failure on a release runner.
        """
        key = native_deps._AWS_CLI_KEY_FILE.read_bytes()
        assert native_deps._AWS_CLI_KEY_FPR in native_deps._key_fingerprints(key)

    def test_existing_aws_is_returned_without_downloading(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(native_deps.shutil, "which", lambda _c: "/usr/bin/aws")

        def explode(*_a: object, **_k: object) -> None:
            raise AssertionError("must not download when aws is already present")

        monkeypatch.setattr(native_deps, "_download", explode)
        assert native_deps.ensure_aws_cli() == "/usr/bin/aws"

    def test_non_linux_declines_rather_than_installing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dev machine gets the brew notice, not a system-wide install."""
        monkeypatch.setattr(native_deps.shutil, "which", lambda _c: None)
        monkeypatch.setattr(native_deps.platform, "system", lambda: "Darwin")
        assert native_deps.ensure_aws_cli() is None

    def test_unsupported_arch_declines(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(native_deps.shutil, "which", lambda _c: None)
        monkeypatch.setattr(native_deps.platform, "system", lambda: "Linux")
        monkeypatch.setattr(native_deps.platform, "machine", lambda: "riscv64")
        assert native_deps.ensure_aws_cli() is None

    def test_a_bad_signature_installs_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verification is the gate, so a failure stops before extraction."""
        monkeypatch.setattr(native_deps.shutil, "which", lambda _c: None)
        monkeypatch.setattr(native_deps.platform, "system", lambda: "Linux")
        monkeypatch.setattr(native_deps.platform, "machine", lambda: "x86_64")
        monkeypatch.setattr(native_deps, "_download", lambda _n, _u: b"not-a-zip")
        monkeypatch.setattr(
            native_deps, "_verify_detached_signature", lambda *_a: False
        )

        def explode(*_a: object, **_k: object) -> None:
            raise AssertionError("must not extract an unverified archive")

        monkeypatch.setattr(native_deps, "_extract_zip", explode)
        assert native_deps.ensure_aws_cli() is None

    def test_a_failed_download_installs_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(native_deps.shutil, "which", lambda _c: None)
        monkeypatch.setattr(native_deps.platform, "system", lambda: "Linux")
        monkeypatch.setattr(native_deps.platform, "machine", lambda: "x86_64")
        monkeypatch.setattr(native_deps, "_download", lambda _n, _u: None)

        def explode(*_a: object, **_k: object) -> None:
            raise AssertionError("must not verify a download that never arrived")

        monkeypatch.setattr(native_deps, "_verify_detached_signature", explode)
        assert native_deps.ensure_aws_cli() is None

    def test_extract_preserves_the_executable_bit(self, tmp_path: Path) -> None:
        """ZipFile.extract drops the mode, which would leave `install` unrunnable."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            info = zipfile.ZipInfo("aws/install")
            info.external_attr = 0o755 << 16
            zf.writestr(info, "#!/bin/sh\n")

        assert native_deps._extract_zip(buf.getvalue(), tmp_path) is True
        extracted = tmp_path / "aws" / "install"
        assert extracted.stat().st_mode & 0o111, "install script is not executable"

    def test_a_corrupt_archive_is_reported_not_raised(self, tmp_path: Path) -> None:
        assert native_deps._extract_zip(b"not-a-zip", tmp_path) is False
