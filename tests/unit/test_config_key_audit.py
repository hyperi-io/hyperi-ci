# Project:   HyperI CI
# File:      tests/unit/test_config_key_audit.py
# Purpose:   A config key that does nothing must fail the build
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

"""Gate: every key in defaults.yaml is read by something.

Twenty-nine keys were retired after an audit found them documented, scaffolded
and read by nothing. The gate stops the next one accumulating; without it the
only signal is someone setting a key and wondering why nothing changed.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "audit-config-keys.py"


@pytest.fixture(scope="module")
def audit_module():
    """The script, loaded by path -- its name is not a valid module name."""
    spec = importlib.util.spec_from_file_location("audit_config_keys", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["audit_config_keys"] = module
    spec.loader.exec_module(module)
    return module


class TestNoUnreadKeys:
    def test_every_declared_key_has_a_reader(self, audit_module) -> None:
        unread, _, _ = audit_module.audit()
        assert unread == [], (
            "these keys are declared in defaults.yaml and read by nothing — "
            "delete them, or declare the dynamic reader in "
            "config/dynamic-config-keys.yaml"
        )


class TestAllowlistIntegrity:
    """The allowlist is an escape hatch, so it needs its own guards."""

    def test_no_entry_names_a_missing_reader(self, audit_module) -> None:
        _, stale, _ = audit_module.audit()
        assert stale == [], "allowlist entry outlived the file that reads the key"

    def test_no_entry_is_redundant(self, audit_module) -> None:
        """An entry the audit would pass anyway hides a key from real scrutiny."""
        _, _, unused = audit_module.audit()
        assert unused == []

    def test_every_entry_names_a_path_inside_the_repo(self) -> None:
        data = yaml.safe_load(
            (_ROOT / "config" / "dynamic-config-keys.yaml").read_text(encoding="utf-8")
        )
        for key, reader in data["keys"].items():
            assert not reader.startswith("/"), f"{key}: reader must be repo-relative"
            assert ".." not in reader, f"{key}: reader must not escape the repo"


class TestTheGateActuallyCatchesThings:
    """A gate that cannot fail is not a gate."""

    def test_a_key_with_no_reader_is_reported(self, audit_module) -> None:
        assert not audit_module._has_reader(
            "nonsense.invented.key", 'config.get("something.else")'
        )

    def test_a_comment_does_not_count_as_a_reader(self, audit_module) -> None:
        """The first false-positive class this audit hit."""
        source = audit_module._index([_ROOT / "src" / "hyperi_ci"], {".py"})
        assert "# " not in "\n".join(
            line for line in source.splitlines() if line.strip().startswith("#")
        )

    def test_a_single_segment_ancestor_does_not_credit_a_child(
        self, audit_module
    ) -> None:
        """`"test"` appears everywhere as a stage name; it is not a reader."""
        assert not audit_module._has_reader("test.enabled", 'stage == "test"')

    def test_a_quoted_full_path_counts(self, audit_module) -> None:
        assert audit_module._has_reader(
            "test.enabled", 'config.get("test.enabled", True)'
        )

    def test_a_multi_segment_ancestor_counts(self, audit_module) -> None:
        assert audit_module._has_reader(
            "build.rust.targets", 'config.get("build.rust")'
        )

    def test_a_sibling_key_does_not_credit_the_ancestor(self, audit_module) -> None:
        """How ten dead `test.*` keys passed: `"test.typescript.runner"` was
        read, so `test.typescript.e2e.browser` counted as read too."""
        assert not audit_module._has_reader(
            "test.typescript.e2e.browser",
            'config.get("test.typescript.runner", "auto")',
        )

    def test_an_fstring_child_path_does_not_credit_every_child(
        self, audit_module
    ) -> None:
        assert not audit_module._has_reader(
            "test.tiers.unit.timeout", 'config.get(f"test.tiers.{name}", {})'
        )


class TestPendingKeys:
    """A key declared ahead of its reader is named, never passed off as read."""

    def test_pending_keys_are_not_reported_unread(self, audit_module) -> None:
        unread, _, _ = audit_module.audit()
        assert not set(audit_module.load_pending()) & set(unread)

    def test_every_pending_key_is_declared_and_has_no_reader(
        self, audit_module
    ) -> None:
        """Once the reader lands, the entry shows up as redundant and fails."""
        _, _, unused = audit_module.audit()
        assert not set(audit_module.load_pending()) & set(unused)

    def test_the_run_names_what_is_pending(
        self, audit_module, capsys: pytest.CaptureFixture[str], monkeypatch
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["audit-config-keys.py"])
        assert audit_module.main() == 0
        out = capsys.readouterr().out
        for key in audit_module.load_pending():
            assert f"Pending, no reader yet: {key}" in out
