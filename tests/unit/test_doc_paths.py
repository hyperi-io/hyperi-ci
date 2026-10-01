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

from pathlib import Path

import pytest

from hyperi_ci.config import CIConfig
from hyperi_ci.quality import doc_paths
from hyperi_ci.quality import findings as fdg


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


class TestIgnoreMarker:
    """`<!-- doc-paths: ignore -->` suppresses the warnings on its own line."""

    def test_a_marked_bad_path_is_not_reported(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        doc = root / "docs" / "a.md"
        doc.write_text(
            "See `config/org.yaml` <!-- doc-paths: ignore -->.\n", encoding="utf-8"
        )
        assert doc_paths.scan_code_paths(doc, root) == []

    def test_an_unmarked_bad_path_on_another_line_still_reports(
        self, tmp_path: Path
    ) -> None:
        root = _repo(tmp_path)
        doc = root / "docs" / "a.md"
        doc.write_text(
            "See `config/org.yaml` <!-- doc-paths: ignore -->.\n"
            "Also see `config/other.yaml`.\n",
            encoding="utf-8",
        )
        found = doc_paths.scan_code_paths(doc, root)
        assert [f.message for f in found] == [
            "names `config/other.yaml`, which is no longer in the repo"
        ]
        assert found[0].line == 2

    def test_a_marker_does_not_suppress_the_next_line(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        doc = root / "docs" / "a.md"
        doc.write_text(
            "<!-- doc-paths: ignore -->\nSee `config/org.yaml`.\n", encoding="utf-8"
        )
        found = doc_paths.scan_code_paths(doc, root)
        assert [f.message for f in found] == [
            "names `config/org.yaml`, which is no longer in the repo"
        ]

    def test_a_marked_bad_link_is_not_reported(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        doc = root / "README.md"
        doc.write_text(
            "See [the guide](docs/gone.md) <!-- doc-paths: ignore -->.\n",
            encoding="utf-8",
        )
        assert doc_paths.scan_links(doc, root) == []

    def test_a_later_unmarked_occurrence_of_a_marked_path_still_reports(
        self, tmp_path: Path
    ) -> None:
        # The marked occurrence must not populate `seen` and hide a real one.
        root = _repo(tmp_path)
        doc = root / "docs" / "a.md"
        doc.write_text(
            "First `config/org.yaml` <!-- doc-paths: ignore -->.\n"
            "Later also `config/org.yaml`.\n",
            encoding="utf-8",
        )
        found = doc_paths.scan_code_paths(doc, root)
        assert [f.message for f in found] == [
            "names `config/org.yaml`, which is no longer in the repo"
        ]
        assert found[0].line == 2

    def test_a_later_unmarked_link_occurrence_still_reports(
        self, tmp_path: Path
    ) -> None:
        root = _repo(tmp_path)
        doc = root / "README.md"
        doc.write_text(
            "First [x](docs/gone.md) <!-- doc-paths: ignore -->.\n"
            "Later [y](docs/gone.md) too.\n",
            encoding="utf-8",
        )
        found = doc_paths.scan_links(doc, root)
        assert [f.rule for f in found] == ["docs/link-missing"]
        assert found[0].line == 2

    def test_the_suppressed_counter_tallies_both_kinds(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        doc = root / "docs" / "a.md"
        doc.write_text(
            "See `config/org.yaml` <!-- doc-paths: ignore -->.\n"
            "And [x](docs/gone.md) <!-- doc-paths: ignore -->.\n",
            encoding="utf-8",
        )
        counter = doc_paths._Suppressed()
        assert doc_paths.scan_links(doc, root, suppressed=counter) == []
        assert doc_paths.scan_code_paths(doc, root, suppressed=counter) == []
        assert counter.count == 2

    def test_run_reports_zero_findings_and_logs_the_suppressed_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Loguru bypasses capsys/capfd/caplog, so `info` is monkeypatched.
        lines: list[str] = []
        monkeypatch.setattr(doc_paths, "info", lines.append)
        root = _repo(tmp_path)
        doc = root / "docs" / "a.md"
        doc.write_text(
            "See `config/org.yaml` <!-- doc-paths: ignore -->.\n", encoding="utf-8"
        )
        assert doc_paths.run([doc], _config(), root=root) == 0
        assert any("1 reference(s) ignored by marker" in line for line in lines)

    def test_a_marked_good_path_stays_silent_and_uncounted(
        self, tmp_path: Path
    ) -> None:
        root = _repo(tmp_path)
        doc = root / "docs" / "a.md"
        doc.write_text(
            "See `config/defaults.yaml` <!-- doc-paths: ignore -->.\n",
            encoding="utf-8",
        )
        counter = doc_paths._Suppressed()
        assert doc_paths.scan_code_paths(doc, root, suppressed=counter) == []
        assert counter.count == 0


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

    def test_lychee_at_the_same_mode_owns_the_link(self, tmp_path: Path) -> None:
        # Both would gate, so reporting it here as well only doubles the line.
        root = _repo(tmp_path)
        doc = root / "README.md"
        doc.write_text("[x](docs/gone.md)\n", encoding="utf-8")
        rc = doc_paths.run(
            [doc], _config(doc_paths="blocking"), root=root, lychee_mode="blocking"
        )
        assert rc == 0

    def test_a_promoted_check_keeps_the_link_from_a_warn_only_lychee(
        self, tmp_path: Path
    ) -> None:
        # lychee at `warn` can never fail the run, so handing it the link would
        # make `doc_paths: blocking` unable to fail on the one finding it gates.
        root = _repo(tmp_path)
        doc = root / "README.md"
        doc.write_text("[x](docs/gone.md)\n", encoding="utf-8")
        rc = doc_paths.run(
            [doc], _config(doc_paths="blocking"), root=root, lychee_mode="warn"
        )
        assert rc == 1

    def test_no_files_is_a_clean_skip(self, tmp_path: Path) -> None:
        assert doc_paths.run([], _config(), root=tmp_path) == 0


def _prescriptive(dirs: object, mode: str = "warn") -> CIConfig:
    return _config(doc_paths={"mode": mode, "prescriptive": dirs})


def _standard(root: Path, rel: str, body: str) -> Path:
    doc = root / rel
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(body, encoding="utf-8")
    return doc


class TestPrescriptive:
    """Directories whose inline-code paths name the consumer's tree, not this one."""

    # `config/org.yaml` is drift by every other rule: a YAML file missing from a
    # directory of YAML.
    RULE = "Your settings belong in `config/org.yaml`.\n"

    @pytest.fixture(autouse=True)
    def _capture(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.caught: list[fdg.Finding] = []

        def surface(tool: str, found: list[fdg.Finding], **_: object) -> int:
            self.caught.extend(found)
            return 0

        monkeypatch.setattr(doc_paths.fdg, "surface", surface)

    def _found(self, root: Path, docs: list[Path], config: CIConfig) -> list[str]:
        self.caught.clear()
        doc_paths.run(docs, config, root=root)
        return [Path(f.path).relative_to(root).as_posix() for f in self.caught]

    def test_a_listed_dir_is_not_reported(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        doc = _standard(root, "standards/ci.md", self.RULE)
        assert self._found(root, [doc], _prescriptive(["standards"])) == []

    def test_the_same_path_outside_a_listed_dir_is_reported(
        self, tmp_path: Path
    ) -> None:
        root = _repo(tmp_path)
        rule = _standard(root, "standards/ci.md", self.RULE)
        stale = _standard(root, "docs/a.md", self.RULE)
        found = self._found(root, [rule, stale], _prescriptive(["standards"]))
        assert found == ["docs/a.md"]

    def test_the_empty_default_reports_as_before(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        doc = _standard(root, "standards/ci.md", self.RULE)
        assert doc_paths.prescriptive_dirs(_config()) == []
        assert self._found(root, [doc], _config()) == ["standards/ci.md"]

    def test_a_prefix_does_not_swallow_a_sibling(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        doc = _standard(root, "docs-old/a.md", self.RULE)
        assert self._found(root, [doc], _prescriptive(["docs"])) == ["docs-old/a.md"]

    def test_a_nested_dir_and_its_spellings_match(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        doc = _standard(root, "standards/rules/ci.md", self.RULE)
        for entry in ("standards", "standards/", "./standards", "standards/rules"):
            assert self._found(root, [doc], _prescriptive([entry])) == [], entry

    def test_link_destinations_are_still_checked(self, tmp_path: Path) -> None:
        # A link is navigation within this repo, so a broken one is a 404 here.
        root = _repo(tmp_path)
        doc = _standard(root, "standards/ci.md", "See [x](docs/gone.md).\n")
        config = _prescriptive(["standards"], mode="blocking")
        assert doc_paths.run([doc], config, root=root) == 1

    def test_a_relative_root_matches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _repo(tmp_path)
        _standard(root, "standards/ci.md", self.RULE)
        monkeypatch.chdir(root)
        dirs = doc_paths.prescriptive_dirs(_prescriptive(["standards"]))
        assert doc_paths.is_prescriptive(Path("standards/ci.md"), Path("."), dirs)

    def test_a_bare_mode_string_leaves_the_list_empty(self) -> None:
        assert doc_paths.prescriptive_dirs(_config(doc_paths="blocking")) == []

    def test_a_malformed_value_fails_the_check(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        doc = _standard(root, "docs/a.md", "# ok\n")
        for bad in ("standards", ["/standards"], ["."], ["../x"], ["std/*"], [1], [""]):
            assert doc_paths.run([doc], _prescriptive(bad), root=root) == 1, bad
