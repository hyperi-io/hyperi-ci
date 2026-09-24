# Project:   HyperI CI
# File:      tests/unit/test_url_read.py
# Purpose:   The urllib helper retries a transient failure and nothing else
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for ``hyperi_ci.common.url_read``.

One 5xx or dropped connection on an unretried probe read as "this APT repo has
no packages for our codename". These pin which failures are asked again, how
many times, and that everything else surfaces at once.
"""

import email.message
import http.client
import urllib.error
import urllib.request

import pytest

from hyperi_ci import common

_URL = "https://example.invalid/dists/noble/Release"

Script = tuple[list[Exception | bytes], list[str], list[float]]


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(_URL, code, "scripted", email.message.Message(), None)


def _read(attempts: int = common.URL_ATTEMPTS) -> bytes:
    return common.url_read(urllib.request.Request(_URL), timeout=10, attempts=attempts)


@pytest.mark.parametrize(
    "failure",
    [
        _http_error(500),
        _http_error(502),
        _http_error(503),
        _http_error(408),
        _http_error(429),
        urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")),
        TimeoutError("timed out"),
        ConnectionResetError(104, "Connection reset by peer"),
        http.client.IncompleteRead(b"", 10),
    ],
    ids=lambda exc: type(exc).__name__ + str(getattr(exc, "code", "")),
)
def test_a_transient_failure_is_retried(
    fake_urlopen: Script, failure: Exception
) -> None:
    outcomes, asked, sleeps = fake_urlopen
    outcomes.extend([failure, b"BODY"])
    assert _read() == b"BODY"
    assert len(asked) == 2
    assert len(sleeps) == 1


@pytest.mark.parametrize("code", [400, 401, 403, 404, 410])
def test_a_status_retrying_cannot_change_raises_at_once(
    fake_urlopen: Script, code: int
) -> None:
    outcomes, asked, sleeps = fake_urlopen
    outcomes.extend([_http_error(code), b"never served"])
    with pytest.raises(urllib.error.HTTPError) as caught:
        _read()
    assert caught.value.code == code
    assert len(asked) == 1
    assert sleeps == []


def test_gives_up_after_four_attempts_with_the_last_failure(
    fake_urlopen: Script,
) -> None:
    outcomes, asked, sleeps = fake_urlopen
    outcomes.extend(
        [_http_error(500), _http_error(502), _http_error(503), _http_error(504)]
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        _read()
    assert caught.value.code == 504
    assert len(asked) == 4
    # Backoff doubles from about a second, each wait shortened by up to half.
    assert len(sleeps) == 3
    for waited, ceiling in zip(sleeps, (1.0, 2.0, 4.0), strict=True):
        assert ceiling / 2 <= waited <= ceiling


def test_a_dead_host_raises_its_own_error(fake_urlopen: Script) -> None:
    _, asked, _ = fake_urlopen
    with pytest.raises(urllib.error.URLError, match="no route to host"):
        _read()
    assert len(asked) == common.URL_ATTEMPTS


def test_one_attempt_never_retries(fake_urlopen: Script) -> None:
    outcomes, asked, sleeps = fake_urlopen
    outcomes.extend([_http_error(503), b"never served"])
    with pytest.raises(urllib.error.HTTPError):
        _read(attempts=1)
    assert len(asked) == 1
    assert sleeps == []


def test_a_bug_is_not_mistaken_for_the_network(fake_urlopen: Script) -> None:
    outcomes, asked, sleeps = fake_urlopen
    outcomes.extend([ValueError("unknown url type"), b"never served"])
    with pytest.raises(ValueError):
        _read()
    assert len(asked) == 1
    assert sleeps == []


def test_each_retry_is_logged(
    fake_urlopen: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcomes, _, _ = fake_urlopen
    logged: list[str] = []
    monkeypatch.setattr(common, "info", logged.append)
    outcomes.extend([_http_error(503), b"BODY"])
    _read()
    [line] = logged
    assert _URL in line
    assert "HTTP 503" in line
    assert "retry 1 of 3" in line
