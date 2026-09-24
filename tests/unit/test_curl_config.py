# Project:   HyperI CI
# File:      tests/unit/test_curl_config.py
# Purpose:   A secret handed to curl on stdin arrives intact and alone
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for ``hyperi_ci.curl_config``.

A token or webhook on curl's argv is readable by any process on the host
through ``/proc/<pid>/cmdline``. ``curl -K -`` reads it from stdin instead, and
that only holds if the config line survives curl's quoting rules.
"""

import http.server
import shutil
import threading
from collections.abc import Iterator

import pytest

from hyperi_ci.common import run_cmd
from hyperi_ci.curl_config import config_line


class TestConfigLine:
    def test_a_plain_value_is_quoted(self) -> None:
        assert config_line("url", "https://hooks.example/x") == (
            'url = "https://hooks.example/x"\n'
        )

    def test_quote_and_backslash_are_escaped(self) -> None:
        assert config_line("header", 'a"b\\c') == 'header = "a\\"b\\\\c"\n'

    def test_a_newline_cannot_start_a_second_option(self) -> None:
        line = config_line("header", "x\nurl = https://elsewhere\r")
        assert line.count("\n") == 1
        assert line.endswith('"\n')
        assert "\r" not in line


@pytest.fixture
def listener() -> Iterator[tuple[str, list[tuple[str, str | None]]]]:
    """Record the path and Authorization header of each POST to localhost."""
    seen: list[tuple[str, str | None]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            seen.append((self.path, self.headers.get("Authorization")))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl not installed")
def test_real_curl_reads_the_header_and_url_from_stdin(
    listener: tuple[str, list[tuple[str, str | None]]],
) -> None:
    base, seen = listener
    auth = 'Bearer tok_TESTONLY_"abc"\\123'
    stdin = config_line("header", f"Authorization: {auth}")
    stdin += config_line("url", f"{base}/hook")

    result = run_cmd(
        ["curl", "-sS", "--noproxy", "*", "-X", "POST", "-K", "-", "-d", "{}"],
        capture=True,
        check=False,
        stdin_text=stdin,
    )

    assert result.returncode == 0, result.stderr
    assert seen == [("/hook", auth)]
