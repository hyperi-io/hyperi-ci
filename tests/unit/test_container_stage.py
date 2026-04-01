# Project:   HyperI CI
# File:      tests/unit/test_container_stage.py
# Purpose:   Tests for container stage mode detection
#
# License:   FSL-1.1-ALv2
# Copyright: (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from hyperi_ci.container.stage import detect_mode


def test_detect_mode_explicit_contract():
    config = {"container": {"mode": "contract"}}
    assert detect_mode(config, language="rust") == "contract"


def test_detect_mode_explicit_template():
    config = {"container": {"mode": "template"}}
    assert detect_mode(config, language="python") == "template"


def test_detect_mode_explicit_custom():
    config = {"container": {"mode": "custom"}}
    assert detect_mode(config, language="rust") == "custom"


def test_detect_mode_auto_python():
    config = {"container": {}}
    assert detect_mode(config, language="python") == "template"


def test_detect_mode_auto_typescript():
    config = {"container": {}}
    assert detect_mode(config, language="typescript") == "template"


def test_detect_mode_auto_rust():
    config = {"container": {}}
    assert detect_mode(config, language="rust") == "contract"


def test_detect_mode_auto_golang():
    config = {"container": {}}
    assert detect_mode(config, language="golang") == "custom"


def test_detect_mode_empty_string_is_auto():
    config = {"container": {"mode": ""}}
    assert detect_mode(config, language="python") == "template"


def test_detect_mode_no_container_section():
    config = {}
    assert detect_mode(config, language="rust") == "contract"
