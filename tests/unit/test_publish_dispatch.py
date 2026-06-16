# Project:   HyperI CI
# File:      tests/unit/test_publish_dispatch.py
# Purpose:   Tests for release/retry-from-HEAD dispatch + idempotent retry (#35)
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Release/retry dispatch (issue #35).

`hyperi-ci publish` (no tag) dispatches a from-head run — the CI creates the
tag and publishes, so there's no artificial `fix:` commit. `publish <tag>`
re-dispatches an existing tag idempotently (a partial publish can be retried
even when a GH Release already exists). The CLI only triggers; the runner does
the tagging + publishing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import typer

import hyperi_ci.publish as publish_pkg
from hyperi_ci import push
from hyperi_ci.cli import _publish_impl
from hyperi_ci.publish import dispatch as d


def _ok() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


class TestDispatchFromHead:
    def _record(self, monkeypatch) -> list[list[str]]:
        calls: list[list[str]] = []
        monkeypatch.setattr(d, "_detect_workflow_file", lambda: "ci.yml")
        monkeypatch.setattr(d, "_head_in_sync_with_origin", lambda: True)
        monkeypatch.setattr(
            d.subprocess, "run", lambda cmd, **k: (calls.append(cmd), _ok())[1]
        )
        return calls

    def test_auto_dispatches_from_head(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._record(monkeypatch)
        rc = d.dispatch_from_head(bump="auto")
        assert rc == 0
        assert calls[-1] == [
            "gh",
            "workflow",
            "run",
            "ci.yml",
            "-f",
            "from-head=true",
            "-f",
            "bump=auto",
        ]

    def test_forced_bump_passed_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._record(monkeypatch)
        assert d.dispatch_from_head(bump="minor") == 0
        assert "-f" in calls[-1] and "bump=minor" in calls[-1]

    def test_invalid_bump_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._record(monkeypatch)
        assert d.dispatch_from_head(bump="major") == 1
        assert calls == []  # nothing dispatched

    def test_explicit_version_passed_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # --version X.Y.Z travels in the bump channel (issue #37 override).
        calls = self._record(monkeypatch)
        assert d.dispatch_from_head(bump="1.18.4") == 0
        assert "bump=1.18.4" in calls[-1]
        assert "from-head=true" in calls[-1]

    def test_explicit_version_v_prefix_normalised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A leading v is stripped before it travels to the CI.
        calls = self._record(monkeypatch)
        assert d.dispatch_from_head(bump="v2.0.0") == 0
        assert "bump=2.0.0" in calls[-1]

    def test_garbage_version_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._record(monkeypatch)
        assert d.dispatch_from_head(bump="1.2") == 1  # not a full X.Y.Z
        assert calls == []

    def test_dry_run_does_not_dispatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._record(monkeypatch)
        assert d.dispatch_from_head(bump="auto", dry_run=True) == 0
        assert calls == []


class TestIdempotentRetry:
    def test_existing_tag_redispatches_even_with_release(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A GH Release already existing must NOT block the retry — publish
        # handlers skip artefacts already in their registry (issue #35).
        monkeypatch.setattr(d, "_get_version_tags", lambda: ["v1.2.3"])
        monkeypatch.setattr(d, "_tag_has_release", lambda t: True)
        monkeypatch.setattr(d, "_detect_workflow_file", lambda: "ci.yml")
        calls: list[list[str]] = []
        monkeypatch.setattr(
            d.subprocess, "run", lambda cmd, **k: (calls.append(cmd), _ok())[1]
        )
        rc = d.dispatch_publish("v1.2.3")
        assert rc == 0
        assert ["gh", "workflow", "run", "ci.yml", "-f", "tag=v1.2.3"] in calls

    def test_missing_tag_still_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(d, "_get_version_tags", lambda: ["v1.2.3"])
        assert d.dispatch_publish("v9.9.9") == 1


class TestPublishCliVersionFlag:
    """`hyperi-ci publish --version X.Y.Z` routes to a from-head release at the
    exact version, mutually exclusive with a TAG and --bump (issue #37)."""

    def _capture_from_head(self, monkeypatch) -> list[str]:
        seen: list[str] = []

        def fake_from_head(*, bump, dry_run=False):
            seen.append(bump)
            return 0

        monkeypatch.setattr(publish_pkg, "dispatch_from_head", fake_from_head)
        return seen

    def test_version_routes_to_from_head_normalised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = self._capture_from_head(monkeypatch)
        with pytest.raises(typer.Exit) as exc:
            _publish_impl(
                tag=None, list_tags=False, dry_run=False, bump=None, version="v1.18.4"
            )
        assert exc.value.exit_code == 0
        assert seen == ["1.18.4"]  # leading v stripped, travels as bump

    def test_version_with_tag_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = self._capture_from_head(monkeypatch)
        with pytest.raises(typer.Exit) as exc:
            _publish_impl(
                tag="v1.0.0",
                list_tags=False,
                dry_run=False,
                bump=None,
                version="1.18.4",
            )
        assert exc.value.exit_code == 1
        assert seen == []  # never dispatched

    def test_version_with_bump_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = self._capture_from_head(monkeypatch)
        with pytest.raises(typer.Exit) as exc:
            _publish_impl(
                tag=None,
                list_tags=False,
                dry_run=False,
                bump="patch",
                version="1.18.4",
            )
        assert exc.value.exit_code == 1
        assert seen == []

    def test_garbage_version_rejected_before_dispatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = self._capture_from_head(monkeypatch)
        with pytest.raises(typer.Exit) as exc:
            _publish_impl(
                tag=None, list_tags=False, dry_run=False, bump=None, version="1.2"
            )
        assert exc.value.exit_code == 1
        assert seen == []


class TestTagHead:
    def test_dry_run_emits_version_and_tag(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(push, "_compute_next_version", lambda **k: "1.2.3")
        out = tmp_path / "gh_output"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out))
        rc = push.tag_head(bump="patch", dry_run=True)
        assert rc == 0
        text = out.read_text()
        assert "version=1.2.3" in text
        assert "tag=v1.2.3" in text

    def test_invalid_bump_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert push.tag_head(bump="major", dry_run=True) == 1

    def test_explicit_version_used_verbatim(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # An explicit X.Y.Z (the --version override) is tagged as-is, with no
        # last-tag math — proven by making _compute_next_version blow up.
        def _boom(**k):
            raise AssertionError("must not compute a bump for an explicit version")

        monkeypatch.setattr(push, "_compute_next_version", _boom)
        out = tmp_path / "gh_output"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out))
        rc = push.tag_head(bump="1.18.4", dry_run=True)
        assert rc == 0
        text = out.read_text()
        assert "version=1.18.4" in text
        assert "tag=v1.18.4" in text

    def test_no_version_available_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(push, "_compute_next_version", lambda **k: None)
        assert push.tag_head(bump="patch", dry_run=True) == 1

    def test_creates_ref_via_gh_api(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Tag created via the GitHub API (works with persist-credentials:false).
        monkeypatch.setattr(push, "_compute_next_version", lambda **k: "1.2.3")
        monkeypatch.setenv("GITHUB_REPOSITORY", "hyperi-io/x")
        monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "o"))
        calls: list[list[str]] = []

        def fake_run_cmd(cmd, **k):
            calls.append(cmd)
            if cmd[:2] == ["git", "rev-parse"]:
                return subprocess.CompletedProcess(cmd, 0, "abc1234\n", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(push, "run_cmd", fake_run_cmd)
        assert push.tag_head(bump="patch") == 0
        gh = next(c for c in calls if c[:2] == ["gh", "api"])
        assert "ref=refs/tags/v1.2.3" in gh
        assert "sha=abc1234" in gh

    def test_explicit_version_refuses_tag_off_head(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # An explicit --version that names a tag pointing somewhere other than
        # HEAD (orphan / typo) must be refused, not published over (issue #37).
        monkeypatch.setenv("GITHUB_REPOSITORY", "hyperi-io/x")
        monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "o"))
        gh_calls: list[list[str]] = []

        def fake_run_cmd(cmd, **k):
            if cmd[:2] == ["git", "rev-parse"] and "HEAD" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "headsha0\n", "")
            if cmd[:2] == ["git", "rev-parse"]:  # the tag peel — points elsewhere
                return subprocess.CompletedProcess(cmd, 0, "orphan99\n", "")
            gh_calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(push, "run_cmd", fake_run_cmd)
        assert push.tag_head(bump="1.18.3") == 1
        assert not any(c[:2] == ["gh", "api"] for c in gh_calls)  # never created

    def test_explicit_version_at_head_is_idempotent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The same explicit version already at HEAD is a no-op success.
        monkeypatch.setenv("GITHUB_REPOSITORY", "hyperi-io/x")
        out = tmp_path / "o"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out))

        def fake_run_cmd(cmd, **k):
            return subprocess.CompletedProcess(cmd, 0, "samesha0\n", "")

        monkeypatch.setattr(push, "run_cmd", fake_run_cmd)
        assert push.tag_head(bump="1.18.4") == 0
        assert "version=1.18.4" in out.read_text()

    def test_existing_ref_is_idempotent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A 422 "Reference already exists" must not fail the retry.
        monkeypatch.setattr(push, "_compute_next_version", lambda **k: "1.2.3")
        monkeypatch.setenv("GITHUB_REPOSITORY", "hyperi-io/x")
        monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "o"))

        def fake_run_cmd(cmd, **k):
            if cmd[:2] == ["git", "rev-parse"]:
                return subprocess.CompletedProcess(cmd, 0, "abc1234\n", "")
            return subprocess.CompletedProcess(
                cmd, 1, "", "HTTP 422: Reference already exists"
            )

        monkeypatch.setattr(push, "run_cmd", fake_run_cmd)
        assert push.tag_head(bump="patch") == 0
