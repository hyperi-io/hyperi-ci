# Project:   HyperI CI
# File:      tests/unit/test_push.py
# Purpose:   Tests for push command (--publish trailer-amend flow)
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hyperi_ci.push import (
    _check_dirty_tree,
    _check_not_ci_commit,
    _has_publish_trailer,
    push,
)


class TestPushFlagValidation:
    """Test mutually exclusive flags."""

    def test_publish_and_no_ci_mutually_exclusive(self) -> None:
        rc = push(publish=True, no_ci=True)
        assert rc == 1

    def test_default_push_accepted(self) -> None:
        with patch("hyperi_ci.push._default_push", return_value=0) as mock:
            rc = push()
            assert rc == 0
            mock.assert_called_once()

    def test_publish_flag_routes_to_publish_push(self) -> None:
        with patch("hyperi_ci.push._publish_push", return_value=0) as mock:
            rc = push(publish=True)
            assert rc == 0
            mock.assert_called_once()

    def test_no_ci_flag_routes_to_skip_ci_push(self) -> None:
        with patch("hyperi_ci.push._skip_ci_push", return_value=0) as mock:
            rc = push(no_ci=True)
            assert rc == 0
            mock.assert_called_once()


class TestCheckDirtyTree:
    """Test dirty tree detection."""

    def test_clean_tree_returns_zero(self) -> None:
        result = MagicMock(stdout="", returncode=0)
        with patch("hyperi_ci.push.run_cmd", return_value=result):
            assert _check_dirty_tree(cwd=None) == 0

    def test_dirty_tree_returns_one(self) -> None:
        result = MagicMock(stdout=" M src/file.py\n", returncode=0)
        with patch("hyperi_ci.push.run_cmd", return_value=result):
            assert _check_dirty_tree(cwd=None) == 1


class TestCheckNotCICommit:
    """Test CI bot commit detection."""

    def test_normal_commit_allowed(self) -> None:
        with patch(
            "hyperi_ci.push._get_last_commit_message",
            return_value="fix: update config parser",
        ):
            assert _check_not_ci_commit(cwd=None) == 0

    def test_version_commit_blocked(self) -> None:
        with patch(
            "hyperi_ci.push._get_last_commit_message",
            return_value="chore: version 1.5.0 [skip ci]",
        ):
            assert _check_not_ci_commit(cwd=None) == 1

    def test_release_commit_blocked(self) -> None:
        with patch(
            "hyperi_ci.push._get_last_commit_message",
            return_value="chore(release): 2.0.0 [skip ci]",
        ):
            assert _check_not_ci_commit(cwd=None) == 1

    def test_none_message_allowed(self) -> None:
        with patch(
            "hyperi_ci.push._get_last_commit_message",
            return_value=None,
        ):
            assert _check_not_ci_commit(cwd=None) == 0


class TestHasPublishTrailer:
    """Test ``Publish: true`` trailer detection in commit messages."""

    def test_no_trailer(self) -> None:
        msg = "fix: update config parser\n\nLong body explaining why.\n"
        assert _has_publish_trailer(msg) is False

    def test_trailer_present(self) -> None:
        msg = "fix: update config parser\n\nPublish: true\n"
        assert _has_publish_trailer(msg) is True

    def test_trailer_case_insensitive_key(self) -> None:
        msg = "fix: thing\n\npublish: true\n"
        assert _has_publish_trailer(msg) is True

    def test_trailer_case_insensitive_value(self) -> None:
        msg = "fix: thing\n\nPublish: TRUE\n"
        assert _has_publish_trailer(msg) is True

    def test_trailer_with_whitespace(self) -> None:
        msg = "fix: thing\n\n  Publish:   true  \n"
        assert _has_publish_trailer(msg) is True

    def test_trailer_false_does_not_match(self) -> None:
        msg = "fix: thing\n\nPublish: false\n"
        assert _has_publish_trailer(msg) is False

    def test_trailer_other_key_does_not_match(self) -> None:
        msg = "fix: thing\n\nPublished-by: someone\n"
        assert _has_publish_trailer(msg) is False


class TestDefaultPush:
    """Test default push flow."""

    def test_dirty_tree_aborts(self) -> None:
        with patch("hyperi_ci.push._check_dirty_tree", return_value=1):
            from hyperi_ci.push import _default_push

            rc = _default_push(dry_run=False, force=False, cwd=None)
            assert rc == 1

    def test_check_failure_aborts(self) -> None:
        with (
            patch("hyperi_ci.push._check_dirty_tree", return_value=0),
            patch("hyperi_ci.push._run_check", return_value=1),
        ):
            from hyperi_ci.push import _default_push

            rc = _default_push(dry_run=False, force=False, cwd=None)
            assert rc == 1

    def test_force_skips_check(self) -> None:
        with (
            patch("hyperi_ci.push._check_dirty_tree", return_value=0),
            patch("hyperi_ci.push._run_check") as mock_check,
            patch("hyperi_ci.push._rebase_and_push", return_value=0),
        ):
            from hyperi_ci.push import _default_push

            rc = _default_push(dry_run=False, force=True, cwd=None)
            assert rc == 0
            mock_check.assert_not_called()

    def test_dry_run_no_side_effects(self) -> None:
        with (
            patch("hyperi_ci.push._check_dirty_tree", return_value=0),
            patch("hyperi_ci.push._run_check", return_value=0),
            patch("hyperi_ci.push._bump_gate", return_value=0),
            patch("hyperi_ci.push._rebase_and_push") as mock_push,
        ):
            from hyperi_ci.push import _default_push

            rc = _default_push(dry_run=True, force=False, cwd=None)
            assert rc == 0
            mock_push.assert_not_called()


class TestPublishPush:
    """Test --publish flow (trailer-amend, single CI run)."""

    def test_not_on_main_aborts(self) -> None:
        with (
            patch("hyperi_ci.push.require_gh", return_value=True),
            patch("hyperi_ci.push.get_current_branch", return_value="feat/thing"),
        ):
            from hyperi_ci.push import _publish_push

            rc = _publish_push(dry_run=False, force=False, bump=None, cwd=None)
            assert rc == 1

    def test_no_gh_cli_aborts(self) -> None:
        with patch("hyperi_ci.push.require_gh", return_value=False):
            from hyperi_ci.push import _publish_push

            rc = _publish_push(dry_run=False, force=False, bump=None, cwd=None)
            assert rc == 1

    def test_dirty_tree_aborts(self) -> None:
        with (
            patch("hyperi_ci.push.require_gh", return_value=True),
            patch("hyperi_ci.push.get_current_branch", return_value="main"),
            patch("hyperi_ci.push._check_dirty_tree", return_value=1),
        ):
            from hyperi_ci.push import _publish_push

            rc = _publish_push(dry_run=False, force=False, bump=None, cwd=None)
            assert rc == 1

    def test_check_failure_aborts(self) -> None:
        with (
            patch("hyperi_ci.push.require_gh", return_value=True),
            patch("hyperi_ci.push.get_current_branch", return_value="main"),
            patch("hyperi_ci.push._check_dirty_tree", return_value=0),
            patch("hyperi_ci.push._run_check", return_value=1),
        ):
            from hyperi_ci.push import _publish_push

            rc = _publish_push(dry_run=False, force=False, bump=None, cwd=None)
            assert rc == 1

    def test_amends_when_no_trailer_then_pushes(self) -> None:
        # HEAD has no Publish: true trailer → amend → rebase + push.
        with (
            patch("hyperi_ci.push.require_gh", return_value=True),
            patch("hyperi_ci.push.get_current_branch", return_value="main"),
            patch("hyperi_ci.push._check_dirty_tree", return_value=0),
            patch("hyperi_ci.push._run_check", return_value=0),
            patch("hyperi_ci.push._bump_gate", return_value=0),
            patch(
                "hyperi_ci.push._get_last_commit_message",
                return_value="fix: thing\n",
            ),
            patch(
                "hyperi_ci.push._amend_publish_trailer", return_value=0
            ) as mock_amend,
            patch("hyperi_ci.push._pull_rebase", return_value=0),
            patch("hyperi_ci.push._push_with_env", return_value=0) as mock_push,
        ):
            from hyperi_ci.push import _publish_push

            rc = _publish_push(dry_run=False, force=False, bump=None, cwd=None)
            assert rc == 0
            mock_amend.assert_called_once()
            mock_push.assert_called_once()

    def test_skips_amend_when_trailer_already_present(self) -> None:
        with (
            patch("hyperi_ci.push.require_gh", return_value=True),
            patch("hyperi_ci.push.get_current_branch", return_value="main"),
            patch("hyperi_ci.push._check_dirty_tree", return_value=0),
            patch("hyperi_ci.push._run_check", return_value=0),
            patch("hyperi_ci.push._bump_gate", return_value=0),
            patch(
                "hyperi_ci.push._get_last_commit_message",
                return_value="fix: thing\n\nPublish: true\n",
            ),
            patch(
                "hyperi_ci.push._amend_publish_trailer", return_value=0
            ) as mock_amend,
            patch("hyperi_ci.push._pull_rebase", return_value=0),
            patch("hyperi_ci.push._push_with_env", return_value=0) as mock_push,
        ):
            from hyperi_ci.push import _publish_push

            rc = _publish_push(dry_run=False, force=False, bump=None, cwd=None)
            assert rc == 0
            mock_amend.assert_not_called()
            mock_push.assert_called_once()

    def test_dry_run_no_amend_no_push(self) -> None:
        with (
            patch("hyperi_ci.push.require_gh", return_value=True),
            patch("hyperi_ci.push.get_current_branch", return_value="main"),
            patch("hyperi_ci.push._check_dirty_tree", return_value=0),
            patch("hyperi_ci.push._run_check", return_value=0),
            patch("hyperi_ci.push._bump_gate", return_value=0),
            patch(
                "hyperi_ci.push._get_last_commit_message",
                return_value="fix: thing\n",
            ),
            patch(
                "hyperi_ci.push._amend_publish_trailer", return_value=0
            ) as mock_amend,
            patch("hyperi_ci.push._pull_rebase", return_value=0),
            patch("hyperi_ci.push._push_with_env", return_value=0) as mock_push,
        ):
            from hyperi_ci.push import _publish_push

            rc = _publish_push(dry_run=True, force=False, bump=None, cwd=None)
            assert rc == 0
            mock_amend.assert_not_called()
            mock_push.assert_not_called()


class TestForcedBumpPush:
    """Test --bump-patch / --bump-minor flow.

    When bump is set, _publish_push computes the next version from the
    latest tag, writes it to VERSION, and commits with a conventional
    fix:/feat: marker message + Publish: true trailer. The VERSION
    write is essential — it makes the commit non-empty so consumer
    `paths-ignore` filters don't skip the CI run.
    """

    def test_bump_patch_creates_marker_commit(self) -> None:
        with (
            patch("hyperi_ci.push.require_gh", return_value=True),
            patch("hyperi_ci.push.get_current_branch", return_value="main"),
            patch("hyperi_ci.push._check_dirty_tree", return_value=0),
            patch("hyperi_ci.push._run_check", return_value=0),
            patch("hyperi_ci.push._bump_gate", return_value=0),
            patch("hyperi_ci.push._compute_next_version", return_value="1.5.5"),
            patch(
                "hyperi_ci.push._write_version_and_commit", return_value=0
            ) as mock_marker,
            patch("hyperi_ci.push._pull_rebase", return_value=0),
            patch("hyperi_ci.push._push_with_env", return_value=0) as mock_push,
        ):
            from hyperi_ci.push import _publish_push

            rc = _publish_push(dry_run=False, force=False, bump="patch", cwd=None)
            assert rc == 0
            mock_marker.assert_called_once()
            kwargs = mock_marker.call_args.kwargs
            assert kwargs["next_version"] == "1.5.5"
            msg = kwargs["message"]
            assert msg.startswith("fix(release): force patch bump v1.5.5\n")
            assert "Publish: true" in msg
            mock_push.assert_called_once()

    def test_bump_minor_creates_feat_marker(self) -> None:
        with (
            patch("hyperi_ci.push.require_gh", return_value=True),
            patch("hyperi_ci.push.get_current_branch", return_value="main"),
            patch("hyperi_ci.push._check_dirty_tree", return_value=0),
            patch("hyperi_ci.push._run_check", return_value=0),
            patch("hyperi_ci.push._bump_gate", return_value=0),
            patch("hyperi_ci.push._compute_next_version", return_value="1.6.0"),
            patch(
                "hyperi_ci.push._write_version_and_commit", return_value=0
            ) as mock_marker,
            patch("hyperi_ci.push._pull_rebase", return_value=0),
            patch("hyperi_ci.push._push_with_env", return_value=0),
        ):
            from hyperi_ci.push import _publish_push

            rc = _publish_push(dry_run=False, force=False, bump="minor", cwd=None)
            assert rc == 0
            kwargs = mock_marker.call_args.kwargs
            assert kwargs["next_version"] == "1.6.0"
            msg = kwargs["message"]
            assert msg.startswith("feat(release): force minor bump v1.6.0\n")
            assert "Publish: true" in msg

    def test_bump_dry_run_no_commit_no_push(self) -> None:
        with (
            patch("hyperi_ci.push.require_gh", return_value=True),
            patch("hyperi_ci.push.get_current_branch", return_value="main"),
            patch("hyperi_ci.push._check_dirty_tree", return_value=0),
            patch("hyperi_ci.push._run_check", return_value=0),
            patch("hyperi_ci.push._bump_gate", return_value=0),
            patch("hyperi_ci.push._compute_next_version", return_value="1.5.5"),
            patch(
                "hyperi_ci.push._write_version_and_commit", return_value=0
            ) as mock_marker,
            patch("hyperi_ci.push._pull_rebase", return_value=0),
            patch("hyperi_ci.push._push_with_env", return_value=0) as mock_push,
        ):
            from hyperi_ci.push import _publish_push

            rc = _publish_push(dry_run=True, force=False, bump="patch", cwd=None)
            assert rc == 0
            mock_marker.assert_not_called()
            mock_push.assert_not_called()

    def test_bump_aborts_when_no_existing_version(self) -> None:
        # No tags AND no VERSION → can't compute next; hard fail.
        with (
            patch("hyperi_ci.push.require_gh", return_value=True),
            patch("hyperi_ci.push.get_current_branch", return_value="main"),
            patch("hyperi_ci.push._check_dirty_tree", return_value=0),
            patch("hyperi_ci.push._run_check", return_value=0),
            patch("hyperi_ci.push._bump_gate", return_value=0),
            patch("hyperi_ci.push._compute_next_version", return_value=None),
        ):
            from hyperi_ci.push import _publish_push

            rc = _publish_push(dry_run=False, force=False, bump="patch", cwd=None)
            assert rc == 1

    def test_bump_invalid_level_rejected_at_top_level(self) -> None:
        # The push() top-level function validates bump value before
        # dispatching; an unknown level returns 1 without touching git.
        from hyperi_ci.push import push

        rc = push(bump="major")  # not in _BUMP_TO_TYPE
        assert rc == 1

    def test_bump_implies_publish(self) -> None:
        # User passes --bump-patch but not --publish; push() should
        # route to _publish_push anyway (bump implies publish).
        with patch("hyperi_ci.push._publish_push", return_value=0) as mock:
            from hyperi_ci.push import push

            rc = push(bump="patch")
            assert rc == 0
            mock.assert_called_once()
            assert mock.call_args.kwargs["bump"] == "patch"


class TestComputeNextVersion:
    """Test _compute_next_version semver math."""

    def test_patch_increment(self) -> None:
        result = MagicMock(stdout="v1.5.4\nv1.5.3\n", returncode=0)
        with patch("hyperi_ci.push.run_cmd", return_value=result):
            from hyperi_ci.push import _compute_next_version

            assert _compute_next_version(bump="patch", cwd=None) == "1.5.5"

    def test_minor_increment_resets_patch(self) -> None:
        result = MagicMock(stdout="v1.5.4\n", returncode=0)
        with patch("hyperi_ci.push.run_cmd", return_value=result):
            from hyperi_ci.push import _compute_next_version

            assert _compute_next_version(bump="minor", cwd=None) == "1.6.0"

    def test_no_tags_no_version_returns_none(self) -> None:
        result = MagicMock(stdout="", returncode=0)
        with patch("hyperi_ci.push.run_cmd", return_value=result):
            from hyperi_ci.push import _compute_next_version

            # No VERSION file in cwd either (we're in a tmp-less test)
            with patch("hyperi_ci.push.Path") as mock_path:
                mock_version = MagicMock()
                mock_version.is_file.return_value = False
                mock_path.return_value.__truediv__.return_value = mock_version
                mock_path.cwd.return_value.__truediv__.return_value = mock_version
                result_v = _compute_next_version(bump="patch", cwd=None)
                assert result_v is None

    def test_unsupported_bump_returns_none(self) -> None:
        result = MagicMock(stdout="v1.5.4\n", returncode=0)
        with patch("hyperi_ci.push.run_cmd", return_value=result):
            from hyperi_ci.push import _compute_next_version

            assert _compute_next_version(bump="major", cwd=None) is None


class TestSkipCIPush:
    """Test --no-ci flow."""

    def test_dirty_tree_aborts(self) -> None:
        with patch("hyperi_ci.push._check_dirty_tree", return_value=1):
            from hyperi_ci.push import _skip_ci_push

            rc = _skip_ci_push(dry_run=False, cwd=None)
            assert rc == 1

    def test_ci_commit_blocked(self) -> None:
        with (
            patch("hyperi_ci.push._check_dirty_tree", return_value=0),
            patch("hyperi_ci.push._check_not_ci_commit", return_value=1),
        ):
            from hyperi_ci.push import _skip_ci_push

            rc = _skip_ci_push(dry_run=False, cwd=None)
            assert rc == 1

    def test_already_skip_ci_warns_and_pushes(self) -> None:
        with (
            patch("hyperi_ci.push._check_dirty_tree", return_value=0),
            patch("hyperi_ci.push._check_not_ci_commit", return_value=0),
            patch(
                "hyperi_ci.push._get_last_commit_message",
                return_value="fix: thing [skip ci]",
            ),
            patch("hyperi_ci.push._push_with_env", return_value=0) as mock_push,
        ):
            from hyperi_ci.push import _skip_ci_push

            rc = _skip_ci_push(dry_run=False, cwd=None)
            assert rc == 0
            mock_push.assert_called_once()

    def test_dry_run_no_amend(self) -> None:
        with (
            patch("hyperi_ci.push._check_dirty_tree", return_value=0),
            patch("hyperi_ci.push._check_not_ci_commit", return_value=0),
            patch(
                "hyperi_ci.push._get_last_commit_message",
                return_value="fix: normal commit",
            ),
            patch("hyperi_ci.push.run_cmd") as mock_cmd,
            patch("hyperi_ci.push._push_with_env") as mock_push,
        ):
            from hyperi_ci.push import _skip_ci_push

            rc = _skip_ci_push(dry_run=True, cwd=None)
            assert rc == 0
            mock_cmd.assert_not_called()
            mock_push.assert_not_called()


class TestPrePushHook:
    """Test pre-push hook generation in init."""

    def test_init_creates_pre_push_hook(self, tmp_path) -> None:
        """Verify init scaffolds the pre-push hook."""
        from hyperi_ci.init import init_project

        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
        (tmp_path / ".git").mkdir()

        with patch("hyperi_ci.init.detect_language", return_value="python"):
            init_project(tmp_path)

        hook = tmp_path / ".githooks" / "pre-push"
        assert hook.exists()
        content = hook.read_text()
        assert "HYPERCI_PUSH" in content
        assert "hyperi-ci push" in content
        assert hook.stat().st_mode & 0o755

    def test_init_preserves_existing_pre_push_hook(self, tmp_path) -> None:
        """Verify init does not overwrite an existing pre-push hook."""
        from hyperi_ci.init import init_project

        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
        (tmp_path / ".git").mkdir()

        hooks_dir = tmp_path / ".githooks"
        hooks_dir.mkdir()
        hook = hooks_dir / "pre-push"
        hook.write_text("#!/bin/bash\n# custom hook\n")
        hook.chmod(0o755)

        with patch("hyperi_ci.init.detect_language", return_value="python"):
            init_project(tmp_path)

        assert hook.read_text() == "#!/bin/bash\n# custom hook\n"


class TestStageModifiedLockfiles:
    """Verify the marker commit absorbs auto-modified lockfiles."""

    def test_stages_cargo_lock_when_modified(self):
        """Cargo.lock modifications get staged into the marker."""
        from hyperi_ci.push import _stage_modified_lockfiles

        with patch("hyperi_ci.push.run_cmd") as mock_run:
            mock_run.side_effect = [
                # First call: git diff --name-only returns Cargo.lock modified
                MagicMock(stdout="Cargo.lock\n", returncode=0),
                # Second call: git add Cargo.lock succeeds
                MagicMock(stdout="", returncode=0),
            ]
            _stage_modified_lockfiles(cwd=None)

        assert mock_run.call_count == 2
        # First was git diff --name-only
        assert mock_run.call_args_list[0].args[0] == ["git", "diff", "--name-only"]
        # Second was git add Cargo.lock
        assert mock_run.call_args_list[1].args[0] == ["git", "add", "Cargo.lock"]

    def test_stages_multiple_lockfile_types(self):
        """All known lockfile basenames get staged when modified together."""
        from hyperi_ci.push import _stage_modified_lockfiles

        with patch("hyperi_ci.push.run_cmd") as mock_run:
            mock_run.side_effect = [
                MagicMock(
                    stdout="Cargo.lock\nuv.lock\npackage-lock.json\n", returncode=0
                ),
                MagicMock(stdout="", returncode=0),
                MagicMock(stdout="", returncode=0),
                MagicMock(stdout="", returncode=0),
            ]
            _stage_modified_lockfiles(cwd=None)

        assert mock_run.call_count == 4
        staged = [call.args[0][2] for call in mock_run.call_args_list[1:]]
        assert staged == ["Cargo.lock", "uv.lock", "package-lock.json"]

    def test_ignores_non_lockfile_modifications(self):
        """User-modified non-lockfile files are NOT auto-staged."""
        from hyperi_ci.push import _stage_modified_lockfiles

        with patch("hyperi_ci.push.run_cmd") as mock_run:
            mock_run.side_effect = [
                MagicMock(stdout="src/main.rs\nCargo.lock\nREADME.md\n", returncode=0),
                MagicMock(stdout="", returncode=0),
            ]
            _stage_modified_lockfiles(cwd=None)

        # Only Cargo.lock should be added; src/main.rs and README.md ignored.
        assert mock_run.call_count == 2
        assert mock_run.call_args_list[1].args[0] == ["git", "add", "Cargo.lock"]

    def test_stages_lockfile_in_subdirectory(self):
        """Lockfiles in subdirectories (basename match) are staged."""
        from hyperi_ci.push import _stage_modified_lockfiles

        with patch("hyperi_ci.push.run_cmd") as mock_run:
            mock_run.side_effect = [
                MagicMock(stdout="services/api/Cargo.lock\n", returncode=0),
                MagicMock(stdout="", returncode=0),
            ]
            _stage_modified_lockfiles(cwd=None)

        assert mock_run.call_args_list[1].args[0] == [
            "git",
            "add",
            "services/api/Cargo.lock",
        ]

    def test_no_modified_files_is_noop(self):
        """Empty diff output makes the function a no-op."""
        from hyperi_ci.push import _stage_modified_lockfiles

        with patch("hyperi_ci.push.run_cmd") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            _stage_modified_lockfiles(cwd=None)

        # Only the git diff call should fire; no git add follow-up.
        assert mock_run.call_count == 1


class TestRebaseAndPush:
    """First push of a new branch with no upstream (issue #38).

    A freshly-created local branch has no `@{u}`; the old code ran a bare
    `git pull --rebase` that aborted with "no tracking information" and
    was mislabelled as a rebase CONFLICT. The new-branch path must skip
    the rebase and push with `-u origin <branch>` instead.
    """

    def test_new_branch_no_upstream_pushes_with_set_upstream(self):
        from hyperi_ci.push import _rebase_and_push

        with (
            patch("hyperi_ci.push._has_upstream", return_value=False),
            patch("hyperi_ci.push.get_current_branch", return_value="feat/x"),
            patch("hyperi_ci.push._push_with_env", return_value=0) as mock_push,
        ):
            assert _rebase_and_push(cwd=None) == 0

        # No rebase attempted; push sets the upstream.
        mock_push.assert_called_once()
        assert mock_push.call_args.kwargs["args"] == ["-u", "origin", "feat/x"]

    def test_new_branch_unknown_name_errors(self):
        from hyperi_ci.push import _rebase_and_push

        with (
            patch("hyperi_ci.push._has_upstream", return_value=False),
            patch("hyperi_ci.push.get_current_branch", return_value=None),
            patch("hyperi_ci.push._push_with_env", return_value=0) as mock_push,
        ):
            assert _rebase_and_push(cwd=None) == 1
        mock_push.assert_not_called()

    def test_existing_upstream_rebases_then_pushes(self):
        from hyperi_ci.push import _rebase_and_push

        with (
            patch("hyperi_ci.push._has_upstream", return_value=True),
            patch("hyperi_ci.push.run_cmd") as mock_run,
            patch("hyperi_ci.push._push_with_env", return_value=0) as mock_push,
        ):
            assert _rebase_and_push(cwd=None) == 0

        # Rebase ran before the push.
        assert mock_run.call_args.args[0] == ["git", "pull", "--rebase"]
        mock_push.assert_called_once()

    def test_has_upstream_true_on_zero_exit(self):
        from hyperi_ci.push import _has_upstream

        with patch("hyperi_ci.push.run_cmd") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="origin/main\n")
            assert _has_upstream(cwd=None) is True

    def test_has_upstream_false_on_nonzero_exit(self):
        from hyperi_ci.push import _has_upstream

        with patch("hyperi_ci.push.run_cmd") as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stdout="")
            assert _has_upstream(cwd=None) is False


class TestBumpGate:
    """Predicted-bump gate in the publish path (issue #26)."""

    def _pred(self, bump, last_tag="v1.0.0", minor=None, major=None):
        from hyperi_ci.quality.predicted_bump import BumpPrediction

        return BumpPrediction(
            bump=bump,
            last_tag=last_tag,
            minor_reasons=minor or [],
            major_reasons=major or [],
        )

    def test_patch_passes(self):
        from hyperi_ci.push import _bump_gate

        with patch(
            "hyperi_ci.quality.predicted_bump.predict_bump",
            return_value=self._pred("patch"),
        ):
            assert _bump_gate(cwd=None, forced_bump=None) == 0

    def test_minor_blocked_without_optin(self, monkeypatch):
        from hyperi_ci.push import _bump_gate

        monkeypatch.delenv("HYPERCI_ALLOW_MINOR_BUMP", raising=False)
        monkeypatch.delenv("HYPERCI_ALLOW_MAJOR_BUMP", raising=False)
        # HYPERCI_ALLOW_FEAT also satisfies the minor gate — clear it too,
        # or running the suite under `hyperi-ci push` of a feat commit
        # (which exports it) fails this test on ambient env.
        monkeypatch.delenv("HYPERCI_ALLOW_FEAT", raising=False)
        with patch(
            "hyperi_ci.quality.predicted_bump.predict_bump",
            return_value=self._pred("minor", minor=["feat: x"]),
        ):
            assert _bump_gate(cwd=None, forced_bump=None) == 1

    def test_minor_allowed_with_env(self, monkeypatch):
        from hyperi_ci.push import _bump_gate

        monkeypatch.setenv("HYPERCI_ALLOW_MINOR_BUMP", "1")
        with patch(
            "hyperi_ci.quality.predicted_bump.predict_bump",
            return_value=self._pred("minor", minor=["feat: x"]),
        ):
            assert _bump_gate(cwd=None, forced_bump=None) == 0

    def test_minor_allowed_by_forced_bump_minor(self, monkeypatch):
        from hyperi_ci.push import _bump_gate

        monkeypatch.delenv("HYPERCI_ALLOW_MINOR_BUMP", raising=False)
        with patch(
            "hyperi_ci.quality.predicted_bump.predict_bump",
            return_value=self._pred("minor", minor=["feat: x"]),
        ):
            assert _bump_gate(cwd=None, forced_bump="minor") == 0

    def test_major_blocked_even_with_minor_optin(self, monkeypatch):
        from hyperi_ci.push import _bump_gate

        monkeypatch.setenv("HYPERCI_ALLOW_MINOR_BUMP", "1")
        monkeypatch.delenv("HYPERCI_ALLOW_MAJOR_BUMP", raising=False)
        with patch(
            "hyperi_ci.quality.predicted_bump.predict_bump",
            return_value=self._pred("major", major=["feat!: y"]),
        ):
            assert _bump_gate(cwd=None, forced_bump=None) == 1

    def test_major_allowed_with_env(self, monkeypatch):
        from hyperi_ci.push import _bump_gate

        monkeypatch.setenv("HYPERCI_ALLOW_MAJOR_BUMP", "1")
        with patch(
            "hyperi_ci.quality.predicted_bump.predict_bump",
            return_value=self._pred("major", major=["feat!: y"]),
        ):
            assert _bump_gate(cwd=None, forced_bump=None) == 0

    def test_none_passes(self):
        from hyperi_ci.push import _bump_gate

        with patch(
            "hyperi_ci.quality.predicted_bump.predict_bump",
            return_value=self._pred("none", last_tag=None),
        ):
            assert _bump_gate(cwd=None, forced_bump=None) == 0

    def test_forced_patch_does_not_authorise_minor(self, monkeypatch):
        from hyperi_ci.push import _bump_gate

        monkeypatch.delenv("HYPERCI_ALLOW_MINOR_BUMP", raising=False)
        monkeypatch.delenv("HYPERCI_ALLOW_FEAT", raising=False)
        with patch(
            "hyperi_ci.quality.predicted_bump.predict_bump",
            return_value=self._pred("minor", minor=["feat: x"]),
        ):
            assert _bump_gate(cwd=None, forced_bump="patch") == 1

    def test_forced_minor_does_not_authorise_major(self, monkeypatch):
        from hyperi_ci.push import _bump_gate

        monkeypatch.delenv("HYPERCI_ALLOW_MAJOR_BUMP", raising=False)
        monkeypatch.delenv("HYPERCI_ALLOW_BREAKING", raising=False)
        with patch(
            "hyperi_ci.quality.predicted_bump.predict_bump",
            return_value=self._pred("major", major=["feat!: y"]),
        ):
            assert _bump_gate(cwd=None, forced_bump="minor") == 1

    def test_allow_feat_authorises_minor(self, monkeypatch):
        # The commit-text opt-in carries the same intent one level up:
        # an operator who set HYPERCI_ALLOW_FEAT to get the feat: commit
        # through must not be re-blocked for the minor it implies.
        from hyperi_ci.push import _bump_gate

        monkeypatch.delenv("HYPERCI_ALLOW_MINOR_BUMP", raising=False)
        monkeypatch.setenv("HYPERCI_ALLOW_FEAT", "1")
        with patch(
            "hyperi_ci.quality.predicted_bump.predict_bump",
            return_value=self._pred("minor", minor=["feat: x"]),
        ):
            assert _bump_gate(cwd=None, forced_bump=None) == 0

    def test_allow_breaking_authorises_major(self, monkeypatch):
        from hyperi_ci.push import _bump_gate

        monkeypatch.delenv("HYPERCI_ALLOW_MAJOR_BUMP", raising=False)
        monkeypatch.setenv("HYPERCI_ALLOW_BREAKING", "1")
        with patch(
            "hyperi_ci.quality.predicted_bump.predict_bump",
            return_value=self._pred("major", major=["feat!: y"]),
        ):
            assert _bump_gate(cwd=None, forced_bump=None) == 0


class TestBumpGateWiring:
    """The gate's placement in _publish_push (issue #26 review findings).

    The authoritative gate runs AFTER the pull-rebase so history imported
    from origin is analysed; a gate failure must abort before the push;
    and the forced bump level must be forwarded.
    """

    def _patches(self, **overrides):
        defaults = {
            "require_gh": patch("hyperi_ci.push.require_gh", return_value=True),
            "branch": patch("hyperi_ci.push.get_current_branch", return_value="main"),
            "dirty": patch("hyperi_ci.push._check_dirty_tree", return_value=0),
            "check": patch("hyperi_ci.push._run_check", return_value=0),
            "msg": patch(
                "hyperi_ci.push._get_last_commit_message",
                return_value="fix: thing\n\nPublish: true\n",
            ),
        }
        defaults.update(overrides)
        return defaults

    def test_gate_failure_after_pull_aborts_before_push(self):
        from hyperi_ci.push import _publish_push

        call_order: list[str] = []
        with (
            patch("hyperi_ci.push.require_gh", return_value=True),
            patch("hyperi_ci.push.get_current_branch", return_value="main"),
            patch("hyperi_ci.push._check_dirty_tree", return_value=0),
            patch("hyperi_ci.push._run_check", return_value=0),
            patch(
                "hyperi_ci.push._get_last_commit_message",
                return_value="fix: thing\n\nPublish: true\n",
            ),
            patch(
                "hyperi_ci.push._pull_rebase",
                side_effect=lambda **kw: call_order.append("pull") or 0,
            ),
            patch(
                "hyperi_ci.push._bump_gate",
                side_effect=lambda **kw: call_order.append("gate") or 1,
            ),
            patch("hyperi_ci.push._push_with_env", return_value=0) as mock_push,
        ):
            rc = _publish_push(dry_run=False, force=False, bump=None, cwd=None)

        assert rc == 1
        # Gate ran after the pull (so imported history is analysed) and
        # the push never happened.
        assert call_order == ["pull", "gate"]
        mock_push.assert_not_called()

    def test_gate_receives_forced_bump(self):
        from hyperi_ci.push import _publish_push

        with (
            patch("hyperi_ci.push.require_gh", return_value=True),
            patch("hyperi_ci.push.get_current_branch", return_value="main"),
            patch("hyperi_ci.push._check_dirty_tree", return_value=0),
            patch("hyperi_ci.push._run_check", return_value=0),
            patch("hyperi_ci.push._compute_next_version", return_value="1.5.5"),
            patch("hyperi_ci.push._write_version_and_commit", return_value=0),
            patch("hyperi_ci.push._pull_rebase", return_value=0),
            patch("hyperi_ci.push._bump_gate", return_value=0) as mock_gate,
            patch("hyperi_ci.push._push_with_env", return_value=0),
        ):
            rc = _publish_push(dry_run=False, force=False, bump="minor", cwd=None)

        assert rc == 0
        assert mock_gate.call_args.kwargs["forced_bump"] == "minor"

    def test_dry_run_gates_local_range(self):
        # Dry run mutates nothing, so it gates the LOCAL range before the
        # early returns - a blocked bump surfaces without a pull.
        from hyperi_ci.push import _publish_push

        with (
            patch("hyperi_ci.push.require_gh", return_value=True),
            patch("hyperi_ci.push.get_current_branch", return_value="main"),
            patch("hyperi_ci.push._check_dirty_tree", return_value=0),
            patch("hyperi_ci.push._run_check", return_value=0),
            patch("hyperi_ci.push._bump_gate", return_value=1) as mock_gate,
            patch("hyperi_ci.push._pull_rebase", return_value=0) as mock_pull,
        ):
            rc = _publish_push(dry_run=True, force=False, bump=None, cwd=None)

        assert rc == 1
        mock_gate.assert_called_once()
        mock_pull.assert_not_called()
