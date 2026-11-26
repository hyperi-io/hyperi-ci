# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/bindings.py
# Purpose:   Test, build, stamp and publish a Rust repo's maturin Python bindings
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Python bindings of a Rust crate, built by maturin (PyO3).

A binding is a directory one level below the repo root holding a
``pyproject.toml`` whose build backend is maturin. ``build.python_bindings``
in ``.hyperi-ci.yaml`` turns them off (``enabled: false``), names the
directories (``paths``) and opts in to the PyPI upload (``publish: true``).

Wheels land in ``dist/wheels/``. The binary publisher reads only the top level
of ``dist/``, so a wheel never reaches GitHub Releases or R2. A Linux leg
builds a manylinux and a musllinux wheel for its own arch with ``--zig``.
Windows and macOS legs build natively.
"""

import re
import tempfile
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from packaging.version import InvalidVersion, Version

from hyperi_ci.common import error, group, info, run_cmd, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.stamp import StampError, replace_toml_table_version, repo_paths
from hyperi_ci.versions import tool_version

ENABLED_KEY = "build.python_bindings.enabled"
PATHS_KEY = "build.python_bindings.paths"
PUBLISH_KEY = "build.python_bindings.publish"

WHEEL_DIR = Path("dist") / "wheels"

MANYLINUX = "manylinux_2_28"
MUSLLINUX = "musllinux_1_2"

# name-version-[build-]python-abi-platform.whl
_WHEEL_NAME = re.compile(r"^[^-]+-(?P<version>[^-]+)-")


@dataclass(frozen=True, slots=True)
class Binding:
    """One maturin project inside the repo.

    Attributes:
        path: The directory holding its pyproject.toml and Cargo.toml.
        requires_python: ``[project] requires-python``, empty when unset.

    """

    path: Path
    requires_python: str


@dataclass(frozen=True, slots=True)
class WheelSpec:
    """One ``maturin build``: a Rust target, and the Linux tag it must meet.

    Attributes:
        target: Rust target triple.
        compatibility: manylinux or musllinux tag, empty for a native build.

    """

    target: str
    compatibility: str = ""


def _maturin_binding(pyproject: Path) -> Binding | None:
    """Return the binding ``pyproject`` declares, or None unless maturin builds it."""
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    if data.get("build-system", {}).get("build-backend") != "maturin":
        return None
    requires = data.get("project", {}).get("requires-python", "")
    return Binding(pyproject.parent, requires if isinstance(requires, str) else "")


def find_bindings(config: CIConfig, root: Path) -> list[Binding]:
    """Return the repo's maturin bindings, the listed ones or those one level down.

    Args:
        config: Merged CI configuration.
        root: Repo root.

    Returns:
        The bindings, empty when there are none or they are turned off.

    Raises:
        ValueError: ``build.python_bindings.paths`` breaks a
            :func:`~hyperi_ci.stamp.repo_paths` rule, or names a directory
            with no maturin ``pyproject.toml``.

    """
    if config.get(ENABLED_KEY, True) is False:
        return []
    listed = repo_paths(config.get(PATHS_KEY), root, PATHS_KEY)
    if not listed:
        found = (_maturin_binding(p) for p in sorted(root.glob("*/pyproject.toml")))
        return [binding for binding in found if binding]
    bindings = []
    for rel in listed:
        binding = _maturin_binding(root / rel / "pyproject.toml")
        if binding is None:
            raise ValueError(
                f"{PATHS_KEY}: {rel} has no pyproject.toml built by maturin"
            )
        bindings.append(binding)
    return bindings


def wheel_specs(target: str) -> list[WheelSpec]:
    """Return the wheels a build leg for ``target`` produces.

    A Linux gnu leg builds a manylinux wheel and a musllinux one for the same
    arch. Any other target builds one native wheel.
    """
    if target.endswith("-unknown-linux-gnu"):
        musl = target.removesuffix("-gnu") + "-musl"
        return [WheelSpec(target, MANYLINUX), WheelSpec(musl, MUSLLINUX)]
    return [WheelSpec(target)]


def build_argv(
    binding: Binding, spec: WheelSpec, out: Path, *, release: bool
) -> list[str]:
    """Return the ``maturin build`` argv for one wheel, run through the pinned maturin."""
    argv = [
        "uvx",
        "--from",
        f"maturin[zig]=={tool_version('maturin')}",
        "maturin",
        "build",
        "--manifest-path",
        str(binding.path / "Cargo.toml"),
        "--target",
        spec.target,
        "--out",
        str(out),
    ]
    if release:
        argv.append("--release")
    if spec.compatibility:
        argv += ["--zig", "--compatibility", spec.compatibility]
    return argv


def build_wheels(config: CIConfig, targets: Sequence[str], root: Path) -> int:
    """Build every binding's release wheels for the leg's targets into dist/wheels/.

    Args:
        config: Merged CI configuration.
        targets: The Rust targets this leg built.
        root: Repo root.

    Returns:
        Exit code (0 = success, including no bindings).

    """
    try:
        bindings = find_bindings(config, root)
    except ValueError as exc:
        error(str(exc))
        return 1
    if not bindings:
        return 0
    out = root / WHEEL_DIR
    out.mkdir(parents=True, exist_ok=True)
    for binding in bindings:
        for target in targets:
            for spec in wheel_specs(target):
                label = f"{binding.path.name} {spec.target} {spec.compatibility}"
                with group(f"Python wheel: {label.strip()}"):
                    rc = _build_one(binding, spec, out, root, host=target)
                    if rc != 0:
                        error(f"maturin build failed: {label.strip()}")
                        return rc
    built = sorted(p.name for p in out.glob("*.whl"))
    success(f"Built {len(built)} wheel(s): {', '.join(built)}")
    return 0


def _build_one(
    binding: Binding, spec: WheelSpec, out: Path, root: Path, *, host: str
) -> int:
    """Build one wheel, adding the rustup target first when it is not the host's."""
    if spec.target != host:
        added = run_cmd(["rustup", "target", "add", spec.target], check=False)
        if added.returncode != 0:
            return added.returncode
    argv = build_argv(binding, spec, out, release=True)
    return run_cmd(argv, check=False, cwd=root).returncode


def test_bindings(config: CIConfig, root: Path, target: str) -> int:
    """Build each binding's debug wheel and run its ``tests/`` against that wheel.

    Args:
        config: Merged CI configuration.
        root: Repo root.
        target: This host's Rust target.

    Returns:
        Exit code (0 = success, including no bindings).

    """
    try:
        bindings = find_bindings(config, root)
    except ValueError as exc:
        error(str(exc))
        return 1
    for binding in bindings:
        with group(f"Python binding tests: {binding.path.name}"):
            rc = _test_one(binding, root, target)
            if rc != 0:
                error(f"Python binding tests failed: {binding.path.name}")
                return rc
    return 0


def _test_one(binding: Binding, root: Path, target: str) -> int:
    """Build one binding's debug wheel into a temp dir and pytest its tests/."""
    tests = binding.path / "tests"
    if not tests.is_dir():
        warn(f"{binding.path.name}: no tests/ directory, nothing to run")
        return 0
    with tempfile.TemporaryDirectory(prefix="hyperi-ci-wheel-") as out:
        argv = build_argv(binding, WheelSpec(target), Path(out), release=False)
        rc = run_cmd(argv, check=False, cwd=root).returncode
        if rc != 0:
            return rc
        wheels = sorted(Path(out).glob("*.whl"))
        if len(wheels) != 1:
            error(f"expected one wheel from maturin, found {len(wheels)}")
            return 1
        python = (
            ["--python", binding.requires_python] if binding.requires_python else []
        )
        argv = [
            "uv",
            "run",
            "--isolated",
            "--no-project",
            *python,
            "--with",
            "pytest",
            "--with",
            str(wheels[0]),
            "pytest",
            str(tests),
        ]
        rc = run_cmd(argv, check=False, cwd=root).returncode
    if rc == 0:
        success(f"Python binding tests passed: {binding.path.name}")
    return rc


def stamp(config: CIConfig, root: Path, version: str) -> None:
    """Stamp ``version`` into each binding's Cargo.toml and any static pyproject version.

    A binding's Cargo.toml sits outside the root workspace, so the root stamp
    never reaches it, and maturin takes the wheel version from it.

    Raises:
        StampError: The bindings list is unreadable, or a version survives the stamp.

    """
    try:
        for binding in find_bindings(config, root):
            for name, table in (
                ("Cargo.toml", "package"),
                ("pyproject.toml", "project"),
            ):
                manifest = binding.path / name
                if not manifest.is_file():
                    continue
                text = manifest.read_text(encoding="utf-8")
                stamped = replace_toml_table_version(text, table, version)
                if stamped != text:
                    manifest.write_text(stamped, encoding="utf-8", newline="\n")
                    info(f"Stamped {manifest.relative_to(root).as_posix()}: {version}")
    except ValueError as exc:
        raise StampError(str(exc)) from exc


def wheel_version(name: str) -> str:
    """Return the version in a wheel filename, or "" when it has none."""
    match = _WHEEL_NAME.match(name)
    return match["version"] if match else ""


def carries_version(name: str, version: str) -> bool:
    """Say whether a wheel filename holds ``version``, a Cargo prerelease included.

    maturin writes the PEP 440 form, so ``1.2.0-beta.1`` arrives as ``1.2.0b1``.
    """
    try:
        return Version(wheel_version(name)) == Version(version)
    except InvalidVersion:
        return False


def publish(config: CIConfig, root: Path, version: str) -> int:
    """Upload dist/wheels/ to PyPI when ``build.python_bindings.publish`` is true.

    Every wheel must carry ``version``, so a stale or unstamped wheel never
    ships under a release.

    Args:
        config: Merged CI configuration.
        root: Repo root.
        version: The release version, empty when none resolved.

    Returns:
        Exit code (0 = success, or nothing to do).

    """
    wheels = sorted((root / WHEEL_DIR).glob("*.whl"))
    if not wheels:
        return 0
    if config.get(PUBLISH_KEY, False) is not True:
        info(
            f"{len(wheels)} Python wheel(s) built, not uploaded: {PUBLISH_KEY} is not true"
        )
        return 0
    if not version:
        error("No release version resolved -- refusing to upload Python wheels")
        return 1
    stale = [w.name for w in wheels if not carries_version(w.name, version)]
    if stale:
        error(f"Wheels without version {version}: {', '.join(stale)}")
        return 1
    from hyperi_ci.languages.python.release import publish_pypi

    with group("Publish: PyPI (Python bindings)"):
        return publish_pypi([str(w) for w in wheels])
