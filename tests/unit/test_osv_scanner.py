# Project:   HyperI CI
# File:      tests/unit/test_osv_scanner.py
# Purpose:   Tests for the osv-scanner malicious-package helper
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for ``hyperi_ci.quality.osv_scanner``."""

import subprocess
import tomllib
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from hyperi_ci import tools
from hyperi_ci.common import run_cmd
from hyperi_ci.quality import osv_scanner
from hyperi_ci.quality.ignores import IgnoreEntry

# The shape dfe-archiver ships: two accepted advisories, with comments.
REPO_TOML = """\
# Advisories osv-scanner reports that are accepted, each with its reason

[[IgnoredVulns]]
id = "GHSA-w9wp-h8wv-79jx"
reason = "not reachable"

[[IgnoredVulns]]
id = "RUSTSEC-2024-0436"
reason = "compile-time proc macro"
"""

# Every other top-level setting osv-scanner v2 reads.
REPO_TOML_ALL_SETTINGS = """\
GoVersionOverride = "1.23.4"
ScanGoModVersion = true

[[IgnoredVulns]]
id = "GHSA-aaaa-bbbb-cccc"
ignoreUntil = 2027-01-01T00:00:00Z
reason = "repo reason"

[[PackageOverrides]]
name = "left-pad"
ecosystem = "npm"
ignore = true
reason = "vendored"

[[PackageOverrides]]
name = "lodash"
ecosystem = "npm"
[PackageOverrides.vulnerability]
ignore = true
[PackageOverrides.license]
override = ["MIT"]"""

# What osv-scanner v2.6.0 writes to stderr, exiting 127, when osv.dev is down.
OSV_DEV_UNREACHABLE_STDERR = (
    "Error during extraction: (extracting as vulnmatch/osvdev) max retries "
    "exceeded: attempt 4: request failed: Post "
    '"https://api.osv.dev/v1/querybatch": dial tcp: connect: connection refused\n'
)


def _entry(vuln_id: str, reason: str = "from quality.ignore") -> IgnoreEntry:
    return IgnoreEntry("osv-scanner", vuln_id, reason)


class TestRenderIgnoreConfig:
    """Render osv-scanner.toml ``[[IgnoredVulns]]`` blocks from entries."""

    def test_empty_entries_render_empty_string(self) -> None:
        assert osv_scanner.render_ignore_config([]) == ""

    def test_entry_without_expiry_omits_ignore_until(self) -> None:
        out = osv_scanner.render_ignore_config(
            [IgnoreEntry("osv-scanner", "MAL-2026-1", "because")]
        )
        assert "[[IgnoredVulns]]" in out
        assert 'id = "MAL-2026-1"' in out
        assert 'reason = "because"' in out
        assert "ignoreUntil" not in out

    def test_entry_with_expiry_emits_rfc3339_ignore_until(self) -> None:
        out = osv_scanner.render_ignore_config(
            [IgnoreEntry("osv-scanner", "MAL-2026-1", "r", date(2026, 6, 15))]
        )
        assert "ignoreUntil = 2026-06-15T00:00:00Z" in out

    def test_reason_double_quotes_are_escaped(self) -> None:
        out = osv_scanner.render_ignore_config(
            [IgnoreEntry("osv-scanner", "MAL-1", 'he said "hi"')]
        )
        assert r"\"hi\"" in out

    def test_multiple_entries_separated(self) -> None:
        out = osv_scanner.render_ignore_config(
            [
                IgnoreEntry("osv-scanner", "MAL-1", "r1"),
                IgnoreEntry("osv-scanner", "MAL-2", "r2"),
            ]
        )
        assert out.count("[[IgnoredVulns]]") == 2


class TestBuildCommand:
    """Compose the osv-scanner CLI invocation."""

    def test_basic_targets_lockfile(self) -> None:
        cmd = osv_scanner.build_command(Path("Cargo.lock"))
        assert cmd == ["osv-scanner", "scan", "source", "--lockfile", "Cargo.lock"]

    def test_with_config_appends_config_flag(self) -> None:
        cmd = osv_scanner.build_command(Path("Cargo.lock"), Path("/tmp/osv.toml"))
        assert "--config" in cmd
        assert "/tmp/osv.toml" in cmd


class TestRepoConfigPath:
    """osv-scanner reads only the lockfile's own directory."""

    def test_beside_the_lockfile(self, tmp_path: Path) -> None:
        lockfile = tmp_path / "sub" / "Cargo.lock"
        assert osv_scanner.repo_config_path(lockfile) == (
            tmp_path / "sub" / "osv-scanner.toml"
        )

    def test_a_bare_lockfile_name_resolves_in_the_working_directory(self) -> None:
        assert osv_scanner.repo_config_path(Path("Cargo.lock")) == Path(
            "osv-scanner.toml"
        )


class TestMergeConfig:
    """Generated ignores are appended to the repo's own config, never replace it."""

    def test_repo_ignores_survive_beside_generated_ones(self, tmp_path: Path) -> None:
        repo = tmp_path / "osv-scanner.toml"
        repo.write_text(REPO_TOML, encoding="utf-8")

        merged, shadowed = osv_scanner.merge_config(
            repo.read_text(encoding="utf-8"), [_entry("MAL-2026-1")]
        )

        ids = [v["id"] for v in tomllib.loads(merged)["IgnoredVulns"]]
        assert ids == ["GHSA-w9wp-h8wv-79jx", "RUSTSEC-2024-0436", "MAL-2026-1"]
        assert shadowed == []

    def test_the_repo_text_is_kept_verbatim_comments_included(self) -> None:
        merged, _ = osv_scanner.merge_config(REPO_TOML, [_entry("MAL-2026-1")])
        assert merged.startswith(REPO_TOML)

    def test_every_other_setting_is_kept(self) -> None:
        merged, _ = osv_scanner.merge_config(
            REPO_TOML_ALL_SETTINGS, [_entry("MAL-2026-1")]
        )

        repo = tomllib.loads(REPO_TOML_ALL_SETTINGS)
        out = tomllib.loads(merged)
        assert out["GoVersionOverride"] == "1.23.4"
        assert out["ScanGoModVersion"] is True
        assert out["PackageOverrides"] == repo["PackageOverrides"]
        assert out["IgnoredVulns"][0] == repo["IgnoredVulns"][0]
        assert out["IgnoredVulns"][1]["id"] == "MAL-2026-1"
        assert set(out) == set(repo)

    def test_a_generated_block_after_a_subtable_lands_at_the_top_level(
        self,
    ) -> None:
        """The repo text above ends inside ``[PackageOverrides.license]``."""
        merged, _ = osv_scanner.merge_config(
            REPO_TOML_ALL_SETTINGS, [_entry("MAL-2026-1")]
        )
        out = tomllib.loads(merged)
        assert "IgnoredVulns" not in out["PackageOverrides"][1]["license"]
        assert len(out["PackageOverrides"]) == 2

    def test_on_a_duplicate_id_the_repo_entry_wins(self) -> None:
        merged, shadowed = osv_scanner.merge_config(
            REPO_TOML,
            [_entry("RUSTSEC-2024-0436", "generated reason"), _entry("MAL-1")],
        )

        vulns = tomllib.loads(merged)["IgnoredVulns"]
        dupes = [v for v in vulns if v["id"] == "RUSTSEC-2024-0436"]
        assert dupes == [
            {"id": "RUSTSEC-2024-0436", "reason": "compile-time proc macro"}
        ]
        assert shadowed == ["RUSTSEC-2024-0436"]
        assert [v["id"] for v in vulns][-1] == "MAL-1"

    def test_no_repo_config_gives_the_generated_config_alone(self) -> None:
        entries = [_entry("MAL-2026-1")]
        merged, shadowed = osv_scanner.merge_config("", entries)
        assert merged == osv_scanner.render_ignore_config(entries)
        assert shadowed == []

    def test_a_repo_file_with_no_trailing_newline_still_merges(self) -> None:
        merged, _ = osv_scanner.merge_config(
            REPO_TOML.rstrip("\n"), [_entry("MAL-2026-1")]
        )
        assert len(tomllib.loads(merged)["IgnoredVulns"]) == 3

    def test_an_inline_ignore_array_is_refused(self) -> None:
        """A table header cannot extend an array the repo declared inline."""
        repo = 'IgnoredVulns = [{ id = "GHSA-1", reason = "r" }]\n'
        with pytest.raises(osv_scanner.ConfigMergeError, match="cannot append"):
            osv_scanner.merge_config(repo, [_entry("MAL-2026-1")])

    def test_a_single_ignore_table_is_refused(self) -> None:
        repo = '[IgnoredVulns]\nid = "GHSA-1"\n'
        with pytest.raises(osv_scanner.ConfigMergeError, match="array of tables"):
            osv_scanner.merge_config(repo, [_entry("MAL-2026-1")])

    def test_invalid_toml_is_refused(self) -> None:
        with pytest.raises(osv_scanner.ConfigMergeError, match="not valid TOML"):
            osv_scanner.merge_config("[[IgnoredVulns]\n", [_entry("MAL-1")])


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> Any:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class _Runner:
    """Stands in for ``run_cmd``: records each call, returns a fixed exit code."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.result = _completed(returncode, stdout, stderr)
        self.cmds: list[list[str]] = []
        self.config_text: str | None = None

    def __call__(self, cmd: list[str], **_kwargs: Any) -> Any:
        self.cmds.append(cmd)
        if "--config" in cmd:
            config = Path(cmd[cmd.index("--config") + 1])
            self.config_text = config.read_text(encoding="utf-8")
        return self.result


class TestRun:
    """Orchestration: detect, (write config), run, apply the mode."""

    @staticmethod
    def _lockfile(tmp_path: Path, name: str = "Cargo.lock") -> Path:
        lf = tmp_path / name
        lf.write_text("# lockfile\n")
        return lf

    @staticmethod
    def _runner(
        monkeypatch: pytest.MonkeyPatch, returncode: int = 0, **kw: str
    ) -> _Runner:
        runner = _Runner(returncode, **kw)
        monkeypatch.setattr(osv_scanner, "available", lambda: True)
        monkeypatch.setattr(osv_scanner, "run_cmd", runner)
        return runner

    def test_disabled_mode_skips_without_running(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        runner = self._runner(monkeypatch, 1)
        ok = osv_scanner.run(self._lockfile(tmp_path), [], "disabled")
        assert ok is True
        assert runner.cmds == []

    def test_skips_and_passes_locally_when_binary_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        runner = self._runner(monkeypatch, 1)
        monkeypatch.setattr(osv_scanner, "available", lambda: False)
        monkeypatch.setattr(tools, "is_ci", lambda: False)
        ok = osv_scanner.run(self._lockfile(tmp_path), [], "blocking")
        assert ok is True
        assert runner.cmds == []

    def test_a_blocking_scan_with_no_binary_fails_in_ci(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A blocking gate that could not run has not passed."""
        said: list[str] = []
        monkeypatch.setattr(osv_scanner, "available", lambda: False)
        monkeypatch.setattr(tools, "is_ci", lambda: True)
        monkeypatch.setattr(tools, "error", said.append)
        ok = osv_scanner.run(self._lockfile(tmp_path), [], "blocking")
        assert ok is False
        assert any("osv-scanner" in s and "not installed" in s for s in said), said

    def test_a_warn_scan_with_no_binary_still_passes_in_ci(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(osv_scanner, "available", lambda: False)
        monkeypatch.setattr(tools, "is_ci", lambda: True)
        ok = osv_scanner.run(self._lockfile(tmp_path), [], "warn")
        assert ok is True

    def test_missing_lockfile_skips(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        runner = self._runner(monkeypatch, 1)
        ok = osv_scanner.run(tmp_path / "absent.lock", [], "warn")
        assert ok is True
        assert runner.cmds == []

    def test_a_missing_lockfile_does_not_read_as_a_clean_scan(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Skipped-unscannable and scanned-clean must not look the same.

        A repo osv-scanner cannot read is not a repo it cleared (issue #223).
        """
        said: list[str] = []
        self._runner(monkeypatch, 1)
        monkeypatch.setattr(osv_scanner, "warn", said.append)
        osv_scanner.run(tmp_path / "absent.lock", [], "warn")
        logged = "\n".join(said)
        assert "NOT SCANNED" in logged
        assert "not a clean result" in logged

    def test_blocking_says_out_loud_that_it_gated_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
    ) -> None:
        self._runner(monkeypatch, 1)
        monkeypatch.setattr(osv_scanner, "is_ci", lambda: True)
        osv_scanner.run(tmp_path / "absent.lock", [], "blocking")
        assert (
            "::warning title=osv-scanner scanned nothing::" in capsys.readouterr().out
        )

    def test_no_entries_means_no_config_flag(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        runner = self._runner(monkeypatch, 0)
        ok = osv_scanner.run(self._lockfile(tmp_path), [], "warn")
        assert ok is True
        assert "--config" not in runner.cmds[0]

    def test_entries_write_config_and_pass_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        runner = self._runner(monkeypatch, 0)
        osv_scanner.run(
            self._lockfile(tmp_path, "pnpm-lock.yaml"), [_entry("MAL-2026-1")], "warn"
        )
        assert runner.config_text is not None
        assert "MAL-2026-1" in runner.config_text

    def test_the_config_is_written_outside_the_checkout_and_removed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Left beside the lockfile, it showed up as untracked and got committed."""
        runner = self._runner(monkeypatch, 0)
        osv_scanner.run(self._lockfile(tmp_path), [_entry("MAL-2026-1")], "warn")
        cmd = runner.cmds[0]
        config = Path(cmd[cmd.index("--config") + 1])
        assert tmp_path not in config.parents
        assert not config.exists()
        assert not (tmp_path / "osv-scanner.toml").exists()

    def test_the_repo_config_is_merged_into_the_generated_one(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``--config`` stops osv-scanner reading the repo's file, so we carry it."""
        repo = tmp_path / "osv-scanner.toml"
        repo.write_text(REPO_TOML, encoding="utf-8")
        runner = self._runner(monkeypatch, 0)

        osv_scanner.run(self._lockfile(tmp_path), [_entry("MAL-2026-1")], "blocking")

        assert runner.config_text is not None
        ids = [v["id"] for v in tomllib.loads(runner.config_text)["IgnoredVulns"]]
        assert ids == ["GHSA-w9wp-h8wv-79jx", "RUSTSEC-2024-0436", "MAL-2026-1"]
        assert repo.read_text(encoding="utf-8") == REPO_TOML

    def test_an_unmergeable_repo_config_fails_a_blocking_scan_unrun(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "osv-scanner.toml").write_text(
            'IgnoredVulns = [{ id = "GHSA-1", reason = "r" }]\n', encoding="utf-8"
        )
        said: list[str] = []
        runner = self._runner(monkeypatch, 0)
        monkeypatch.setattr(osv_scanner, "error", said.append)

        ok = osv_scanner.run(self._lockfile(tmp_path), [_entry("MAL-1")], "blocking")

        assert ok is False
        assert runner.cmds == []
        assert any("NOT SCANNED" in s for s in said), said

    def test_an_unmergeable_repo_config_warns_in_warn_mode(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "osv-scanner.toml").write_text("[[broken\n", encoding="utf-8")
        said: list[str] = []
        runner = self._runner(monkeypatch, 0)
        monkeypatch.setattr(osv_scanner, "warn", said.append)

        ok = osv_scanner.run(self._lockfile(tmp_path), [_entry("MAL-1")], "warn")

        assert ok is True
        assert runner.cmds == []
        assert any("not valid TOML" in s for s in said), said

    def test_a_clean_scan_passes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._runner(monkeypatch, 0)
        assert osv_scanner.run(self._lockfile(tmp_path), [], "blocking") is True

    def test_a_finding_fails_a_blocking_scan(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._runner(monkeypatch, 1, stdout="1 known vulnerability")
        assert osv_scanner.run(self._lockfile(tmp_path), [], "blocking") is False

    def test_a_finding_passes_a_warn_scan(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        said: list[str] = []
        self._runner(monkeypatch, 1, stdout="1 known vulnerability")
        monkeypatch.setattr(osv_scanner, "warn", said.append)
        assert osv_scanner.run(self._lockfile(tmp_path), [], "warn") is True
        assert any("issues found" in s for s in said), said

    @pytest.mark.parametrize("mode", ["warn", "blocking"])
    def test_a_lockfile_with_no_packages_is_not_scanned_not_a_finding(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str
    ) -> None:
        """osv-scanner exits 128 on 0 packages, which is no result at all."""
        said: list[str] = []
        self._runner(
            monkeypatch,
            128,
            stderr="No package sources found, --help for usage information.",
        )
        monkeypatch.setattr(osv_scanner, "warn", said.append)
        monkeypatch.setattr(osv_scanner, "error", said.append)

        ok = osv_scanner.run(self._lockfile(tmp_path), [], mode)

        assert ok is True
        logged = "\n".join(said)
        assert "NOT SCANNED" in logged
        assert "lists no packages" in logged
        assert "issues found" not in logged

    def test_blocking_annotates_a_lockfile_with_no_packages_in_ci(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
    ) -> None:
        self._runner(monkeypatch, 128)
        monkeypatch.setattr(osv_scanner, "is_ci", lambda: True)
        osv_scanner.run(self._lockfile(tmp_path), [], "blocking")
        assert (
            "::warning title=osv-scanner scanned nothing::" in capsys.readouterr().out
        )

    @pytest.mark.parametrize("mode", ["warn", "blocking"])
    @pytest.mark.parametrize(
        ("returncode", "stderr"),
        [
            (129, "API query failed"),
            (127, OSV_DEV_UNREACHABLE_STDERR),
        ],
        ids=["exit-129", "exit-127-osvdev"],
    )
    def test_an_unreachable_osv_dev_is_not_scanned_not_a_finding(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        mode: str,
        returncode: int,
        stderr: str,
    ) -> None:
        """An outage is a scan that did not happen, never an advisory."""
        said: list[str] = []
        self._runner(monkeypatch, returncode, stderr=stderr)
        monkeypatch.setattr(osv_scanner, "warn", said.append)
        monkeypatch.setattr(osv_scanner, "error", said.append)

        ok = osv_scanner.run(self._lockfile(tmp_path), [], mode)

        assert ok is True
        logged = "\n".join(said)
        assert "NOT SCANNED" in logged
        assert "osv.dev could not be queried" in logged
        assert "issues found" not in logged
        assert "failed" not in logged

    def test_blocking_annotates_an_unreachable_osv_dev_in_ci(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
    ) -> None:
        self._runner(monkeypatch, 127, stderr=OSV_DEV_UNREACHABLE_STDERR)
        monkeypatch.setattr(osv_scanner, "is_ci", lambda: True)
        osv_scanner.run(self._lockfile(tmp_path), [], "blocking")
        assert (
            "::warning title=osv-scanner scanned nothing::" in capsys.readouterr().out
        )

    @pytest.mark.parametrize(
        ("mode", "passes", "said_via"),
        [("warn", True, "warn"), ("blocking", False, "error")],
    )
    def test_another_scanner_error_keeps_the_mode_but_is_not_a_finding(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        mode: str,
        passes: bool,
        said_via: str,
    ) -> None:
        """A 127 with no osv.dev failure in it is a scanner fault, not an outage."""
        said: list[str] = []
        self._runner(monkeypatch, 127, stderr="Failed to read config file: boom")
        monkeypatch.setattr(osv_scanner, said_via, said.append)

        ok = osv_scanner.run(self._lockfile(tmp_path), [], mode)

        assert ok is passes
        logged = "\n".join(said)
        assert "scanner error (exit 127), not a finding" in logged
        assert "NOT SCANNED" not in logged


@pytest.mark.skipif(not osv_scanner.available(), reason="osv-scanner not installed")
class TestRealBinary:
    """The real binary, on the one path that needs no network."""

    def test_a_package_lock_with_no_packages_exits_128(self, tmp_path: Path) -> None:
        """Pins the exit code the NOT SCANNED branch keys on."""
        lockfile = tmp_path / "package-lock.json"
        lockfile.write_text(
            '{"name": "x", "version": "1.0.0", "lockfileVersion": 3, '
            '"requires": true, "packages": {"": {"name": "x", "version": "1.0.0"}}}\n',
            encoding="utf-8",
        )
        result = run_cmd(osv_scanner.build_command(lockfile), check=False, capture=True)
        assert result.returncode == 128, result.stdout + result.stderr

    def test_run_reports_it_as_not_scanned(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        lockfile = tmp_path / "Cargo.lock"
        lockfile.write_text("version = 4\n", encoding="utf-8")
        said: list[str] = []
        monkeypatch.setattr(osv_scanner, "warn", said.append)

        ok = osv_scanner.run(lockfile, [_entry("MAL-1")], "blocking")

        assert ok is True
        assert any("lists no packages" in s for s in said), said

    @pytest.mark.slow
    def test_an_unreachable_osv_dev_is_reported_as_not_scanned(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Pins the exit the outage branch keys on; the retries take about 20s."""
        dead_proxy = "http://127.0.0.1:9"
        for var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            monkeypatch.setenv(var, dead_proxy)
        for var in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(var, raising=False)
        lockfile = tmp_path / "Cargo.lock"
        lockfile.write_text(
            'version = 3\n\n[[package]]\nname = "smallvec"\nversion = "0.6.9"\n'
            'source = "registry+https://github.com/rust-lang/crates.io-index"\n',
            encoding="utf-8",
        )
        said: list[str] = []
        monkeypatch.setattr(osv_scanner, "warn", said.append)
        monkeypatch.setattr(osv_scanner, "error", said.append)

        ok = osv_scanner.run(lockfile, [], "blocking")

        assert ok is True
        assert any("osv.dev could not be queried" in s for s in said), said
