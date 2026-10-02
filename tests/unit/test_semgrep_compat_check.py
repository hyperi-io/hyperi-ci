# Project:   HyperI CI
# File:      tests/unit/test_semgrep_compat_check.py
# Purpose:   Tests for the semgrep compatibility-rule drift check
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Semgrep compatibility-rule drift check tests.

Three outcomes, not two: matches, drifted, and could-not-ask. Collapsing the
third would report every rule as removed on a network blip.
"""

import importlib.util
from pathlib import Path

import pytest
import yaml

from hyperi_ci.quality.semgrep import PYTHON_COMPAT_RULES

_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "check_semgrep_compat_rules", _ROOT / "scripts" / "check-semgrep-compat-rules.py"
)
assert _SPEC is not None and _SPEC.loader is not None  # a real file always resolves
check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check)


def _pack(ids: list[str]) -> str:
    """A registry pack in the shape semgrep.dev serves: `rules:` then a list."""
    return yaml.safe_dump(
        {
            "rules": [
                {"id": rid, "languages": ["python"], "severity": "INFO"} for rid in ids
            ]
        }
    )


class TestPackParsing:
    def test_reads_ids_from_a_rules_mapping(self) -> None:
        assert check.pack_rule_ids(_pack(["a.python37.b"])) == {"a.python37.b"}

    def test_reads_ids_from_a_bare_list(self) -> None:
        assert check.pack_rule_ids(yaml.safe_dump([{"id": "x"}])) == {"x"}

    def test_a_non_pack_is_none(self) -> None:
        assert check.pack_rule_ids("<html>rate limited</html>") is None

    def test_target_version_comes_from_the_id(self) -> None:
        rid = "python.lang.compatibility.python37.python37-compatibility-pdb"
        assert check.target_version(rid) == "3.7"
        assert check.target_version("python.lang.compatibility.python310.x") == "3.10"
        assert check.target_version("python.lang.security.audit.eval") is None


class TestTheCheckHasThreeOutcomes:
    def test_the_shipped_table_labels_every_id_with_its_own_version(self) -> None:
        for rid, version in PYTHON_COMPAT_RULES.items():
            assert check.target_version(rid) == version, rid

    def test_a_matching_pack_passes(self) -> None:
        assert check.main(lambda: _pack(list(PYTHON_COMPAT_RULES))) == 0

    def test_a_removed_rule_is_drift(self, capsys: pytest.CaptureFixture[str]) -> None:
        ids = list(PYTHON_COMPAT_RULES)
        assert check.main(lambda: _pack(ids[1:])) == 1
        assert ids[0] in capsys.readouterr().out

    def test_a_new_rule_is_drift(self, capsys: pytest.CaptureFixture[str]) -> None:
        added = "python.lang.compatibility.python38.python38-compatibility-walrus"
        assert check.main(lambda: _pack([*PYTHON_COMPAT_RULES, added])) == 1
        assert f"{added} (targets 3.8)" in capsys.readouterr().out

    def test_an_unreachable_registry_is_neither(self) -> None:
        assert check.main(lambda: None) == 2

    def test_an_empty_or_garbled_pack_is_not_a_clean_result(self) -> None:
        assert check.main(lambda: _pack([])) == 2
        assert check.main(lambda: "<html>oops</html>") == 2
