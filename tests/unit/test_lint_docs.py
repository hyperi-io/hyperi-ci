# Project:   HyperI CI
# File:      tests/unit/test_lint_docs.py
# Purpose:   Cover doc discovery, the lychee/markdownlint parsers, the nudge
#            and the orchestrator
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for the documentation checking dimension.

Covers :mod:`hyperi_ci.quality.targets` doc discovery, the two external-tool
output parsers, the docs-untouched nudge and the orchestrator. The parsers are
fed REAL captured output from lychee 0.24.2 and markdownlint-cli2 0.23.3 rather
than a mock, so a change in either tool's shape shows up here.
"""

import json
import os
from pathlib import Path

from hyperi_ci.config import CIConfig
from hyperi_ci.quality import doc_links, docs_touched, lint_docs, markdownlint
from hyperi_ci.quality.targets import discover_markdown_files


def _config(**quality: object) -> CIConfig:
    return CIConfig(_raw={"quality": quality})


class TestDiscovery:
    """What counts as a doc, and what is deliberately left alone."""

    def test_markdown_anywhere_in_the_tree_is_found(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("# a\n", encoding="utf-8")
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "guide.markdown").write_text("# b\n", encoding="utf-8")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "notes.md").write_text("# c\n", encoding="utf-8")
        found = discover_markdown_files(tmp_path)
        assert [p.name for p in found] == ["README.md", "guide.markdown", "notes.md"]

    def test_generated_and_upstream_files_are_skipped(self, tmp_path: Path) -> None:
        for name in ("CHANGELOG.md", "LICENSE.md", "NOTICE.md", "README.md"):
            (tmp_path / name).write_text("# x\n", encoding="utf-8")
        assert [p.name for p in discover_markdown_files(tmp_path)] == ["README.md"]

    def test_fixture_markdown_is_test_data_not_docs(self, tmp_path: Path) -> None:
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "fixtures").mkdir()
        (tmp_path / "tests" / "fixtures" / "broken.md").write_text(
            "[x](gone.md)\n", encoding="utf-8"
        )
        (tmp_path / "README.md").write_text("# x\n", encoding="utf-8")
        assert [p.name for p in discover_markdown_files(tmp_path)] == ["README.md"]

    def test_worktrees_and_node_modules_never_descend(self, tmp_path: Path) -> None:
        for noisy in (".worktrees", "node_modules", ".git"):
            (tmp_path / noisy).mkdir()
            (tmp_path / noisy / "doc.md").write_text("# x\n", encoding="utf-8")
        assert discover_markdown_files(tmp_path) == []

    def test_a_configured_exclusion_is_pruned(self, tmp_path: Path) -> None:
        (tmp_path / "vendored").mkdir()
        (tmp_path / "vendored" / "a.md").write_text("# x\n", encoding="utf-8")
        assert discover_markdown_files(tmp_path, exclude_dirs=["vendored/"]) == []


class TestLycheeParsing:
    """Captured lychee 0.24.2 --format json output."""

    REAL_OUTPUT = json.dumps(
        {
            "total": 5,
            "errors": 2,
            "error_map": {
                "docs/a.md": [
                    {
                        "url": "file:///repo/docs/nope.md",
                        "status": {
                            "text": "File not found. Check if file exists and path is correct"
                        },
                        "span": {"line": 3, "column": 29},
                    },
                    {
                        "url": "file:///repo/docs/b.md#no-such-heading",
                        "status": {"text": "Cannot find fragment"},
                        "span": {"line": 5, "column": 31},
                    },
                ]
            },
            "excluded_map": {
                "docs/a.md": [
                    {
                        "url": "https://example.com/",
                        "status": {"text": "Excluded"},
                        "span": {"line": 7, "column": 4},
                    }
                ]
            },
        }
    )

    def test_both_error_kinds_are_parsed_with_their_line(self) -> None:
        found = doc_links.parse(self.REAL_OUTPUT)
        assert len(found) == 2
        assert [f.line for f in found] == [3, 5]
        assert all(f.level == "error" for f in found)
        assert all(f.rule == "docs/broken-link" for f in found)

    def test_an_offline_excluded_link_is_not_a_finding(self) -> None:
        messages = " ".join(f.message for f in doc_links.parse(self.REAL_OUTPUT))
        assert "example.com" not in messages

    def test_a_clean_run_parses_to_nothing(self) -> None:
        assert doc_links.parse(json.dumps({"total": 0, "error_map": {}})) == []

    def test_unparseable_output_yields_nothing_rather_than_raising(self) -> None:
        assert doc_links.parse("not json at all") == []
        assert doc_links.parse("") == []


class TestMarkdownlintParsing:
    """Captured markdownlint-cli2 0.23.3 text output."""

    REAL_OUTPUT = "\n".join(
        [
            "markdownlint-cli2 v0.23.3 (markdownlint v0.41.1)",
            "Finding: docs/bad.md",
            "Linting: 1 file",
            "Summary: 4 issues in 1 file",
            'docs/bad.md:3 error MD022/blanks-around-headings Headings should be surrounded by blank lines [Context: "## Two"]',
            "docs/bad.md:5:1 error MD030/list-marker-space Spaces after list markers [Expected: 1; Actual: 3]",
            "docs/bad.md:6:21 error MD009/no-trailing-spaces Trailing spaces [Expected: 0 or 2; Actual: 3]",
        ]
    )

    def test_findings_are_parsed_and_banner_lines_are_not(self) -> None:
        found = markdownlint.parse(self.REAL_OUTPUT)
        assert len(found) == 3
        assert [f.line for f in found] == [3, 5, 6]

    def test_the_rule_id_keeps_its_alias_and_gains_a_docs_url(self) -> None:
        found = markdownlint.parse(self.REAL_OUTPUT)
        assert found[0].rule == "MD022/blanks-around-headings"
        assert found[0].url.endswith("/MD022.md")

    def test_a_clean_run_parses_to_nothing(self) -> None:
        assert markdownlint.parse("Summary: 0 issues\n") == []

    def test_a_repo_config_wins_over_the_shipped_default(self, tmp_path: Path) -> None:
        assert markdownlint.repo_config(tmp_path) is None
        (tmp_path / ".markdownlint.yaml").write_text("MD013: false\n", encoding="utf-8")
        assert markdownlint.repo_config(tmp_path) == tmp_path / ".markdownlint.yaml"

    def test_the_shipped_default_exists_and_disables_the_width_rule(self) -> None:
        # The house style has no markdown width limit, so a line-length rule
        # would report every correctly written paragraph.
        text = markdownlint.DEFAULT_CONFIG.read_text(encoding="utf-8")
        assert "MD013: false" in text

    def test_the_wheel_carries_the_default_config(self) -> None:
        """A wheel without it makes every consumer inherit MD013 - assert it ships."""
        root = Path(__file__).resolve().parents[2]
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
        assert 'packages = ["src/hyperi_ci"]' in pyproject
        assert markdownlint.DEFAULT_CONFIG.is_relative_to(root / "src" / "hyperi_ci")


class TestDocsTouched:
    """The nudge classifies, and never gates."""

    def test_source_changed_and_docs_did_not(self) -> None:
        source, docs = docs_touched.classify(["src/app.py", "src/lib.rs"])
        assert len(source) == 2
        assert docs == []

    def test_a_doc_change_clears_it(self) -> None:
        source, docs = docs_touched.classify(["src/app.py", "README.md"])
        assert source and docs

    def test_a_test_change_is_neither(self) -> None:
        source, docs = docs_touched.classify(
            ["tests/test_app.py", "src/app_test.go", "a.spec.ts"]
        )
        assert source == []
        assert docs == []

    def test_a_lockfile_is_neither(self) -> None:
        source, docs = docs_touched.classify(["uv.lock", "Cargo.toml"])
        assert source == []
        assert docs == []

    def test_it_never_returns_non_zero(self, tmp_path: Path) -> None:
        # No git repo at tmp_path, so there is no comparison; and even with
        # one, this check has no failing branch.
        assert docs_touched.run(_config(docs_touched="blocking"), root=tmp_path) == 0

    def test_disabled_runs_nothing(self, tmp_path: Path) -> None:
        assert docs_touched.run(_config(docs_touched="disabled"), root=tmp_path) == 0

    def test_the_pr_base_ref_is_preferred(self, monkeypatch) -> None:
        monkeypatch.setenv("GITHUB_BASE_REF", "develop")
        assert docs_touched.base_ref() == "origin/develop"
        monkeypatch.delenv("GITHUB_BASE_REF")
        assert docs_touched.base_ref() == "origin/HEAD"


class TestOrchestrator:
    """Whole-dimension behaviour."""

    def test_a_repo_with_no_markdown_skips_cleanly(self, tmp_path: Path) -> None:
        (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
        assert lint_docs.run(tmp_path, _config()) == 0

    def test_defaults_never_fail_a_repo_with_broken_docs(self, tmp_path: Path) -> None:
        # The ratchet's whole point: adopting the dimension reports, it does
        # not turn an existing tree red.
        (tmp_path / "README.md").write_text(
            "[gone](docs/nope.md)\n\n```mermaid\ngraph TD\n  A[x --> B{{{\n",
            encoding="utf-8",
        )
        assert lint_docs.run(tmp_path, CIConfig()) == 0

    def test_a_promoted_check_gates(self, tmp_path: Path) -> None:
        # Both link checks are promoted, because the orchestrator hands the
        # link rule to lychee wherever lychee is installed.
        (tmp_path / "README.md").write_text("[gone](docs/nope.md)\n", encoding="utf-8")
        config = _config(doc_paths="blocking", doc_links="blocking")
        assert lint_docs.run(tmp_path, config) == 1

    def test_promoting_doc_paths_alone_still_gates_with_lychee_installed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """`doc_paths: blocking` beside the default `doc_links: warn`.

        On 2.10.10 this config failed a broken link. Handing the link to a
        lychee that only warns left nothing able to fail it.
        """
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        lychee = bin_dir / "lychee"
        report = json.dumps(
            {"error_map": {"README.md": [{"url": "docs/nope.md", "span": {}}]}}
        )
        lychee.write_text(f"#!/bin/sh\necho '{report}'\nexit 2\n", encoding="utf-8")
        lychee.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "README.md").write_text("[gone](docs/nope.md)\n", encoding="utf-8")
        assert doc_links.planned_mode(_config(doc_paths="blocking")) == "warn"
        assert lint_docs.run(repo, _config(doc_paths="blocking")) == 1

    def test_every_check_defaults_to_warn(self) -> None:
        # Guards the ratchet at the config layer: a default flipped to blocking
        # would fail consumer CI on adoption.
        from hyperi_ci.config import load_config

        defaults = load_config()
        for tool in (
            "doc_paths",
            "doc_links",
            "mermaid_parse",
            "markdownlint",
            "docs_touched",
        ):
            assert defaults.get(f"quality.{tool}") == "warn", tool


class TestLycheeInstall:
    """lychee had no install path anywhere, so the check never ran (issue #230)."""

    def test_the_url_uses_the_lychee_prefixed_tag(self, monkeypatch) -> None:
        """Upstream tags `lychee-vX.Y.Z`, so the tag is not the version with a v."""
        seen: dict[str, object] = {}

        def fake_install(name, url, *, tar_member=None, expected_sha256=None):
            seen.update(name=name, url=url, member=tar_member, sha=expected_sha256)
            return "/usr/local/bin/lychee"

        monkeypatch.setattr(doc_links, "install_ci_binary", fake_install)
        assert doc_links._install_lychee() == "/usr/local/bin/lychee"
        assert "/releases/download/lychee-v" in str(seen["url"])
        assert str(seen["url"]).endswith("-unknown-linux-musl.tar.gz")
        assert seen["member"] == "lychee"
        assert seen["sha"]

    def test_a_disabled_check_installs_nothing(self, monkeypatch) -> None:
        called: list[int] = []
        monkeypatch.setattr(doc_links, "_install_lychee", lambda: called.append(1))
        config = CIConfig(_raw={"quality": {"doc_links": "disabled"}})
        assert doc_links.planned_mode(config) is None
        assert called == []

    def test_planned_mode_resolves_the_binary_so_doc_paths_is_not_doubled(
        self, monkeypatch
    ) -> None:
        """planned_mode is asked BEFORE run, so it must install to answer honestly.

        Answering "no" and then installing makes doc_paths report every broken
        link a second time.
        """
        monkeypatch.setattr(doc_links.shutil, "which", lambda _n: None)
        monkeypatch.setattr(
            doc_links, "_install_lychee", lambda: "/usr/local/bin/lychee"
        )
        config = CIConfig(_raw={"quality": {"doc_links": "warn"}})
        assert doc_links.planned_mode(config) == "warn"
