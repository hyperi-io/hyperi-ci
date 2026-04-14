# Project:   HyperI CI
# File:      tests/unit/test_push.py
# Purpose:   Tests for push command
#
# License:   Proprietary — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hyperi_ci.push import (
    _check_dirty_tree,
    _check_not_ci_commit,
    _detect_new_tag,
    _version_sort_key,
    push,
)


class TestPushFlagValidation:
    """Test mutually exclusive flags."""

    def test_release_and_no_ci_mutually_exclusive(self) -> None:
        rc = push(release=True, no_ci=True)
        assert rc == 1

    def test_default_push_accepted(self) -> None:
        with patch("hyperi_ci.push._default_push", return_value=0) as mock:
            rc = push()
            assert rc == 0
            mock.assert_called_once()

    def test_release_flag_routes_to_release_push(self) -> None:
        with patch("hyperi_ci.push._release_push", return_value=0) as mock:
            rc = push(release=True)
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


class TestVersionSortKey:
    """Test version tag sorting."""

    def test_simple_version(self) -> None:
        assert _version_sort_key("v1.2.3") == (1, 2, 3)

    def test_without_v_prefix(self) -> None:
        assert _version_sort_key("1.2.3") == (1, 2, 3)

    def test_two_part_version(self) -> None:
        assert _version_sort_key("v1.2") == (1, 2)

    def test_non_numeric_part(self) -> None:
        assert _version_sort_key("v1.2.beta") == (1, 2, 0)

    def test_sorting_order(self) -> None:
        tags = ["v1.0.0", "v2.1.0", "v1.9.0", "v2.0.1"]
        sorted_tags = sorted(tags, key=_version_sort_key, reverse=True)
        assert sorted_tags == ["v2.1.0", "v2.0.1", "v1.9.0", "v1.0.0"]


class TestDetectNewTag:
    """Test tag diff detection."""

    def test_detects_new_tag(self) -> None:
        before = {"v1.0.0", "v1.1.0"}
        after = {"v1.0.0", "v1.1.0", "v1.2.0"}

        with (
            patch("hyperi_ci.push.run_cmd"),
            patch("hyperi_ci.push._get_current_tags", return_value=after),
        ):
            result = _detect_new_tag(before, cwd=None)
            assert result == "v1.2.0"

    def test_no_new_tag(self) -> None:
        before = {"v1.0.0", "v1.1.0"}

        with (
            patch("hyperi_ci.push.run_cmd"),
            patch("hyperi_ci.push._get_current_tags", return_value=before),
        ):
            result = _detect_new_tag(before, cwd=None)
            assert result is None

    def test_multiple_new_tags_returns_latest(self) -> None:
        before = {"v1.0.0"}
        after = {"v1.0.0", "v1.1.0", "v1.2.0"}

        with (
            patch("hyperi_ci.push.run_cmd"),
            patch("hyperi_ci.push._get_current_tags", return_value=after),
        ):
            result = _detect_new_tag(before, cwd=None)
            assert result == "v1.2.0"


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
            patch("hyperi_ci.push._rebase_and_push") as mock_push,
        ):
            from hyperi_ci.push import _default_push

            rc = _default_push(dry_run=True, force=False, cwd=None)
            assert rc == 0
            mock_push.assert_not_called()


class TestReleasePush:
    """Test release push flow decision branches."""

    def test_not_on_main_aborts(self) -> None:
        with (
            patch("hyperi_ci.push.require_gh", return_value=True),
            patch("hyperi_ci.push.get_current_branch", return_value="feat/thing"),
        ):
            from hyperi_ci.push import _release_push

            rc = _release_push(dry_run=False, force=False, cwd=None)
            assert rc == 1

    def test_no_gh_cli_aborts(self) -> None:
        with patch("hyperi_ci.push.require_gh", return_value=False):
            from hyperi_ci.push import _release_push

            rc = _release_push(dry_run=False, force=False, cwd=None)
            assert rc == 1

    def test_ci_failure_aborts_publish(self) -> None:
        with (
            patch("hyperi_ci.push.require_gh", return_value=True),
            patch("hyperi_ci.push.get_current_branch", return_value="main"),
            patch("hyperi_ci.push._check_dirty_tree", return_value=0),
            patch("hyperi_ci.push._run_check", return_value=0),
            patch("hyperi_ci.push._get_current_tags", return_value=set()),
            patch("hyperi_ci.push._get_latest_run_id", return_value="100"),
            patch("hyperi_ci.push._rebase_and_push", return_value=0),
            patch("hyperi_ci.push._poll_for_new_run", return_value="101"),
            patch("hyperi_ci.watch.watch_run", return_value=1),
        ):
            from hyperi_ci.push import _release_push

            rc = _release_push(dry_run=False, force=False, cwd=None)
            assert rc == 1

    def test_no_new_tag_exits_cleanly(self) -> None:
        with (
            patch("hyperi_ci.push.require_gh", return_value=True),
            patch("hyperi_ci.push.get_current_branch", return_value="main"),
            patch("hyperi_ci.push._check_dirty_tree", return_value=0),
            patch("hyperi_ci.push._run_check", return_value=0),
            patch("hyperi_ci.push._get_current_tags", return_value={"v1.0.0"}),
            patch("hyperi_ci.push._get_latest_run_id", return_value="100"),
            patch("hyperi_ci.push._rebase_and_push", return_value=0),
            patch("hyperi_ci.push._poll_for_new_run", return_value="101"),
            patch("hyperi_ci.watch.watch_run", return_value=0),
            patch("hyperi_ci.push._detect_new_tag", return_value=None),
        ):
            from hyperi_ci.push import _release_push

            rc = _release_push(dry_run=False, force=False, cwd=None)
            assert rc == 0


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
