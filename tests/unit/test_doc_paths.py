# Project:   HyperI CI
# File:      tests/unit/test_doc_paths.py
# Purpose:   Cover the path-existence drift check and its false-positive filter
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for :mod:`hyperi_ci.quality.doc_paths`.

Real markdown on disk throughout. The filter is the part worth testing: the
rule is only usable if ``/metrics`` and ``application/json`` stay quiet, so
those cases carry as much weight as the drift it is meant to catch.
"""

from __future__ import annotations

from pathlib import Path

from hyperi_ci.config import CIConfig
from hyperi_ci.quality import doc_paths


def _config(**overrides: object) -> CIConfig:
    return CIConfig(_raw={"quality": {"doc_paths": "warn", **overrides}})


def _repo(tmp_path: Path) -> Path:
    """A small repo: a docs dir of markdown and a config dir of YAML."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "defaults.yaml").write_text("a: 1\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    return tmp_path


class TestLinkDestinations:
    """A markdown link whose target is gone is an error."""

    def test_missing_relative_link_is_reported(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        doc = root / "README.md"
        doc.write_text("See [the guide](docs/gone.md).\n", encoding="utf-8")
        found = doc_paths.scan_links(doc, root)
        assert [f.rule for f in found] == ["docs/link-missing"]
        assert found[0].level == "error"
        assert found[0].line == 1

    def test_existing_link_is_silent(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        doc = root / "README.md"
        doc.write_text("See [the guide](docs/guide.md).\n", encoding="utf-8")
        assert doc_paths.scan_links(doc, root) == []

    def test_code_in_a_fence_is_not_a_link(self, tmp_path: Path) -> None:
        """A C++ lambda capture and a Python generic are valid link syntax."""
        root = _repo(tmp_path)
        doc = root / "README.md"
        doc.write_text(
            "```cpp\n"
            ".or_else([](const std::string & err) { return err; });\n"
            "```\n"
            "```python\n"
            "def first[T](items: list[T]) -> T | None: ...\n"
            "```\n",
            encoding="utf-8",
        )
        assert doc_paths.scan_links(doc, root) == []

    def test_a_link_after_a_fence_still_reports(self, tmp_path: Path) -> None:
        """Stripping a fence must not blind the scanner to what follows it."""
        root = _repo(tmp_path)
        doc = root / "README.md"
        doc.write_text(
            "```cpp\n[](int x) { return x; }\n```\nSee [gone](docs/gone.md).\n",
            encoding="utf-8",
        )
        found = doc_paths.scan_links(doc, root)
        assert [f.rule for f in found] == ["docs/link-missing"]
        assert found[0].line == 4

    def test_fragment_and_query_are_stripped_before_resolving(
        self, tmp_path: Path
    ) -> None:
        root = _repo(tmp_path)
        doc = root / "README.md"
        doc.write_text("[x](docs/guide.md#a-heading)\n", encoding="utf-8")
        assert doc_paths.scan_links(doc, root) == []

    def test_external_and_anchor_destinations_are_not_paths(
        self, tmp_path: Path
    ) -> None:
        root = _repo(tmp_path)
        doc = root / "README.md"
        doc.write_text(
            "[a](https://example.com/x) [b](#section) [c](mailto:x@y.z)\n",
            encoding="utf-8",
        )
        assert doc_paths.scan_links(doc, root) == []

    def test_reference_style_link_is_covered(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        doc = root / "README.md"
        doc.write_text("See [the guide][g].\n\n[g]: docs/gone.md\n", encoding="utf-8")
        assert [f.rule for f in doc_paths.scan_links(doc, root)] == [
            "docs/link-missing"
        ]

    def test_link_relative_to_the_doc_resolves(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        doc = root / "docs" / "index.md"
        doc.write_text("[sibling](guide.md)\n", encoding="utf-8")
        assert doc_paths.scan_links(doc, root) == []


class TestInlineCodePaths:
    """The filter that decides whether a slash-separated token is a claim."""

    def test_drift_in_a_directory_of_the_same_kind_is_reported(
        self, tmp_path: Path
    ) -> None:
        root = _repo(tmp_path)
        doc = root / "docs" / "a.md"
        doc.write_text("Settings live in `config/org.yaml`.\n", encoding="utf-8")
        found = doc_paths.scan_code_paths(doc, root)
        assert [f.rule for f in found] == ["docs/path-missing"]
        assert found[0].level == "warning"

    def test_a_kind_the_directory_has_never_held_is_another_repo(
        self, tmp_path: Path
    ) -> None:
        # src/ holds Python, never Rust, so `src/main.rs` describes a consumer.
        root = _repo(tmp_path)
        doc = root / "docs" / "a.md"
        doc.write_text("Your binary is `src/main.rs`.\n", encoding="utf-8")
        assert doc_paths.scan_code_paths(doc, root) == []

    def test_an_existing_path_is_silent(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        doc = root / "docs" / "a.md"
        doc.write_text("See `config/defaults.yaml`.\n", encoding="utf-8")
        assert doc_paths.scan_code_paths(doc, root) == []

    def test_routes_and_slash_commands_are_not_paths(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        doc = root / "docs" / "a.md"
        doc.write_text(
            "Probe `/healthz`, `/readyz` and `/metrics`; run `/deps`.\n",
            encoding="utf-8",
        )
        assert doc_paths.scan_code_paths(doc, root) == []

    def test_a_bare_directory_name_is_not_a_path(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        doc = root / "docs" / "a.md"
        doc.write_text(
            "Build output lands in `target/` and `dist/`.\n", encoding="utf-8"
        )
        assert doc_paths.scan_code_paths(doc, root) == []

    def test_a_mime_type_is_not_a_path(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        doc = root / "docs" / "a.md"
        doc.write_text("Send `application/json`.\n", encoding="utf-8")
        assert doc_paths.scan_code_paths(doc, root) == []

    def test_a_template_placeholder_is_not_a_path(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        doc = root / "docs" / "a.md"
        doc.write_text(
            "Edit `config/<name>.yaml` and `config/*.yaml`.\n", encoding="utf-8"
        )
        assert doc_paths.scan_code_paths(doc, root) == []

    def test_paths_inside_a_fenced_block_are_samples_not_claims(
        self, tmp_path: Path
    ) -> None:
        root = _repo(tmp_path)
        doc = root / "docs" / "a.md"
        doc.write_text(
            "Run it:\n\n```bash\ncat `config/org.yaml`\n```\n", encoding="utf-8"
        )
        assert doc_paths.scan_code_paths(doc, root) == []

    def test_a_repeated_token_is_reported_once_per_doc(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        doc = root / "docs" / "a.md"
        doc.write_text(
            "`config/org.yaml` and again `config/org.yaml`.\n", encoding="utf-8"
        )
        assert len(doc_paths.scan_code_paths(doc, root)) == 1


class TestLooksLikeAFile:
    """The segment + extension rule, stated directly."""

    def test_two_segments_with_an_extension_qualify(self) -> None:
        assert doc_paths.looks_like_a_file("config/org.yaml")

    def test_a_single_segment_does_not(self) -> None:
        assert not doc_paths.looks_like_a_file("/metrics")

    def test_a_trailing_slash_does_not(self) -> None:
        assert not doc_paths.looks_like_a_file("src/hyperi_ci/")

    def test_no_extension_does_not(self) -> None:
        assert not doc_paths.looks_like_a_file("scripts/dfe-stack")

    def test_a_leading_dot_slash_is_stripped_before_counting(self) -> None:
        assert not doc_paths.looks_like_a_file("./Chart.yaml")


class TestRun:
    """Gate semantics: warn reports, blocking fails, disabled is silent."""

    def test_warn_reports_without_failing(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        doc = root / "README.md"
        doc.write_text("[x](docs/gone.md)\n", encoding="utf-8")
        assert doc_paths.run([doc], _config(), root=root) == 0

    def test_blocking_fails_on_a_missing_link(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        doc = root / "README.md"
        doc.write_text("[x](docs/gone.md)\n", encoding="utf-8")
        assert doc_paths.run([doc], _config(doc_paths="blocking"), root=root) == 1

    def test_blocking_does_not_fail_on_a_warning_level_finding(
        self, tmp_path: Path
    ) -> None:
        # An inline-code path is inferred, not declared, so it stays advisory
        # even where the check gates.
        root = _repo(tmp_path)
        doc = root / "docs" / "a.md"
        doc.write_text("See `config/org.yaml`.\n", encoding="utf-8")
        assert doc_paths.run([doc], _config(doc_paths="blocking"), root=root) == 0

    def test_disabled_runs_nothing(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        doc = root / "README.md"
        doc.write_text("[x](docs/gone.md)\n", encoding="utf-8")
        assert doc_paths.run([doc], _config(doc_paths="disabled"), root=root) == 0

    def test_check_links_false_leaves_links_to_lychee(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        doc = root / "README.md"
        doc.write_text("[x](docs/gone.md)\n", encoding="utf-8")
        rc = doc_paths.run(
            [doc], _config(doc_paths="blocking"), root=root, check_links=False
        )
        assert rc == 0

    def test_no_files_is_a_clean_skip(self, tmp_path: Path) -> None:
        assert doc_paths.run([], _config(), root=tmp_path) == 0
