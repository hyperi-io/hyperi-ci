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
