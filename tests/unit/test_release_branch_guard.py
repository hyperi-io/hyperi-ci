# Project:   HyperI CI
# File:      tests/unit/test_release_branch_guard.py
# Purpose:   The release-branch-guard composite's verdict on a moved branch
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""release-branch-guard stops a release before it publishes once main moved (issue #717)."""

import http.client
import http.server
import importlib.util
import json
import re
import threading
import urllib.error
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

import pytest
import yaml

_ACTION = (
    Path(__file__).parent.parent.parent / ".github" / "actions" / "release-branch-guard"
)
_SCRIPT = _ACTION / "branch_guard.py"


def _guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("branch_guard", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SHA = "a" * 40
TIP = "b" * 40
OTHER = "d" * 40


def _verdict(
    *, tip: str, tagged: str = "", version: str = "1.2.3", image: str = ""
) -> tuple:
    return _guard().verdict("main", SHA, tip, tagged, version, "pushing", image)


class TestVerdict:
    def test_an_unmoved_branch_passes(self) -> None:
        code, message = _verdict(tip=SHA)
        assert code == 0
        assert "::error::" not in message

    def test_a_re_run_after_the_tag_was_cut_passes(self) -> None:
        # Publish failed after the tag push, main moved, and the job is re-run.
        code, message = _verdict(tip=TIP, tagged=SHA)
        assert code == 0
        assert message.startswith(f"v1.2.3 already tags {SHA}")

    def test_a_tag_at_another_commit_does_not_pass(self) -> None:
        code, _ = _verdict(tip=TIP, tagged=OTHER)
        assert code == 1

    def test_a_tag_with_no_version_does_not_pass(self) -> None:
        code, _ = _verdict(tip=TIP, tagged=SHA, version="")
        assert code == 1

    def test_a_moved_branch_fails_and_gives_both_recoveries(self) -> None:
        code, message = _verdict(tip=TIP)
        assert code == 1
        assert message.startswith(f"::error::main moved past {SHA} (now {TIP})")
        assert "'hyperi-ci release'" in message
        assert "'hyperi-ci release v1.2.3'" in message

    def test_a_moved_branch_names_the_image_already_pushed(self) -> None:
        image = "ghcr.io/hyperi-io/x:1.2.3@sha256:" + "c" * 64
        _, message = _verdict(tip=TIP, image=image)
        assert f"The image {image} was already pushed" in message

    def test_an_unreadable_tip_fails_closed(self) -> None:
        code, message = _verdict(tip="")
        assert code == 1
        assert message.startswith("::error::Could not read main")
        assert "'hyperi-ci release v1.2.3'" in message


Route = Callable[[str], tuple]


@contextmanager
def _github(route: Route) -> Iterator[tuple[str, list[str]]]:
    """Serve ``route(path) -> (status, body)`` on localhost as a stand-in API."""
    seen: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
            seen.append(self.path)
            assert self.headers.get("Authorization") == "Bearer t"
            status, body = route(self.path)
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


class TestTheApi:
    def test_tip_of_reads_the_branch_ref(self) -> None:
        def route(path: str) -> tuple:
            return 200, {"object": {"sha": TIP, "type": "commit"}}

        with _github(route) as (api, seen):
            assert _guard().tip_of(api, "hyperi-io/x", "release/1.x", "t") == TIP
        assert seen == ["/repos/hyperi-io/x/git/ref/heads/release/1.x"]

    def test_tag_commit_dereferences_an_annotated_tag_once(self) -> None:
        def route(path: str) -> tuple:
            if path.endswith("/git/ref/tags/v1.2.3"):
                return 200, {"object": {"sha": OTHER, "type": "tag"}}
            return 200, {"object": {"sha": SHA, "type": "commit"}}

        with _github(route) as (api, seen):
            assert _guard().tag_commit(api, "hyperi-io/x", "v1.2.3", "t") == SHA
        assert seen == [
            "/repos/hyperi-io/x/git/ref/tags/v1.2.3",
            f"/repos/hyperi-io/x/git/tags/{OTHER}",
        ]

    def test_a_missing_tag_is_empty_and_not_retried(self) -> None:
        with _github(lambda path: (404, {"message": "Not Found"})) as (api, seen):
            assert _guard().tag_commit(api, "hyperi-io/x", "v9.9.9", "t") == ""
        assert len(seen) == 1

    def test_a_server_error_is_retried_then_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        guard = _guard()
        monkeypatch.setattr(guard.time, "sleep", lambda seconds: None)
        with _github(lambda path: (502, {})) as (api, seen):
            assert guard.tip_of(api, "hyperi-io/x", "main", "t") == ""
        assert len(seen) == guard._ATTEMPTS


@pytest.mark.parametrize(
    "error",
    [
        http.client.RemoteDisconnected("Remote end closed connection"),
        ConnectionResetError("reset by peer"),
        urllib.error.URLError("temporary failure in name resolution"),
        TimeoutError("timed out"),
    ],
)
def test_a_dropped_connection_is_retried_then_fails_closed(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    guard = _guard()
    calls: list[str] = []

    def drop(request: object, timeout: float) -> object:
        calls.append(str(request))
        raise error

    monkeypatch.setattr(guard.urllib.request, "urlopen", drop)
    monkeypatch.setattr(guard.time, "sleep", lambda seconds: None)
    assert guard.tip_of("https://api.example", "hyperi-io/x", "main", "t") == ""
    assert len(calls) == guard._ATTEMPTS


class TestMain:
    @pytest.fixture
    def env(self, monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
        monkeypatch.setenv("RELEASE_BRANCH", "beta")
        monkeypatch.setenv("RELEASE_SHA", SHA)
        monkeypatch.setenv("RELEASE_VERSION", "1.2.3")
        monkeypatch.setenv("GITHUB_REPOSITORY", "hyperi-io/x")
        monkeypatch.setenv("GITHUB_API_URL", "https://api.github.com")
        monkeypatch.setenv("GH_TOKEN", "t")
        monkeypatch.setenv("GUARD_BEFORE", "pushing")
        monkeypatch.delenv("PUSHED_IMAGE", raising=False)
        return monkeypatch

    def test_a_moved_branch_with_no_tag_fails(
        self, env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        guard = _guard()
        env.setattr(guard, "tip_of", lambda api, repo, branch, token: TIP)
        env.setattr(guard, "tag_commit", lambda api, repo, tag, token: "")
        assert guard.main() == 1
        assert "beta moved past" in capsys.readouterr().out

    def test_a_moved_branch_with_the_tag_here_passes(
        self, env: pytest.MonkeyPatch
    ) -> None:
        guard = _guard()
        asked: list[str] = []

        def tagged(api: str, repo: str, tag: str, token: str) -> str:
            asked.append(tag)
            return SHA

        env.setattr(guard, "tip_of", lambda api, repo, branch, token: TIP)
        env.setattr(guard, "tag_commit", tagged)
        assert guard.main() == 0
        assert asked == ["v1.2.3"]

    def test_an_unmoved_branch_asks_for_no_tag(self, env: pytest.MonkeyPatch) -> None:
        guard = _guard()
        env.setattr(guard, "tip_of", lambda api, repo, branch, token: SHA)

        def no_tag(api: str, repo: str, tag: str, token: str) -> str:
            raise AssertionError("the tag was looked up for an unmoved branch")

        env.setattr(guard, "tag_commit", no_tag)
        assert guard.main() == 0

    def test_it_refuses_without_its_inputs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in ("RELEASE_BRANCH", "RELEASE_SHA", "GITHUB_REPOSITORY"):
            monkeypatch.delenv(name, raising=False)
        assert _guard().main() == 1


def test_the_action_sets_exactly_the_variables_the_script_reads() -> None:
    action = yaml.safe_load((_ACTION / "action.yml").read_text(encoding="utf-8"))
    (step,) = action["runs"]["steps"]
    provided = set(step["env"])
    source = _SCRIPT.read_text(encoding="utf-8")
    read = set(re.findall(r'os\.environ\.get\("([A-Z_]+)"', source))
    # The runner sets these for every step.
    runner = {"GITHUB_REPOSITORY", "GITHUB_API_URL"}
    assert read - runner == provided
