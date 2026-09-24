# Project:   HyperI CI
# File:      tests/unit/test_pip_audit_retry.py
# Purpose:   pip-audit retries an unreachable advisory DB, never a finding
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""pip-audit against a flaky advisory DB.

One connection reset from PyPI failed the whole quality gate as though it were
a vulnerability (#325). The outputs below are what pip-audit 2.10.1 printed,
trimmed to the lines that decide the outcome.
"""

import subprocess
import time

import pytest

from hyperi_ci import common
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.python import quality

# The failure from the Quality job of run 36069734752.
_RESET = (
    "Traceback (most recent call last):\n"
    '  File "/x/urllib3/connectionpool.py", line 793, in urlopen\n'
    "ConnectionResetError: [Errno 104] Connection reset by peer\n"
    "\n"
    "During handling of the above exception, another exception occurred:\n"
    "\n"
    "Traceback (most recent call last):\n"
    '  File "/x/requests/adapters.py", line 696, in send\n'
    "urllib3.exceptions.ProtocolError: ('Connection aborted.', "
    "ConnectionResetError(104, 'Connection reset by peer'))\n"
    "\n"
    "During handling of the above exception, another exception occurred:\n"
    "\n"
    "Traceback (most recent call last):\n"
    '  File "/x/pip_audit/_service/pypi.py", line 64, in query\n'
    '  File "/x/requests/adapters.py", line 711, in send\n'
    "    raise ConnectionError(err, request=request)\n"
    "requests.exceptions.ConnectionError: ('Connection aborted.', "
    "ConnectionResetError(104, 'Connection reset by peer'))\n"
)

_FINDING_STDOUT = (
    "Name     Version ID             Fix Versions\n"
    "-------- ------- -------------- ------------\n"
    "requests 2.19.0  PYSEC-2023-74  2.31.0\n"
)
_FINDING_STDERR = "Found 1 known vulnerability in 1 package\n"

_CLEAN_STDERR = "No known vulnerabilities found\n"

_TRACE_TAIL = (
    "Traceback (most recent call last):\n"
    '  File "/x/requests/adapters.py", line 723, in send\n'
    "    raise ProxyError(e, request=request)\n"
)


def _config() -> CIConfig:
    """Every Python tool off except pip-audit, which blocks."""
    off = ["ruff", "ruff_format", "ty", "pyright", "bandit", "ruff_docstrings"]
    python = dict.fromkeys([*off, "vulture"], "disabled")
    python["pip_audit"] = "blocking"
    return CIConfig(_raw={"quality": {"python": python}})


class _Harness:
    """Feed scripted pip-audit results to the quality run and record what it did."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        attempts: list[tuple[int, str, str]],
    ) -> None:
        self.attempts = list(attempts)
        self.audits = 0
        self.sleeps: list[float] = []
        self.said: list[str] = []
        # A strict or skip setting in the caller's shell would change the modes.
        monkeypatch.delenv("HYPERCI_QUALITY_STRICT", raising=False)
        monkeypatch.delenv("HYPERCI_QUALITY_SKIP", raising=False)
        monkeypatch.setattr(subprocess, "run", self._run)
        monkeypatch.setattr(time, "sleep", self.sleeps.append)
        monkeypatch.setattr(quality.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
        for name in ("info", "warn", "error", "success"):
            monkeypatch.setattr(quality, name, self.said.append)

    def _run(self, cmd: list[str], *_args: object, **_kwargs: object):
        if "pip-audit" not in cmd:
            # The ruff version probe, which runs whatever the modes say.
            return subprocess.CompletedProcess(cmd, 0, "ruff 0.16.8\n", "")
        self.audits += 1
        rc, out, err = self.attempts.pop(0)
        return subprocess.CompletedProcess(cmd, rc, out, err)


class TestUnreachableAdvisoryDbIsRetried:
    """A reset says nothing about the dependencies, so it is asked again."""

    def test_a_reset_then_a_clean_scan_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Harness(monkeypatch, [(1, "", _RESET), (0, "", _CLEAN_STDERR)])
        assert quality.run(_config()) == 0
        assert harness.audits == 2
        assert len(harness.sleeps) == 1

    def test_every_attempt_unreachable_still_fails_and_says_so(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A scan that never reached the advisory DB proved nothing."""
        attempts = [(1, "", _RESET)] * common.URL_ATTEMPTS
        harness = _Harness(monkeypatch, attempts)
        assert quality.run(_config()) == 1
        assert harness.audits == common.URL_ATTEMPTS
        assert "  pip-audit: failed" in harness.said
        unreachable = f"advisory DB unreachable after {common.URL_ATTEMPTS} attempts"
        assert sum(unreachable in line for line in harness.said) == 1

    def test_a_warn_tier_pip_audit_still_does_not_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config()
        config._raw["quality"]["python"]["pip_audit"] = {
            "mode": "warn",
            "reason": "advisory feed is flaky on this runner",
        }
        attempts = [(1, "", _RESET)] * common.URL_ATTEMPTS
        harness = _Harness(monkeypatch, attempts)
        assert quality.run(config) == 0
        assert harness.audits == common.URL_ATTEMPTS
        assert any("advisory DB unreachable" in line for line in harness.said)


class TestAFindingIsNeverRetried:
    """Asking again cannot make a vulnerability go away."""

    def test_a_finding_fails_on_the_first_attempt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _Harness(
            monkeypatch, [(1, _FINDING_STDOUT, _FINDING_STDERR), (0, "", "")]
        )
        assert quality.run(_config()) == 1
        assert harness.audits == 1
        assert harness.sleeps == []
        assert "  pip-audit: failed" in harness.said
        assert not any("advisory DB unreachable" in line for line in harness.said)

    def test_a_finding_beside_network_noise_is_still_a_finding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The finding summary wins over any network noise in the same run."""
        harness = _Harness(
            monkeypatch, [(1, _FINDING_STDOUT, _RESET + _FINDING_STDERR)]
        )
        assert quality.run(_config()) == 1
        assert harness.audits == 1


class TestWhatCountsAsUnreachable:
    """Each exception line here is one pip-audit 2.10.1 printed on a failed run."""

    @pytest.mark.parametrize(
        "last_line",
        [
            # Connection reset, the #325 failure.
            "requests.exceptions.ConnectionError: ('Connection aborted.', "
            "ConnectionResetError(104, 'Connection reset by peer'))",
            # Proxy refused the connection.
            "requests.exceptions.ProxyError: HTTPSConnectionPool(host='pypi.org', "
            "port=443): Max retries exceeded with url: /pypi/requests/2.34.2/json "
            "(Caused by ProxyError('Unable to connect to proxy', "
            "NewConnectionError(\"HTTPSConnection(host='127.0.0.1', port=9): "
            "Failed to establish a new connection: [Errno 111] Connection "
            'refused")))',
            # DNS failure.
            "requests.exceptions.ProxyError: HTTPSConnectionPool(host='pypi.org', "
            "port=443): Max retries exceeded with url: /pypi/requests/2.34.2/json "
            "(Caused by ProxyError('Unable to connect to proxy', "
            "NameResolutionError(\"HTTPSConnection(host='no-such-host.invalid', "
            "port=8080): Failed to resolve 'no-such-host.invalid' ([Errno -2] "
            'Name or service not known)")))',
            # Read timeout.
            "requests.exceptions.ReadTimeout: HTTPSConnectionPool(host='pypi.org', "
            "port=443): Read timed out. (read timeout=2)",
            # A 5xx from the index, chained under pip-audit's own ServiceError.
            "requests.exceptions.HTTPError: 503 Server Error: Service Unavailable "
            "for url: https://pypi.org/pypi/requests/2.34.2/json\n"
            "The above exception was the direct cause of the following exception:"
            "\npip_audit._service.interface.ServiceError",
            # A connect timeout, which pip-audit reports itself.
            "ERROR:pip_audit._cli:Could not connect to PyPI's vulnerability feed\n"
            "ERROR:pip_audit._cli:Tip: your network may be blocking this service. "
            "Try another service with `-s SERVICE`",
        ],
        ids=["reset", "refused", "dns", "read-timeout", "5xx", "connect-timeout"],
    )
    def test_connection_failures(self, last_line: str) -> None:
        result = subprocess.CompletedProcess([], 1, "", f"{_TRACE_TAIL}{last_line}\n")
        assert quality._advisory_db_unreachable(result) is True

    def test_a_finding_is_not(self) -> None:
        result = subprocess.CompletedProcess([], 1, _FINDING_STDOUT, _FINDING_STDERR)
        assert quality._advisory_db_unreachable(result) is False

    def test_a_clean_scan_is_not(self) -> None:
        result = subprocess.CompletedProcess([], 0, "", _CLEAN_STDERR)
        assert quality._advisory_db_unreachable(result) is False

    def test_a_4xx_from_the_index_is_not(self) -> None:
        """Asking again gets the same answer."""
        stderr = (
            "requests.exceptions.HTTPError: 403 Client Error: Forbidden for url: "
            "https://pypi.org/pypi/requests/2.34.2/json\n"
            "pip_audit._service.interface.ServiceError\n"
        )
        result = subprocess.CompletedProcess([], 1, "", stderr)
        assert quality._advisory_db_unreachable(result) is False
