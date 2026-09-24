# Project:   HyperI CI
# File:      tests/unit/test_log_scrub.py
# Purpose:   The log scrubber keeps our own prose and still masks real secrets
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""The log scrubber keeps our own prose and still masks real secrets (issue #255).

gitleaks' generic-api-key rule matched log prose containing the word "keys" and
ate the part of our deprecation warning that names the key to rename. The
scrubber drops that one rule and nothing else.
"""

import dataclasses

from scalo.logger.scrub import ScrubConfig, build_scrubber

from hyperi_ci.common import SCRUB_CONFIG

_WARNING = (
    "Renamed config keys in .hyperi-ci.yaml: publish.binaries -> release.binaries"
)


def test_our_config_warning_survives_the_scrubber() -> None:
    assert build_scrubber(SCRUB_CONFIG).scrub(_WARNING) == _WARNING


def test_a_real_token_is_still_masked() -> None:
    # Built at runtime so the repo's own secret scan does not read it as a leak.
    token = "ghp_" + "Xy7Qa9Lm2Bn4" * 3
    scrubbed = build_scrubber(SCRUB_CONFIG).scrub(f"pushed with {token} by mistake")
    assert token not in scrubbed


def test_nothing_but_the_one_rule_differs_from_the_default() -> None:
    # patterns="minimal" also drops generic-api-key, but it drops the PyPI,
    # npm and Cloudflare rules with it, which are the credentials this tool
    # handles.
    assert SCRUB_CONFIG.secrets.exclude_rules == frozenset({"generic-api-key"})
    unexcluded = dataclasses.replace(
        SCRUB_CONFIG,
        secrets=dataclasses.replace(SCRUB_CONFIG.secrets, exclude_rules=frozenset()),
    )
    assert unexcluded == ScrubConfig()
