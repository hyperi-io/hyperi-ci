# Project:   HyperI CI
# File:      tests/unit/test_curl_fetch.py
# Purpose:   Every Python-side fetch retries, and writes to a file
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for the one curl helper in ``hyperi_ci.common``.

A fetch that did not retry turned one GitHub or CDN 500 into a failed job or
a failed runner-image bake. A fetch that retried everything spent five more
unauthenticated requests on a GitHub rate-limit 403. The helper retries what
asking again can fix, gives each attempt a time limit, and keeps the body in
an ``-o`` file so stdout carries only the status it judges the attempt by.
"""

import ast
import http.server
import re
import shutil
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from hyperi_ci import common

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src" / "hyperi_ci"
_ACTIONS = _ROOT / ".github" / "actions"
# These POST data, and a retried POST is not safe to repeat.
_SENDERS = frozenset({"argocd/gitops_push.py", "release_notify.py"})
_HELPER = ("common.py", "curl_fetch")


def _contains(cmd: list[str], run: list[str]) -> bool:
    return any(cmd[i : i + len(run)] == run for i in range(len(cmd)))


def _fake_curl(
    monkeypatch: pytest.MonkeyPatch,
    *,
    rc: int = 0,
    status: str = "200",
    body: bytes | None = b"BODY",
) -> list[tuple[list[str], dict]]:
    """Stand in for curl: record the call, write ``body`` to its -o file."""
    calls: list[tuple[list[str], dict]] = []

    def fake(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((cmd, kwargs))
        if body is not None:
            Path(cmd[cmd.index("-o") + 1]).write_bytes(body)
        return subprocess.CompletedProcess(cmd, rc, stdout=status, stderr="")

    monkeypatch.setattr(common, "run_cmd", fake)
    return calls


class TestCurlFetch:
    def test_the_body_goes_to_the_file_and_the_status_to_stdout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls = _fake_curl(monkeypatch)
        dest = tmp_path / "out"

        common.curl_fetch("https://example.invalid/tool", dest)

        [(cmd, kwargs)] = calls
        assert cmd[0] == "curl"
        assert cmd[-1] == "https://example.invalid/tool"
        assert cmd[cmd.index("-o") + 1] == str(dest)
        assert _contains(cmd, ["-w", "%{http_code}"])
        assert "-fsS" in cmd
        assert "-L" in cmd
        assert kwargs["check"] is False
        assert kwargs["capture"] is True

    def test_curl_is_never_asked_to_retry_by_itself(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # curl's own retry cannot tell a 404 from a 503 under -f.
        calls = _fake_curl(monkeypatch)
        common.curl_fetch("https://example.invalid/x", tmp_path / "out")
        [(cmd, _)] = calls
        assert not [arg for arg in cmd if arg.startswith("--retry")]

    def test_each_attempt_has_a_time_limit_and_a_backstop(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls = _fake_curl(monkeypatch)
        common.curl_fetch("https://example.invalid/x", tmp_path / "out")
        [(cmd, kwargs)] = calls
        assert _contains(cmd, ["--connect-timeout", "10"])
        assert _contains(cmd, ["--max-time", "180"])
        # A backstop inside --max-time kills a curl that is still within its limit.
        assert kwargs["timeout"] > 180

    def test_max_time_moves_the_limit_and_the_backstop(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls = _fake_curl(monkeypatch)
        common.curl_fetch("https://x.invalid", tmp_path / "o", max_time=300)
        [(cmd, kwargs)] = calls
        assert _contains(cmd, ["--max-time", "300"])
        assert kwargs["timeout"] > 300

    def test_extra_flags_reach_curl_before_the_url(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls = _fake_curl(monkeypatch)
        common.curl_fetch(
            "https://example.invalid/x",
            tmp_path / "out",
            extra=("--proto", "=https"),
        )
        [(cmd, _)] = calls
        assert _contains(cmd, ["--proto", "=https"])
        assert cmd.index("--proto") < cmd.index("https://example.invalid/x")

    def test_follow_redirects_off_drops_the_flag(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls = _fake_curl(monkeypatch)
        common.curl_fetch("https://x.invalid", tmp_path / "o", follow_redirects=False)
        [(cmd, _)] = calls
        assert "-L" not in cmd


# Each outcome is (curl exit code, what -w printed, curl's error line).
Outcome = tuple[int, str, str]
Script = tuple[list[Outcome], list[list[str]], list[float], list[str]]


@pytest.fixture
def scripted_curl(monkeypatch: pytest.MonkeyPatch) -> Script:
    """Answer each curl attempt from a script, and skip the retry backoff.

    Returns ``(outcomes, attempts, sleeps, logged)``. Once ``outcomes`` is
    empty every attempt succeeds.
    """
    outcomes: list[Outcome] = []
    attempts: list[list[str]] = []
    sleeps: list[float] = []
    logged: list[str] = []

    def fake(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        attempts.append(cmd)
        rc, status, stderr = outcomes.pop(0) if outcomes else (0, "200", "")
        return subprocess.CompletedProcess(cmd, rc, stdout=status, stderr=stderr)

    monkeypatch.setattr(common, "run_cmd", fake)
    monkeypatch.setattr(common.time, "sleep", sleeps.append)
    monkeypatch.setattr(common, "info", logged.append)
    return outcomes, attempts, sleeps, logged


def _fetch(tmp_path: Path) -> int:
    return common.curl_fetch("https://example.invalid/x", tmp_path / "o").returncode


class TestCurlRetryRule:
    @pytest.mark.parametrize(
        "failure",
        [
            (22, "500", "curl: (22) The requested URL returned error: 500"),
            (22, "502", ""),
            (22, "503", ""),
            (22, "408", ""),
            (22, "429", ""),
            (18, "200", "curl: (18) transfer closed with 5000 bytes remaining"),
            (56, "200", "curl: (56) Recv failure: Connection reset by peer"),
            (7, "000", "curl: (7) Failed to connect"),
            (28, "000", "curl: (28) Operation timed out"),
            (22, "", ""),
        ],
        ids=lambda outcome: f"exit{outcome[0]}-{outcome[1] or 'nostatus'}",
    )
    def test_a_transient_failure_is_retried(
        self, scripted_curl: Script, tmp_path: Path, failure: Outcome
    ) -> None:
        outcomes, attempts, sleeps, _ = scripted_curl
        outcomes.append(failure)
        assert _fetch(tmp_path) == 0
        assert len(attempts) == 2
        assert len(sleeps) == 1

    @pytest.mark.parametrize("status", ["400", "401", "403", "404", "410"])
    def test_a_status_retrying_cannot_change_is_final(
        self, scripted_curl: Script, tmp_path: Path, status: str
    ) -> None:
        outcomes, attempts, sleeps, _ = scripted_curl
        outcomes.append((22, status, f"curl: (22) returned error: {status}"))
        assert _fetch(tmp_path) == 22
        assert len(attempts) == 1
        assert sleeps == []

    def test_gives_up_after_five_retries_with_the_last_failure(
        self, scripted_curl: Script, tmp_path: Path
    ) -> None:
        outcomes, attempts, sleeps, _ = scripted_curl
        outcomes.extend([(22, "503", "")] * 5 + [(28, "000", "")])
        assert _fetch(tmp_path) == 28
        assert len(attempts) == 6
        # Backoff doubles from about a second, each wait shortened by up to half.
        for waited, ceiling in zip(sleeps, (1.0, 2.0, 4.0, 8.0, 16.0), strict=True):
            assert ceiling / 2 <= waited <= ceiling

    def test_no_retry_starts_after_the_window(
        self,
        scripted_curl: Script,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        outcomes, attempts, sleeps, _ = scripted_curl
        outcomes.append((22, "503", ""))
        clock = iter([0.0])
        monkeypatch.setattr(common.time, "monotonic", lambda: next(clock, 600.0))
        assert _fetch(tmp_path) == 22
        assert len(attempts) == 1
        assert sleeps == []

    def test_each_failure_is_logged_with_curls_own_line(
        self, scripted_curl: Script, tmp_path: Path
    ) -> None:
        outcomes, _, _, logged = scripted_curl
        outcomes.extend(
            [
                (22, "503", "curl: (22) The requested URL returned error: 503"),
                (22, "404", "curl: (22) The requested URL returned error: 404"),
            ]
        )
        assert _fetch(tmp_path) == 22
        retried, final = logged
        assert "error: 503" in retried
        assert "retry 1 of 5" in retried
        assert final.startswith("https://example.invalid/x: ")
        assert final.endswith("curl: (22) The requested URL returned error: 404")


class TestCurlRead:
    def test_returns_the_body_and_removes_the_temp_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _fake_curl(monkeypatch, body=b"#!/bin/sh\necho hi\n")

        rc, body = common.curl_read("https://example.invalid/install.sh")

        assert (rc, body) == (0, b"#!/bin/sh\necho hi\n")
        [(cmd, _)] = calls
        assert not Path(cmd[cmd.index("-o") + 1]).exists()

    def test_a_failed_fetch_returns_no_partial_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_curl(monkeypatch, rc=22, status="404", body=b"partial")
        assert common.curl_read("https://example.invalid/x") == (22, b"")

    def test_a_success_with_no_file_reads_as_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_curl(monkeypatch, body=None)
        assert common.curl_read("https://example.invalid/x") == (0, b"")

    def test_options_pass_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _fake_curl(monkeypatch)
        common.curl_read(
            "https://x.invalid",
            extra=("--proto", "=https"),
            follow_redirects=False,
            max_time=7,
        )
        [(cmd, _)] = calls
        assert _contains(cmd, ["--proto", "=https"])
        assert "-L" not in cmd
        assert _contains(cmd, ["--max-time", "7"])


_BODY = b"0123456789" * 1000


@pytest.fixture
def flaky_server(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[str, list[str], list[str]]]:
    """Serve ``_BODY`` on localhost, failing first in the ways ``plan`` lists.

    A number answers with that HTTP status. ``partial`` promises the whole
    body and closes the connection halfway through it. The retry backoff is
    skipped.
    """
    plan: list[str] = []
    hits: list[str] = []
    monkeypatch.setattr(common.time, "sleep", lambda _s: None)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            step = plan.pop(0) if plan else "ok"
            hits.append(step)
            if step.isdigit():
                self.send_response(int(step))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(_BODY)))
            self.end_headers()
            self.wfile.write(_BODY[: len(_BODY) // 2] if step == "partial" else _BODY)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/body", plan, hits
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl not installed")
class TestRealCurlRetries:
    """The real curl binary, against a server that fails once and then serves."""

    @pytest.mark.parametrize("failure", ["500", "503", "408", "429", "partial"])
    def test_one_failure_then_the_body_exactly_once(
        self, flaky_server: tuple[str, list[str], list[str]], failure: str
    ) -> None:
        url, plan, hits = flaky_server
        plan.append(failure)

        rc, body = common.curl_read(url, extra=("--noproxy", "*"))

        assert rc == 0
        assert hits == [failure, "ok"]
        assert body == _BODY

    @pytest.mark.parametrize("status", ["403", "404"])
    def test_a_final_status_is_asked_once(
        self, flaky_server: tuple[str, list[str], list[str]], status: str
    ) -> None:
        url, plan, hits = flaky_server
        plan.append(status)

        rc, body = common.curl_read(url, extra=("--noproxy", "*"))

        assert rc == 22
        assert hits == [status]
        assert body == b""


class TestDownloadArtefact:
    @pytest.fixture
    def errors(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        logged: list[str] = []
        monkeypatch.setattr(common, "error", logged.append)
        return logged

    def test_returns_the_body(
        self, monkeypatch: pytest.MonkeyPatch, errors: list[str]
    ) -> None:
        _fake_curl(monkeypatch, body=b"BINARY")
        assert common.download_artefact("tool", "https://x.invalid") == b"BINARY"
        assert errors == []

    def test_keeps_the_per_attempt_limits(
        self, monkeypatch: pytest.MonkeyPatch, errors: list[str]
    ) -> None:
        calls = _fake_curl(monkeypatch)
        common.download_artefact("tool", "https://x.invalid")
        [(cmd, kwargs)] = calls
        assert _contains(cmd, ["--connect-timeout", "10"])
        assert _contains(cmd, ["--max-time", "180"])
        assert kwargs["timeout"] > 180

    def test_a_curl_failure_is_named_and_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, errors: list[str]
    ) -> None:
        _fake_curl(monkeypatch, rc=22, status="404")
        assert common.download_artefact("hadolint", "https://x.invalid") is None
        assert errors == ["Failed to download hadolint (curl exit 22)"]

    def test_an_empty_body_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, errors: list[str]
    ) -> None:
        _fake_curl(monkeypatch, body=b"")
        assert common.download_artefact("tool", "https://x.invalid") is None
        assert errors == ["Failed to download tool (curl exit 0)"]

    @pytest.mark.parametrize(
        "raised",
        [FileNotFoundError(2, "curl"), subprocess.TimeoutExpired(["curl"], 800)],
    )
    def test_a_missing_or_hung_curl_returns_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
        errors: list[str],
        raised: Exception,
    ) -> None:
        def explode(*_a: object, **_k: object) -> None:
            raise raised

        monkeypatch.setattr(common, "run_cmd", explode)
        assert common.download_artefact("tool", "https://x.invalid") is None
        assert errors == ["Failed to download tool (network error / timeout)"]


def _curl_argvs(source: str) -> list[tuple[int, str]]:
    """Return ``(line, enclosing function)`` for each list or tuple led by curl."""
    found: list[tuple[int, str]] = []

    def visit(node: ast.AST, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.List, ast.Tuple)) and child.elts:
                first = child.elts[0]
                value = first.value if isinstance(first, ast.Constant) else None
                if value == "curl" or (
                    isinstance(value, str) and value.endswith("/curl")
                ):
                    found.append((child.lineno, function))
            is_function = isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            visit(child, child.name if is_function else function)

    visit(ast.parse(source), "")
    return found


def _scan_src() -> dict[str, list[tuple[int, str]]]:
    found = {}
    for path in sorted(_SRC.rglob("*.py")):
        hits = _curl_argvs(path.read_text(encoding="utf-8"))
        if hits:
            found[path.relative_to(_SRC).as_posix()] = hits
    return found


class TestEveryFetchUsesTheHelper:
    def test_no_curl_argv_bypasses_the_helper(self) -> None:
        bypassing = [
            f"src/hyperi_ci/{name}:{line} (in {function or 'module'})"
            for name, hits in _scan_src().items()
            if name not in _SENDERS
            for line, function in hits
            if (name, function) != _HELPER
        ]
        assert not bypassing, (
            "Fetch through hyperi_ci.common.curl_fetch or curl_read, which retry "
            "and write to a file:\n" + "\n".join(bypassing)
        )

    def test_the_scan_sees_the_helper_and_each_sender(self) -> None:
        # An exemption for a file that no longer runs curl is dead, and a scan
        # that misses the helper proves nothing.
        found = _scan_src()
        assert _HELPER[1] in {function for _, function in found[_HELPER[0]]}
        assert _SENDERS <= found.keys()

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ('def f():\n    run(["curl", "-fsSL", url])', [(2, "f")]),
            ('X = ("curl", url)', [(1, "")]),
            ('def f():\n    run(["/usr/bin/curl", url])', [(2, "f")]),
            ('def f():\n    run(["git", "curl"])', []),
            ('def f():\n    run(["libcurl-config"])', []),
            ('def f():\n    which("curl")', []),
        ],
    )
    def test_scan_rules(self, source: str, expected: list[tuple[int, str]]) -> None:
        assert _curl_argvs(source) == expected


def test_the_composite_actions_carry_the_helpers_retry_budget() -> None:
    # curl's own retry cannot give up on a 4xx, so the shell copies keep only
    # the helper's attempt count and window, and those must not drift.
    retry = (
        f"--retry {common._CURL_RETRIES} --retry-all-errors --retry-delay 2 "
        f"--retry-max-time {common._CURL_RETRY_MAX_TIME}"
    )
    downloads = [
        line.strip()
        for path in sorted(_ACTIONS.rglob("*.yml"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if re.search(r"\bcurl\b", line) and " -o " in line
    ]
    assert downloads
    assert all(retry in line for line in downloads), downloads
