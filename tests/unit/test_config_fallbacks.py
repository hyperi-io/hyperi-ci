# Project:   HyperI CI
# File:      tests/unit/test_config_fallbacks.py
# Purpose:   defaults.yaml is the only copy of a shipped config default
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

"""Gate: no source file carries its own copy of a value defaults.yaml ships.

``config.get("test.min_coverage", 0)`` beside a shipped ``80`` is two answers
to one question. Production always loads defaults.yaml, so the literal is dead
there, and alive only in a hand-built config -- which is where a test reads it
and pins the wrong value. ``CIConfig.setting`` reads the shipped value instead.
"""

import ast
from pathlib import Path

import pytest

from hyperi_ci.config import CIConfig, packaged_default, shipped_default

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src" / "hyperi_ci"

# Fallbacks in files other open work owns, each to move to `setting` when that
# work lands. An entry whose fallback is gone fails `test_no_stale_exemption`.
_EXEMPT: dict[tuple[str, str], str] = {
    ("config.py", "language"): "the loader's hardcoded last layer",
    ("config.py", "project"): "the loader, reading the dict it is building",
    ("release/assemble.py", "release.helm.enabled"): "open chart-assemble work",
    ("release/charts.py", "release.helm.enabled"): "open chart-assemble work",
    ("quality/docs_touched.py", "quality.docs_touched"): "open gate work",
    ("quality/render.py", "iac.helm.values"): "open gate work",
    ("quality/render.py", "iac.helm.set"): "open gate work",
    ("quality/repo_advisor.py", "quality.alint"): "open gate work",
}

_MISSING = object()


def _calls(method: str) -> list[tuple[str, str, ast.Call]]:
    """Every ``<x>.<method>("<literal>", ...)`` in the package source."""
    found: list[tuple[str, str, ast.Call]] = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == method
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                continue
            found.append((rel, node.args[0].value, node))
    return found


def _is_config_fallback(key: str, call: ast.Call) -> bool:
    """A ``.get`` on a declared key that supplies its own default."""
    if len(call.args) < 2 and not any(k.arg == "default" for k in call.keywords):
        return False
    func = call.func
    receiver = ast.unparse(func.value) if isinstance(func, ast.Attribute) else ""
    looks_like_config = "." in key or receiver == "config"
    return looks_like_config and packaged_default(key, _MISSING) is not _MISSING


def _fallbacks() -> set[tuple[str, str]]:
    return {
        (rel, key) for rel, key, call in _calls("get") if _is_config_fallback(key, call)
    }


class TestNoInlineCopies:
    def test_no_fallback_duplicates_a_shipped_default(self) -> None:
        offenders = sorted(_fallbacks() - set(_EXEMPT))
        assert offenders == [], (
            "these read a key defaults.yaml declares with their own default -- "
            "use config.setting(key): " + ", ".join(f"{r}: {k}" for r, k in offenders)
        )

    def test_no_stale_exemption(self) -> None:
        stale = sorted(set(_EXEMPT) - _fallbacks())
        assert stale == [], f"exempt fallback no longer exists, drop it: {stale}"

    def test_every_literal_setting_key_is_declared(self) -> None:
        """The static half of `setting`'s KeyError, so it fails before a run."""
        undeclared = sorted(
            f"{rel}: {key}"
            for rel, key, _ in _calls("setting")
            if packaged_default(key, _MISSING) is _MISSING
        )
        assert undeclared == []


class TestTheGateCatchesThings:
    """A gate that cannot fail is not a gate."""

    @staticmethod
    def _call(source: str) -> tuple[str, ast.Call]:
        node = ast.parse(source, mode="eval").body
        assert isinstance(node, ast.Call)
        key = node.args[0]
        assert isinstance(key, ast.Constant) and isinstance(key.value, str)
        return key.value, node

    def test_a_disagreeing_literal_is_caught(self) -> None:
        assert _is_config_fallback(*self._call('config.get("test.min_coverage", 0)'))

    def test_an_agreeing_literal_is_caught_too(self) -> None:
        assert _is_config_fallback(*self._call('config.get("test.enabled", True)'))

    def test_a_keyword_default_is_caught(self) -> None:
        call = 'cfg.get("release.enabled", default=False)'
        assert _is_config_fallback(*self._call(call))

    def test_an_undeclared_key_is_left_alone(self) -> None:
        assert not _is_config_fallback(
            *self._call('config.get("release.no_such_key", "x")')
        )

    def test_a_manifest_dict_read_is_left_alone(self) -> None:
        """pyproject's own `description` is not the config key."""
        assert not _is_config_fallback(*self._call('project.get("description", "")'))


class TestSetting:
    def test_the_merged_value_wins(self) -> None:
        config = CIConfig(_raw={"test": {"min_coverage": 55}})
        assert config.setting("test.min_coverage") == 55

    def test_an_explicit_falsy_value_wins(self) -> None:
        config = CIConfig(_raw={"release": {"enabled": False}})
        assert config.setting("release.enabled") is False

    def test_an_unset_key_takes_the_shipped_value(self) -> None:
        assert CIConfig(_raw={}).setting("test.min_coverage") == 80

    def test_a_scalar_parent_takes_the_shipped_leaf(self) -> None:
        config = CIConfig(_raw={"quality": {"rust": {"feature_matrix": False}}})
        assert config.setting("quality.rust.feature_matrix.enabled") is True

    def test_a_legacy_spelling_resolves(self) -> None:
        config = CIConfig(_raw={"publish": {"enabled": False}})
        assert config.setting("release.enabled") is False

    def test_an_undeclared_key_fails_loudly(self) -> None:
        with pytest.raises(KeyError, match="defaults.yaml"):
            CIConfig(_raw={"release": {"no_such_key": "x"}}).setting(
                "release.no_such_key"
            )

    def test_the_shipped_value_is_a_copy(self) -> None:
        shipped_default("build.golang.targets").append("plan9/amd64")
        assert "plan9/amd64" not in CIConfig(_raw={}).setting("build.golang.targets")
