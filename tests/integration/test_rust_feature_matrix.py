# Project:   HyperI CI
# File:      tests/integration/test_rust_feature_matrix.py
# Purpose:   The feature matrix against real cargo-hack: what it catches, what it writes
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Run the feature matrix with real cargo and cargo-hack on a two-crate fixture.

The fixture's library uses a function that only a feature of ``shared`` turns
on, and only its dev-dependency on ``shared`` enables that feature: the bug the
matrix exists to catch, masked wherever dev-dependency features leak in.

    uv run pytest tests/integration/test_rust_feature_matrix.py -m slow
"""

import shutil
from pathlib import Path

import pytest

from hyperi_ci.common import run_cmd
from hyperi_ci.config import CIConfig
from hyperi_ci.languages.rust import quality as rust_quality

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        shutil.which("cargo") is None or shutil.which("cargo-hack") is None,
        reason="cargo and cargo-hack not installed",
    ),
]

_SHARED = """\
[package]
name = "shared"
version = "0.1.0"
edition = "2021"

[features]
extra = []
"""

_SHARED_LIB = """\
pub fn base() -> u32 {
    0
}

#[cfg(feature = "extra")]
pub fn extra() -> u32 {
    1
}
"""

_APP = """\
[package]
name = "app"
version = "0.1.0"
edition = "{edition}"

[workspace]

[features]
x = []

[dependencies]
shared = {{ path = "../shared" }}

[dev-dependencies]
shared = {{ path = "../shared", features = ["extra"] }}
"""

_APP_LIB = """\
pub fn base() -> u32 {
    shared::base()
}

#[cfg(feature = "x")]
pub fn uses_extra() -> u32 {
    shared::extra()
}
"""


def _fixture(root: Path, edition: str) -> Path:
    """Write ``shared`` and ``app`` under ``root``; return the app directory."""
    for name, manifest, lib in (
        ("shared", _SHARED, _SHARED_LIB),
        ("app", _APP.format(edition=edition), _APP_LIB),
    ):
        (root / name / "src").mkdir(parents=True)
        (root / name / "Cargo.toml").write_text(manifest, encoding="utf-8")
        (root / name / "src" / "lib.rs").write_text(lib, encoding="utf-8")
    app = root / "app"
    run_cmd(["cargo", "generate-lockfile"], check=True, capture=True, cwd=app)
    return app


def _state(app: Path) -> list[tuple[bytes, int]]:
    """Return the bytes and mtime of the manifest and the lockfile."""
    files = (app / "Cargo.toml", app / "Cargo.lock")
    return [(f.read_bytes(), f.stat().st_mtime_ns) for f in files]


@pytest.fixture
def announced(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    messages: list[str] = []
    monkeypatch.setattr(rust_quality, "_ensure_cargo_hack", lambda: True)
    monkeypatch.setattr(
        rust_quality, "announce", lambda msg, _title: messages.append(msg)
    )
    return messages


@pytest.mark.parametrize("edition", ["2021", "2024"])
def test_resolver_2_catches_the_bug_without_touching_the_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    announced: list[str],
    edition: str,
) -> None:
    app = _fixture(tmp_path, edition)
    before = _state(app)
    monkeypatch.chdir(app)

    assert rust_quality._run_feature_matrix(CIConfig(_raw={})) is False

    assert _state(app) == before
    assert announced == []


def test_resolver_1_misses_the_bug_and_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, announced: list[str]
) -> None:
    app = _fixture(tmp_path, "2018")
    before = _state(app)
    monkeypatch.chdir(app)

    assert rust_quality._run_feature_matrix(CIConfig(_raw={})) is True

    assert _state(app) == before
    assert len(announced) == 1
    assert "resolver 1" in announced[0]
