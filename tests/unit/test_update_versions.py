# Project:   HyperI CI
# File:      tests/unit/test_update_versions.py
# Purpose:   Tests for the action-version SSOT sync regexes
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/update-versions.py version-pin regexes."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "update_versions",
    Path(__file__).resolve().parents[2] / "scripts" / "update-versions.py",
)
assert _SPEC is not None and _SPEC.loader is not None  # always resolves for a real file
update_versions = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(update_versions)


def _apply(text: str, versions: dict) -> str:
    for pattern, replacement, _desc in update_versions._build_replacements(versions):
        text = pattern.sub(replacement, text)
    return text


class TestSemanticReleasePin:
    def test_pins_bare_npm_package(self) -> None:
        out = _apply(
            "npm i -g semantic-release@20", {"semantic_release": {"core": "25"}}
        )
        assert "semantic-release@25" in out

    def test_does_not_touch_setup_semantic_release_action_ref(self) -> None:
        # Regression: the action name ends in "semantic-release"; the npm pin
        # regex must not rewrite the action ref's @main to @25.
        ref = "uses: hyperi-io/hyperi-ci/.github/actions/setup-semantic-release@main"
        out = _apply(ref, {"semantic_release": {"core": "25"}})
        assert out == ref


def _sha_versions(sha: str = "abc123", version: str = "v6.0.2") -> dict:
    return {"actions": {"checkout": {"version": version, "sha": sha}}}


class TestActionShaPin:
    """Actions pin to a commit SHA with a `# <version>` comment."""

    def test_pins_sha_with_version_comment(self) -> None:
        out = _apply("  uses: actions/checkout@v6\n", _sha_versions())
        assert "uses: actions/checkout@abc123 # v6.0.2" in out

    def test_idempotent(self) -> None:
        once = _apply("  uses: actions/checkout@v6\n", _sha_versions())
        twice = _apply(once, _sha_versions())
        assert once == twice

    def test_replaces_existing_sha_pin_and_comment(self) -> None:
        out = _apply(
            "  uses: actions/checkout@oldsha # v6.0.1\n",
            _sha_versions(sha="newsha", version="v6.0.2"),
        )
        assert "uses: actions/checkout@newsha # v6.0.2" in out
        assert "oldsha" not in out
        assert "v6.0.1" not in out

    def test_consumes_multitoken_comment(self) -> None:
        # The pre-pin rust-toolchain comment had several tokens — the rewrite
        # must consume the whole trailing comment, not leave a fragment.
        versions = {
            "actions": {"rust-toolchain": {"version": "master", "sha": "deadbeef"}}
        }
        out = _apply(
            "  uses: dtolnay/rust-toolchain@master # master pinned 2026-05-28\n",
            versions,
        )
        assert "uses: dtolnay/rust-toolchain@deadbeef # master\n" in out
        assert "pinned 2026-05-28" not in out

    def test_does_not_touch_other_owners(self) -> None:
        ref = "  uses: actions/setup-go@v6\n"
        assert _apply(ref, _sha_versions()) == ref

    def test_string_value_keeps_tag_pin_back_compat(self) -> None:
        # Old flat format (version string, no sha) still pins the tag.
        out = _apply("  uses: actions/checkout@v5\n", {"actions": {"checkout": "v6"}})
        assert "uses: actions/checkout@v6" in out


from datetime import UTC, datetime  # noqa: E402


def _rel(
    tag: str, days_ago: int, *, prerelease: bool = False, draft: bool = False
) -> dict:
    from datetime import timedelta

    ts = (datetime(2026, 5, 28, tzinfo=UTC) - timedelta(days=days_ago)).isoformat()
    return {
        "tag_name": tag,
        "published_at": ts,
        "prerelease": prerelease,
        "draft": draft,
    }


class TestCooldownSelection:
    """Pick the newest release that has aged past the cooldown."""

    NOW = datetime(2026, 5, 28, tzinfo=UTC)

    def test_skips_releases_younger_than_cooldown(self) -> None:
        # newest is 2 days old (too fresh) → fall back to the 10-day-old one
        releases = [_rel("v8.1.0", 2), _rel("v8.0.0", 10), _rel("v7.0.0", 60)]
        sel = update_versions._select_pinned_release(releases, self.NOW, 7)
        assert sel["tag_name"] == "v8.0.0"

    def test_returns_newest_when_all_aged(self) -> None:
        releases = [_rel("v8.1.0", 8), _rel("v8.0.0", 30)]
        sel = update_versions._select_pinned_release(releases, self.NOW, 7)
        assert sel["tag_name"] == "v8.1.0"

    def test_skips_prerelease_and_draft(self) -> None:
        releases = [
            _rel("v9.0.0-rc1", 20, prerelease=True),
            _rel("v8.9.9", 20, draft=True),
            _rel("v8.0.0", 20),
        ]
        sel = update_versions._select_pinned_release(releases, self.NOW, 7)
        assert sel["tag_name"] == "v8.0.0"

    def test_none_when_all_too_fresh(self) -> None:
        releases = [_rel("v8.1.0", 1), _rel("v8.0.0", 3)]
        assert update_versions._select_pinned_release(releases, self.NOW, 7) is None

    def test_none_when_empty(self) -> None:
        assert update_versions._select_pinned_release([], self.NOW, 7) is None

    def test_missing_timestamp_skipped(self) -> None:
        # timestamp-required posture: no published_at → not eligible
        bad = {
            "tag_name": "v9",
            "published_at": None,
            "prerelease": False,
            "draft": False,
        }
        releases = [bad, _rel("v8.0.0", 20)]
        sel = update_versions._select_pinned_release(releases, self.NOW, 7)
        assert sel["tag_name"] == "v8.0.0"

    def test_highest_semver_not_newest_published(self) -> None:
        # The download-artifact bug: an old backport republished most recently
        # must NOT outrank the real latest. Order is newest-published first.
        releases = [_rel("v3.1.0-node20", 20), _rel("v8.0.1", 60), _rel("v8.0.0", 90)]
        sel = update_versions._select_pinned_release(releases, self.NOW, 7)
        assert sel["tag_name"] == "v8.0.1"

    def test_skips_non_semver_tags(self) -> None:
        releases = [_rel("v3.1.0-node20", 30), _rel("nightly", 30)]
        assert update_versions._select_pinned_release(releases, self.NOW, 7) is None

    def test_major_filter_stays_within_major(self) -> None:
        # A surprise new major that's aged must not auto-win when pinned to v8.
        releases = [_rel("v9.0.0", 20), _rel("v8.2.0", 20)]
        sel = update_versions._select_pinned_release(releases, self.NOW, 7, major=8)
        assert sel["tag_name"] == "v8.2.0"


class TestValidateLocally:
    """Local gates for --auto-update (replaces the remote ci-test-* trigger,
    which validated @main rather than the unpushed bumps)."""

    def _bad_tree(self, tmp_path: Path, content: str) -> Path:
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        (wf_dir / "broken.yml").write_text(content)
        return wf_dir

    def test_catches_unparseable_yaml(self, monkeypatch, tmp_path: Path) -> None:
        wf_dir = self._bad_tree(tmp_path, "jobs:\n  x: [unclosed\n")
        monkeypatch.setattr(update_versions, "_ROOT", tmp_path)
        monkeypatch.setattr(update_versions, "_WORKFLOWS_DIR", wf_dir)
        monkeypatch.setattr(update_versions, "_ACTIONS_DIR", tmp_path / "none")
        failures = update_versions._validate_locally()
        assert len(failures) == 1
        assert failures[0].startswith("YAML parse:")

    def test_catches_ssot_drift(self, monkeypatch, tmp_path: Path) -> None:
        # A wrong SHA against the real versions.yaml SSOT = drift. This is
        # the plan's falsifiable checkpoint: the old remote flow provably
        # could not catch this (it triggered repos pinned @main).
        wf_dir = self._bad_tree(
            tmp_path,
            "jobs:\n  x:\n    steps:\n      - uses: actions/checkout@wrongsha\n",
        )
        monkeypatch.setattr(update_versions, "_ROOT", tmp_path)
        monkeypatch.setattr(update_versions, "_WORKFLOWS_DIR", wf_dir)
        monkeypatch.setattr(update_versions, "_ACTIONS_DIR", tmp_path / "none")
        failures = update_versions._validate_locally()
        assert failures == ["SSOT sync: --check found drift after --apply"]

    def test_real_repo_passes_all_gates(self) -> None:
        # Full run against the actual repo — YAML parse, SSOT sync, and the
        # nested workflow pytest gates. Slowish (spawns pytest) but real:
        # no mocks, and it IS the post-apply state --auto-update relies on.
        assert update_versions._validate_locally() == []


class TestSetActionSpecInYaml:
    """Block-scoped version/sha rewrite preserves comments + other actions."""

    YAML = (
        "actions:\n"
        "  checkout:\n"
        "    version: v6.0.1\n"
        "    sha: oldsha\n"
        "  # comment before cache\n"
        "  cache:\n"
        "    version: v5.0.0\n"
        "    sha: cachesha\n"
    )

    def test_updates_target_block_only(self) -> None:
        out = update_versions._set_action_spec_in_yaml(
            self.YAML, "checkout", "v6.0.2", "newsha"
        )
        assert "    version: v6.0.2\n" in out
        assert "    sha: newsha\n" in out
        # cache untouched
        assert "    version: v5.0.0\n" in out
        assert "    sha: cachesha\n" in out
        # comment preserved
        assert "  # comment before cache\n" in out


# --- tools: mirrored CLI pins ---------------------------------------------
#
# `tools:` entries are consumed from Python / composite-action source rather
# than a `uses:` line, so each version is COPIED into one file and anchored
# there by a `# hyperi-ci:pin tools.<name>` marker.


class TestToolPinPattern:
    """The marker is what makes a pin enforceable - and rewritable safely."""

    def test_matches_yaml_default_line(self) -> None:
        text = "    # hyperi-ci:pin tools.osv-scanner\n    default: v2.4.0\n"
        match = update_versions._tool_pin_pattern("osv-scanner").search(text)
        assert match is not None
        assert match.group(2) == "v2.4.0"

    def test_matches_python_constant(self) -> None:
        # Same marker, different language: one pattern must read both shapes
        # or each file would need its own bespoke regex.
        text = '# hyperi-ci:pin tools.gitleaks\n_GITLEAKS_VERSION = "v8.30.1"\n'
        match = update_versions._tool_pin_pattern("gitleaks").search(text)
        assert match is not None
        assert match.group(2) == "v8.30.1"

    def test_no_match_without_marker(self) -> None:
        # An unmarked pin must go unmatched so it is REPORTED. Matching it
        # anyway is what a bare `default:`/`_VERSION =` regex would do, and
        # that rewrites every tool in a shared file to the same version.
        text = '_GITLEAKS_VERSION = "v8.30.1"\n'
        assert update_versions._tool_pin_pattern("gitleaks").search(text) is None

    def test_no_match_on_another_tools_marker(self) -> None:
        # setup-go-tools/action.yml carries three tools with identical
        # `default:` lines - a pattern must only ever answer to its own name.
        text = "    # hyperi-ci:pin tools.gosec\n    default: v2.27.1\n"
        assert update_versions._tool_pin_pattern("golangci-lint").search(text) is None

    def test_digit_inside_identifier_is_not_the_version(self) -> None:
        # Requiring a `=`/`:` before the token is what stops the rewrite
        # landing inside `_SHA256` and corrupting the identifier.
        text = '# hyperi-ci:pin tools.gitleaks\n_SHA256 = "v1.2.3"\n'
        match = update_versions._tool_pin_pattern("gitleaks").search(text)
        assert match is not None
        assert match.group(2) == "v1.2.3"

    def test_trailing_comment_survives_a_rewrite(self) -> None:
        # The token must end at the version: swallowing the trailing comment
        # would silently delete the note explaining the pin.
        text = "# hyperi-ci:pin tools.gitleaks\n    default: v1.2.3  # note\n"
        out = update_versions._tool_pin_pattern("gitleaks").sub(
            update_versions._pin_replacement("v9.9.9"), text
        )
        assert out == "# hyperi-ci:pin tools.gitleaks\n    default: v9.9.9  # note\n"

    def test_adjacent_markers_resolve_independently(self) -> None:
        # Two tools, one file, same line shape - the case an explicit marker
        # exists for. Rewriting gosec must leave govulncheck alone.
        text = (
            "    # hyperi-ci:pin tools.gosec\n"
            "    default: v2.27.1\n"
            "    # hyperi-ci:pin tools.govulncheck\n"
            "    default: v1.1.4\n"
        )
        gosec = update_versions._tool_pin_pattern("gosec").search(text)
        govulncheck = update_versions._tool_pin_pattern("govulncheck").search(text)
        assert gosec is not None and gosec.group(2) == "v2.27.1"
        assert govulncheck is not None and govulncheck.group(2) == "v1.1.4"

        out = update_versions._tool_pin_pattern("gosec").sub(
            update_versions._pin_replacement("v2.28.0"), text
        )
        assert "    default: v2.28.0\n" in out
        assert "    default: v1.1.4\n" in out


class TestPinReplacement:
    """A re.sub REPLACEMENT, not a pattern - different escaping rules."""

    def test_keeps_the_prefix_group(self) -> None:
        assert update_versions._pin_replacement("v2.4.0") == r"\g<1>v2.4.0"

    def test_backslash_in_version_does_not_corrupt_the_sub(self) -> None:
        # Only `\` is special in a replacement. Left raw, `\2` would expand to
        # group 2 (the OLD version) instead of landing literally; re.escape
        # would be the opposite error and emit a literal `v2\.4\.0`.
        text = "# hyperi-ci:pin tools.gitleaks\n    default: v1.0.0\n"
        out = update_versions._tool_pin_pattern("gitleaks").sub(
            update_versions._pin_replacement(r"v1\2"), text
        )
        assert "    default: v1\\2\n" in out
        assert "v1.0.0" not in out


def _pin_versions(version: str = "v8.30.1", pin: str = "pin.py") -> dict:
    return {"tools": {"gitleaks": {"version": version, "pin": pin}}}


_PIN_BODY = 'header\n# hyperi-ci:pin tools.gitleaks\n_VERSION = "v8.30.1"\n'
_DRIFTED_BODY = 'header\n# hyperi-ci:pin tools.gitleaks\n_VERSION = "v8.0.0"\n'


def _pin_tree(tmp_path: Path, monkeypatch, body: str | None = _PIN_BODY) -> Path:
    """Isolated repo: an empty workflow dir plus one tool-pin file."""
    (tmp_path / "workflows").mkdir()
    pin = tmp_path / "pin.py"
    if body is not None:
        pin.write_text(body, encoding="utf-8")
    monkeypatch.setattr(update_versions, "_ROOT", tmp_path)
    monkeypatch.setattr(update_versions, "_WORKFLOWS_DIR", tmp_path / "workflows")
    monkeypatch.setattr(update_versions, "_ACTIONS_DIR", tmp_path / "none")
    return pin


class TestToolPins:
    """Every malformed entry must yield a REASON, never be warned past.

    Warn-and-continue dropped the tool out of every downstream check, so a
    broken entry left the gate green while the pin stopped being enforced.

    The reason is a string, not a count, and each one carries the _UNFIXABLE
    tag: --check has to tell "run --apply" (fixable version drift) apart from
    "fix this by hand" (nothing to anchor a rewrite to). A bare count cannot,
    and the first attempt at that advice matched substrings nothing emitted -
    so the branch was dead and every malformed entry got told to run --apply.
    """

    def test_good_entry_resolves(self, tmp_path: Path, monkeypatch) -> None:
        _pin_tree(tmp_path, monkeypatch)
        pins, problems = update_versions._tool_pins(_pin_versions())
        assert problems == []
        assert len(pins) == 1
        path, _pattern, version, name = pins[0]
        assert path == tmp_path / "pin.py"
        assert (version, name) == ("v8.30.1", "gitleaks")

    def test_missing_version_is_unenforceable(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _pin_tree(tmp_path, monkeypatch)
        pins, problems = update_versions._tool_pins(
            {"tools": {"gitleaks": {"pin": "pin.py"}}}
        )
        assert pins == []
        assert len(problems) == 1
        assert update_versions._UNFIXABLE in problems[0]

    def test_missing_pin_key_is_unenforceable(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _pin_tree(tmp_path, monkeypatch)
        pins, problems = update_versions._tool_pins(
            {"tools": {"gitleaks": {"version": "v8.30.1"}}}
        )
        assert pins == []
        assert len(problems) == 1
        assert update_versions._UNFIXABLE in problems[0]

    def test_nonexistent_pin_file_is_unenforceable(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # Renaming a pin file without updating `pin:` is the live failure:
        # nothing holds the version, and nothing says so.
        _pin_tree(tmp_path, monkeypatch)
        pins, problems = update_versions._tool_pins(_pin_versions(pin="gone.py"))
        assert pins == []
        assert len(problems) == 1
        # Name the offending path: "1 entry malformed" sends nobody anywhere.
        assert "gone.py" in problems[0]
        assert update_versions._UNFIXABLE in problems[0]

    def test_non_mapping_entry_is_unenforceable(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _pin_tree(tmp_path, monkeypatch)
        pins, problems = update_versions._tool_pins({"tools": {"gitleaks": "v8.30.1"}})
        assert pins == []
        assert len(problems) == 1
        assert update_versions._UNFIXABLE in problems[0]

    def test_malformed_entry_does_not_hide_the_good_ones(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # One bad entry must not abort the scan - the rest still get enforced.
        _pin_tree(tmp_path, monkeypatch)
        pins, problems = update_versions._tool_pins(
            {
                "tools": {
                    "broken": {"version": "v1.0.0"},
                    "gitleaks": {"version": "v8.30.1", "pin": "pin.py"},
                }
            }
        )
        assert len(problems) == 1
        assert "broken" in problems[0]
        assert [p[3] for p in pins] == ["gitleaks"]


class TestToolMismatches:
    def test_in_step_pin_reports_nothing(self, tmp_path: Path, monkeypatch) -> None:
        _pin_tree(tmp_path, monkeypatch)
        assert update_versions._tool_mismatches(_pin_versions()) == []

    def test_drift_reports_token_and_the_pin_line(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _pin_tree(tmp_path, monkeypatch, _DRIFTED_BODY)
        problems = update_versions._tool_mismatches(_pin_versions())
        assert len(problems) == 1
        # The match SPANS the marker, so reporting match.start() would point
        # the reader at line 2 (the marker) rather than line 3 (the pin).
        assert "pin.py:3:" in problems[0]
        assert "v8.0.0" in problems[0]
        assert "v8.30.1" in problems[0]
        # The version TOKEN, not the whole match: echoing group(0) prints the
        # marker line too, i.e. a multi-line mess.
        assert "hyperi-ci:pin" not in problems[0]

    def test_missing_marker_is_reported_not_ignored(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # Rewriting zero lines and staying green is how a pin drifts for nine
        # months unnoticed.
        _pin_tree(tmp_path, monkeypatch, '_VERSION = "v8.30.1"\n')
        problems = update_versions._tool_mismatches(_pin_versions())
        assert len(problems) == 1
        assert "no `# hyperi-ci:pin tools.gitleaks` marker found" in problems[0]

    def test_malformed_entry_surfaces(self, tmp_path: Path, monkeypatch) -> None:
        # The reason travels through to --check, naming the tool and flagging
        # that --apply cannot repair it.
        _pin_tree(tmp_path, monkeypatch)
        problems = update_versions._tool_mismatches(
            {"tools": {"gitleaks": {"version": "v8.30.1"}}}
        )
        assert len(problems) == 1
        assert "gitleaks" in problems[0]
        assert update_versions._UNFIXABLE in problems[0]


class TestToolReleases:
    """rustsec is a MONOREPO - every crate tags as `<crate>/vX.Y.Z`."""

    RELEASES = [
        {"tag_name": "cargo-audit/v0.22.3", "published_at": "2026-05-01T00:00:00Z"},
        {"tag_name": "platforms/v4.0.0", "published_at": "2026-05-02T00:00:00Z"},
        {"tag_name": "rustsec/v0.30.0", "published_at": "2026-05-03T00:00:00Z"},
    ]

    def test_tag_prefix_selects_the_crate_and_strips_the_prefix(self) -> None:
        # Unstripped, `cargo-audit/v0.22.3` is not bare semver, so
        # _parse_semver rejects it and the tool reads as permanently current.
        out = update_versions._tool_releases(
            {"tag_prefix": "cargo-audit/"}, self.RELEASES
        )
        assert [r["tag_name"] for r in out] == ["v0.22.3"]

    def test_other_release_keys_survive_the_strip(self) -> None:
        # published_at must ride along or the cooldown gate has nothing to
        # judge and every release silently drops out.
        out = update_versions._tool_releases(
            {"tag_prefix": "cargo-audit/"}, self.RELEASES
        )
        assert out[0]["published_at"] == "2026-05-01T00:00:00Z"

    def test_no_tag_prefix_passes_through_untouched(self) -> None:
        releases = [{"tag_name": "v8.30.1", "published_at": "2026-05-01T00:00:00Z"}]
        assert update_versions._tool_releases({}, releases) == releases


class TestLatestToolRelease:
    """(tag, status) - the status is load-bearing, not decoration.

    Collapsing these into a bare None made --latest render an API failure as
    "(up to date)": a rate-limited `gh` reported every tool green.
    """

    NOW = datetime(2026, 5, 28, tzinfo=UTC)

    def _gh(self, monkeypatch, payload) -> None:
        # No network: _gh_json is the only door out, so stub it there.
        monkeypatch.setattr(update_versions, "_gh_json", lambda _path: payload)

    def test_ok_for_a_newer_aged_release(self, monkeypatch) -> None:
        self._gh(monkeypatch, [_rel("v8.30.1", 20), _rel("v8.0.0", 90)])
        assert update_versions._latest_tool_release(
            {"repo": "gitleaks/gitleaks", "version": "v8.0.0"}, self.NOW
        ) == ("v8.30.1", "ok")

    def test_current_when_already_on_the_best_candidate(self, monkeypatch) -> None:
        self._gh(monkeypatch, [_rel("v8.30.1", 20), _rel("v8.0.0", 90)])
        assert update_versions._latest_tool_release(
            {"repo": "gitleaks/gitleaks", "version": "v8.30.1"}, self.NOW
        ) == (None, "current")

    def test_no_candidate_when_everything_is_inside_the_cooldown(
        self, monkeypatch
    ) -> None:
        self._gh(monkeypatch, [_rel("v8.31.0", 1)])
        assert update_versions._latest_tool_release(
            {"repo": "gitleaks/gitleaks", "version": "v8.30.1"}, self.NOW
        ) == (None, "no-candidate")

    def test_lookup_failed_on_api_error(self, monkeypatch) -> None:
        # An unreachable API is NOT a current tool. Rendering it as "current"
        # is a silent skip wearing a green hat.
        self._gh(monkeypatch, None)
        assert update_versions._latest_tool_release(
            {"repo": "gitleaks/gitleaks", "version": "v8.30.1"}, self.NOW
        ) == (None, "lookup-failed")

    def test_lookup_failed_without_repo(self, monkeypatch) -> None:
        self._gh(monkeypatch, [_rel("v8.30.1", 20)])
        assert update_versions._latest_tool_release(
            {"version": "v8.30.1"}, self.NOW
        ) == (
            None,
            "lookup-failed",
        )

    def test_cooldown_excluded_newer_pin_is_current_not_a_downgrade(
        self, monkeypatch
    ) -> None:
        # The live gosec case: v2.28.0 is pinned but still inside the cooldown,
        # so the best AGED candidate is the older v2.27.1. Calling that an
        # "update" would have --auto-update roll the tool backwards.
        self._gh(monkeypatch, [_rel("v2.28.0", 2), _rel("v2.27.1", 30)])
        assert update_versions._latest_tool_release(
            {"repo": "securego/gosec", "version": "v2.28.0"}, self.NOW
        ) == (None, "current")

    def test_v_less_ssot_value_does_not_gain_a_v(self, monkeypatch) -> None:
        # cargo-deny: the download URL is built from this string verbatim, so
        # a re-added `v` 404s the install.
        self._gh(monkeypatch, [_rel("v0.20.3", 20)])
        assert update_versions._latest_tool_release(
            {"repo": "EmbarkStudios/cargo-deny", "version": "0.20.2"}, self.NOW
        ) == ("0.20.3", "ok")

    def test_v_less_upstream_tag_stays_v_less(self, monkeypatch) -> None:
        self._gh(monkeypatch, [_rel("0.20.3", 20)])
        assert update_versions._latest_tool_release(
            {"repo": "EmbarkStudios/cargo-deny", "version": "0.20.2"}, self.NOW
        ) == ("0.20.3", "ok")

    def test_monorepo_tag_prefix_resolves(self, monkeypatch) -> None:
        self._gh(
            monkeypatch, [_rel("cargo-audit/v0.22.3", 20), _rel("platforms/v4.0.0", 20)]
        )
        assert update_versions._latest_tool_release(
            {
                "repo": "rustsec/rustsec",
                "version": "v0.22.2",
                "tag_prefix": "cargo-audit/",
            },
            self.NOW,
        ) == ("v0.22.3", "ok")

    def test_monorepo_without_tag_prefix_finds_nothing(self, monkeypatch) -> None:
        # Proves tag_prefix is load-bearing: the prefixed tags are not bare
        # semver, so an unfiltered scan reports "nothing aged past cooldown"
        # forever while cargo-audit quietly goes unmanaged.
        self._gh(
            monkeypatch, [_rel("cargo-audit/v0.22.3", 20), _rel("platforms/v4.0.0", 20)]
        )
        assert update_versions._latest_tool_release(
            {"repo": "rustsec/rustsec", "version": "v0.22.2"}, self.NOW
        ) == (None, "no-candidate")


class TestSetToolVersionInYaml:
    """Block-scoped AND `tools:`-anchored rewrite of one tool's version."""

    YAML = (
        "actions:\n"
        "  gitleaks:\n"
        "    version: v1.0.0\n"
        "\n"
        "tools:\n"
        "  # do not hand-edit\n"
        "  gitleaks:\n"
        "    version: v8.30.1\n"
        "    repo: gitleaks/gitleaks\n"
        "  osv-scanner:\n"
        "    version: v2.4.0\n"
        "\n"
        "watch:\n"
        "  gitleaks:\n"
        "    version: v0.0.1\n"
        "\n"
        "runtimes:\n"
        '  python: "3.12"\n'
        "\n"
        "semantic_release:\n"
        '  core: "25"\n'
    )

    def test_rewrites_only_the_named_tool(self) -> None:
        out = update_versions._set_tool_version_in_yaml(
            self.YAML, "gitleaks", "v8.31.0"
        )
        assert "  gitleaks:\n    version: v8.31.0\n" in out
        assert "  osv-scanner:\n    version: v2.4.0\n" in out

    def test_a_name_shared_with_actions_only_touches_tools(self) -> None:
        # An action and a tool can share a short name; anchoring to `tools:`
        # is the only thing keeping the rewrite out of the actions: block.
        out = update_versions._set_tool_version_in_yaml(
            self.YAML, "gitleaks", "v8.31.0"
        )
        assert "actions:\n  gitleaks:\n    version: v1.0.0\n" in out

    def test_does_not_bleed_into_later_sections(self) -> None:
        # The tools: block must end at the next top-level key, or a rewrite
        # walks on into watch:/runtimes:/semantic_release:.
        out = update_versions._set_tool_version_in_yaml(
            self.YAML, "gitleaks", "v8.31.0"
        )
        assert "watch:\n  gitleaks:\n    version: v0.0.1\n" in out
        assert '  python: "3.12"\n' in out
        assert '  core: "25"\n' in out

    def test_comments_survive(self) -> None:
        # yaml.safe_dump would strip every comment in the file - the whole
        # reason this edits lines directly.
        out = update_versions._set_tool_version_in_yaml(
            self.YAML, "gitleaks", "v8.31.0"
        )
        assert "  # do not hand-edit\n" in out

    def test_unknown_tool_is_a_no_op(self) -> None:
        assert (
            update_versions._set_tool_version_in_yaml(self.YAML, "nope", "v9")
            == self.YAML
        )


class TestReportWatchlist:
    """`watch:` prints at the moment someone is already updating deps."""

    WATCH = {
        "watch": {
            "uv-audit": {
                "what": "replace pip-audit with `uv audit`",
                "blocked_by": "experimental as of uv 0.11.29 - gated\nbehind --preview-features",
                "gate": "`uv audit` runs with no experimental warning",
                "issue": 68,
            }
        }
    }

    def test_prints_what_reason_gate_and_issue(self, capsys) -> None:
        update_versions._report_watchlist(self.WATCH)
        out = capsys.readouterr().out
        assert "uv-audit (#68)" in out
        assert "replace pip-audit with `uv audit`" in out
        # blocked_by carries the REASON. Declaring it and never printing it is
        # how a watchlist decays into nags nobody can evaluate.
        assert "blocked by: experimental as of uv 0.11.29 - gated behind" in out
        # gate is the checkable exit condition - without it the entry is a vibe.
        assert "ready when: `uv audit` runs with no experimental warning" in out

    def test_silent_without_a_watch_section(self, capsys) -> None:
        update_versions._report_watchlist({"tools": {}})
        assert capsys.readouterr().out == ""


class TestRewriteReturnCodes:
    """--check and --fix MUST agree.

    --check is not wired into CI anywhere, so the pre-commit hook (--fix) is
    the only automated gate. They previously disagreed on a missing marker
    (check=1, fix=0), which made every automated gate green for a pin nobody
    was holding.
    """

    def test_missing_marker_fails_check_apply_and_fix(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _pin_tree(tmp_path, monkeypatch, '_VERSION = "v8.30.1"\n')
        assert update_versions._check(_pin_versions()) == 1
        assert update_versions._apply(_pin_versions()) == 1
        assert update_versions._fix(_pin_versions()) == 1

    def test_bad_pin_path_fails_check_apply_and_fix(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _pin_tree(tmp_path, monkeypatch)
        versions = _pin_versions(pin="gone.py")
        assert update_versions._check(versions) == 1
        assert update_versions._apply(versions) == 1
        assert update_versions._fix(versions) == 1

    def test_in_step_pin_is_green_everywhere(self, tmp_path: Path, monkeypatch) -> None:
        _pin_tree(tmp_path, monkeypatch)
        assert update_versions._check(_pin_versions()) == 0
        assert update_versions._apply(_pin_versions()) == 0
        assert update_versions._fix(_pin_versions()) == 0

    def test_rewrite_counts_a_missing_marker_as_unenforceable(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # Nothing to anchor a rewrite to, so 0 changes - and that 0 must not
        # read as success.
        _pin_tree(tmp_path, monkeypatch, '_VERSION = "v8.30.1"\n')
        assert update_versions._rewrite_to_ssot(_pin_versions(), verb="Updated") == (
            0,
            1,
        )

    def test_rewrite_pulls_a_drifted_pin_back_to_ssot(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # --fix (the hook) shares this one path with --apply. It once carried
        # its own copy of the loop and simply lacked the tool-pin half.
        pin = _pin_tree(tmp_path, monkeypatch, _DRIFTED_BODY)
        assert update_versions._rewrite_to_ssot(_pin_versions(), verb="Updated") == (
            1,
            0,
        )
        assert pin.read_text(encoding="utf-8") == _PIN_BODY

    def test_apply_exits_zero_on_drift_but_fix_exits_one(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # --fix is the pre-commit hook: a rewrite must exit non-zero so the
        # framework re-stages the file. --apply is the "make it so" verb.
        pin = _pin_tree(tmp_path, monkeypatch, _DRIFTED_BODY)
        assert update_versions._apply(_pin_versions()) == 0
        assert pin.read_text(encoding="utf-8") == _PIN_BODY

        pin.write_text(_DRIFTED_BODY, encoding="utf-8")
        assert update_versions._fix(_pin_versions()) == 1
        assert pin.read_text(encoding="utf-8") == _PIN_BODY
