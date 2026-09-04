# Project:   HyperI CI
# File:      tests/unit/test_logs.py
# Purpose:   Tests for hyperi_ci.logs — the run-log download lands the zip on
#            disk and extracts it, and a failed download says why.
#
# License:   BUSL-1.1 — HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import io
import subprocess
import zipfile
from unittest.mock import patch

from hyperi_ci.logs import _download_logs


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ci / Quality/1_Run quality checks.txt", "line one\nline two\n")
    return buf.getvalue()


class TestDownloadLogs:
    def test_zip_from_gh_api_is_written_and_extracted(self) -> None:
        def fake_run(cmd, **kwargs):
            assert cmd[:2] == ["gh", "api"]
            assert cmd[2].endswith("/actions/runs/424242/logs")
            kwargs["stdout"].write(_zip_bytes())
            return subprocess.CompletedProcess(cmd, 0)

        with patch("hyperi_ci.logs.subprocess.run", side_effect=fake_run):
            log_dir = _download_logs("424242")

        assert log_dir is not None
        extracted = sorted(
            p.relative_to(log_dir).as_posix() for p in log_dir.rglob("*.txt")
        )
        assert extracted == ["ci / Quality/1_Run quality checks.txt"]
        assert not (log_dir / "logs.zip").exists()

    def test_gh_api_failure_returns_none_with_the_reason(self) -> None:
        def fake_run(cmd, **kwargs):
            raise subprocess.CalledProcessError(1, cmd, stderr=b"HTTP 404: Not Found")

        with (
            patch("hyperi_ci.logs.subprocess.run", side_effect=fake_run),
            patch("hyperi_ci.logs.error") as err,
        ):
            assert _download_logs("424242") is None
        assert "HTTP 404" in err.call_args.args[0]

    def test_non_zip_response_returns_none(self) -> None:
        def fake_run(cmd, **kwargs):
            kwargs["stdout"].write(b"<html>not a zip</html>")
            return subprocess.CompletedProcess(cmd, 0)

        with (
            patch("hyperi_ci.logs.subprocess.run", side_effect=fake_run),
            patch("hyperi_ci.logs.error") as err,
        ):
            assert _download_logs("424242") is None
        assert "not a zip" in err.call_args.args[0]
