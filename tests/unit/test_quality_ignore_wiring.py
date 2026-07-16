# Project:   HyperI CI
# File:      tests/unit/test_quality_ignore_wiring.py
# Purpose:   Per-language wiring tests for the generic quality.ignore list
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Per-language wiring tests for ``quality.ignore``.

Verifies each language's command builder picks up the right entries
(filtered by tool slug) and translates ``id`` into the tool's native
CLI flag.
"""

from __future__ import annotations

from hyperi_ci.languages.python.quality import _build_pip_audit_cmd
from hyperi_ci.quality.ignores import IgnoreEntry


class TestPipAuditCommand:
    """pip-audit translates each ignore entry to --ignore-vuln <id>."""

    def test_no_ignores_yields_bare_command(self) -> None:
        cmd = _build_pip_audit_cmd([])
        assert "--ignore-vuln" not in cmd
        assert "pip-audit" in cmd

    def test_one_ignore_added(self) -> None:
        cmd = _build_pip_audit_cmd(
            [IgnoreEntry("pip-audit", "PYSEC-2025-183", "Disputed")]
        )
        assert cmd.count("--ignore-vuln") == 1
        idx = cmd.index("--ignore-vuln")
        assert cmd[idx + 1] == "PYSEC-2025-183"

    def test_multiple_ignores_each_get_their_own_flag(self) -> None:
        cmd = _build_pip_audit_cmd(
            [
                IgnoreEntry("pip-audit", "PYSEC-A", "r1"),
                IgnoreEntry("pip-audit", "PYSEC-B", "r2"),
            ]
        )
        assert cmd.count("--ignore-vuln") == 2
        # Both ids appear after their respective flags
        assert "PYSEC-A" in cmd
        assert "PYSEC-B" in cmd
