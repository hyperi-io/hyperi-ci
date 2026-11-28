# Project:   HyperI CI
# File:      src/hyperi_ci/languages/rust/cpu_floor.py
# Purpose:   Build x86_64 Linux Rust at the fleet's CPU floor
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""x86_64 Linux Rust built at the CPU floor ``build.rust.cpu_floor`` sets.

The flags go on ``CARGO_TARGET_<TRIPLE>_RUSTFLAGS``, joined onto the runner's
value. Cargo places that env value after the repo's ``[target.<triple>]``
rustflags, so the floor's ``target-cpu`` would beat the repo's: a cargo config
naming a different ``target-cpu`` keeps it and that triple gets no floor.
maturin wheels build without it, because no manylinux tag can say x86-64-v3.
Each packaged floor binary is then run under qemu on a CPU either side of it.
"""

import os
import shlex
import shutil
import signal
import subprocess
import sys
import tomllib
from collections.abc import Generator, MutableMapping, Sequence
from contextlib import contextmanager
from pathlib import Path

from hyperi_ci.ci_env import is_ci
from hyperi_ci.common import error, info, run_cmd, warn
from hyperi_ci.config import CIConfig, shipped_default
from hyperi_ci.languages.rust._jobs import cargo_config_files
from hyperi_ci.native_deps import apt_install

_KEY = "build.rust.cpu_floor"
_CHECK_MODES = ("blocking", "warn", "disabled")

# Every scalo::require_cpu_floor!() refusal prints this line, which scalo keeps stable.
_REFUSAL = "This build needs an x86-64-v3 CPU with PCLMULQDQ:"
_QEMU = "qemu-x86_64"
_BELOW_FLOOR = "Westmere"
_AT_FLOOR = "Haswell"
_QEMU_TIMEOUT = 60

# Process-wide while a stage runs: the PGO, BOLT and wheel steps read them.
_ACTIVE: dict[str, str] = {}
_RUNNER_VALUE: dict[str, str | None] = {}


def target_rustflags_key(target: str) -> str:
    """Return cargo's env name for ``target.<triple>.rustflags``."""
    return (
        f"CARGO_TARGET_{target.upper().replace('-', '_').replace('.', '_')}_RUSTFLAGS"
    )


def _join(*parts: str | None) -> str:
    """Join rustflags strings, dropping empty ones."""
    return " ".join(p.strip() for p in parts if p and p.strip())


def target_cpu(flags: Sequence[str]) -> str | None:
    """Return the last ``target-cpu`` in a rustflags list, the one rustc applies."""
    found = None
    for i, token in enumerate(flags):
        value = token.removeprefix("-C").strip()
        if not value and i + 1 < len(flags):
            value = flags[i + 1]
        if value.startswith("target-cpu="):
            found = value.removeprefix("target-cpu=")
    return found


def _rustflags_tokens(value: object) -> list[str]:
    """Return a cargo ``rustflags`` value, string or list, as tokens."""
    if isinstance(value, str):
        return shlex.split(value)
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def config_target_cpu(target: str, start: Path, cargo_home: Path) -> str | None:
    """Return the ``target-cpu`` a cargo config file sets for ``target``, if any.

    Reads ``[target.<triple>]`` and every ``[target.'cfg(...)']`` table: a cfg
    expression is not evaluated here, so one naming a CPU is never overridden.

    Args:
        target: The Rust target triple.
        start: The directory cargo runs in.
        cargo_home: ``$CARGO_HOME``.

    Returns:
        The CPU name, or None when no config file sets one.

    """
    for path in cargo_config_files(start, cargo_home):
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        tables = data.get("target")
        if not isinstance(tables, dict):
            continue
        for name, table in tables.items():
            if name != target and not name.startswith("cfg("):
                continue
            if isinstance(table, dict):
                cpu = target_cpu(_rustflags_tokens(table.get("rustflags")))
                if cpu:
                    return cpu
    return None


def _string_list(config: CIConfig, key: str, what: str) -> list[str]:
    """Return a list-of-strings setting, the shipped value when it is not one."""
    value = config.setting(key)
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        return value
    shipped = shipped_default(key)
    warn(f"  {key}: {value!r} is not a list of {what}, using {shipped}")
    return shipped


def floor_for(
    config: CIConfig, environ: MutableMapping[str, str], cwd: Path
) -> dict[str, str]:
    """Return the floor flags each triple gets, keyed by triple.

    Args:
        config: Merged CI configuration.
        environ: The environment cargo will run in.
        cwd: The directory cargo runs in.

    Returns:
        Empty when the floor is off, or when ``RUSTFLAGS`` or
        ``CARGO_ENCODED_RUSTFLAGS`` is set, since cargo then reads no target
        rustflags at all.

    """
    if config.setting(f"{_KEY}.enabled") is not True:
        info(f"cpu floor: off, {_KEY}.enabled is not true")
        return {}
    for name in ("CARGO_ENCODED_RUSTFLAGS", "RUSTFLAGS"):
        if name in environ:
            warn(
                f"cpu floor: not applied, {name} is set so cargo ignores target rustflags"
            )
            return {}
    flags = _string_list(config, f"{_KEY}.rustflags", "flags")
    floor_cpu = target_cpu(flags)
    cargo_home = Path(environ.get("CARGO_HOME") or Path.home() / ".cargo")
    out: dict[str, str] = {}
    for target in _string_list(config, f"{_KEY}.targets", "triples"):
        own = config_target_cpu(target, cwd, cargo_home)
        if own and floor_cpu and own != floor_cpu:
            info(f"cpu floor: {target} keeps the cargo config's target-cpu={own}")
            continue
        out[target] = shlex.join(flags)
    return out


@contextmanager
def cpu_floor(
    config: CIConfig,
    environ: MutableMapping[str, str] | None = None,
    *,
    cwd: Path | None = None,
) -> Generator[None]:
    """Add the floor to each triple's target rustflags for one Rust stage.

    Args:
        config: Merged CI configuration.
        environ: The environment to set it in, ``os.environ`` if None.
        cwd: The directory cargo runs in, the process cwd if None.

    Yields:
        Nothing. The runner's values are put back on exit.

    """
    target_env = os.environ if environ is None else environ
    floors = floor_for(config, target_env, cwd or Path.cwd())
    for target, flags in floors.items():
        key = target_rustflags_key(target)
        _RUNNER_VALUE[target] = target_env.get(key)
        _ACTIVE[target] = flags
        target_env[key] = _join(target_env.get(key), flags)
        info(f"cpu floor: {target} builds with {flags}")
    try:
        yield
    finally:
        for target in floors:
            _ACTIVE.pop(target, None)
            _restore(target_env, target, _RUNNER_VALUE.pop(target, None))


def _restore(environ: MutableMapping[str, str], target: str, value: str | None) -> None:
    """Set a triple's target rustflags back to ``value``, unset when None."""
    key = target_rustflags_key(target)
    if value is None:
        environ.pop(key, None)
    else:
        environ[key] = value


def with_floor(target: str, rustflags: str) -> str:
    """Return ``rustflags`` plus the floor, for a step that replaces the runner's value."""
    return _join(rustflags, _ACTIVE.get(target))


@contextmanager
def suspended(environ: MutableMapping[str, str] | None = None) -> Generator[None]:
    """Put the runner's target rustflags back while a baseline build runs.

    Args:
        environ: The environment to change, ``os.environ`` if None.

    Yields:
        Nothing. The floor is back on exit.

    """
    target_env = os.environ if environ is None else environ
    saved = {t: target_env.get(target_rustflags_key(t)) for t in _ACTIVE}
    for target in saved:
        _restore(target_env, target, _RUNNER_VALUE.get(target))
    try:
        yield
    finally:
        for target, value in saved.items():
            _restore(target_env, target, value)


def applies(target: str) -> bool:
    """Return True when the running stage builds ``target`` at the floor."""
    return target in _ACTIVE


def check_mode(config: CIConfig) -> str:
    """Return ``build.rust.cpu_floor.check``: blocking, warn or disabled."""
    key = f"{_KEY}.check"
    value = config.setting(key)
    if value in _CHECK_MODES:
        return value
    shipped = shipped_default(key)
    warn(f"  {key}: {value!r} is not one of {', '.join(_CHECK_MODES)}, using {shipped}")
    return shipped


def _qemu() -> str | None:
    """Return the qemu-x86_64 path, installing qemu-user from apt in CI."""
    found = shutil.which(_QEMU)
    if found or not is_ci() or sys.platform != "linux":
        return found
    info("    cpu floor: installing qemu-user")
    if apt_install(["qemu-user"]) != 0:
        return None
    return shutil.which(_QEMU)


def _emulate(
    qemu: str, cpu: str, binary: Path
) -> subprocess.CompletedProcess[str] | None:
    """Run ``binary --version`` on an emulated ``cpu``, None past the timeout."""
    try:
        return run_cmd(
            [qemu, "-cpu", cpu, str(binary), "--version"],
            check=False,
            capture=True,
            timeout=_QEMU_TIMEOUT,
            own_group=True,
        )
    except subprocess.TimeoutExpired:
        return None


def _outcome(result: subprocess.CompletedProcess[str] | None) -> str:
    """Describe how an emulated run ended."""
    if result is None:
        return f"still running after {_QEMU_TIMEOUT}s"
    if result.returncode < 0:
        return f"killed by {signal.Signals(-result.returncode).name}"
    first = (result.stderr or "").strip().splitlines()
    return f"exit {result.returncode}" + (f" ({first[0]})" if first else "")


def _refused(result: subprocess.CompletedProcess[str] | None) -> bool:
    """Return True when the run is scalo's floor refusal."""
    return result is not None and _REFUSAL in (result.stderr or "")


def floor_problems(qemu: str, binary: Path) -> list[str]:
    """Return what is wrong with a floor binary run either side of the floor."""
    problems = []
    below = _emulate(qemu, _BELOW_FLOOR, binary)
    if below is None or below.returncode != 1 or not _refused(below):
        problems.append(
            f"{binary.name} on -cpu {_BELOW_FLOOR}: {_outcome(below)}, expected "
            f'exit 1 with "{_REFUSAL} ...". Add scalo::require_cpu_floor!(); '
            "to its main.rs (scalo 2.14.48 or later)"
        )
    at = _emulate(qemu, _AT_FLOOR, binary)
    if at is not None and (at.returncode == -signal.SIGILL or _refused(at)):
        problems.append(
            f"{binary.name} on -cpu {_AT_FLOOR}: {_outcome(at)}, but a floor CPU "
            "must run it"
        )
    return problems


def check_binary(binary: Path, mode: str) -> bool:
    """Check a floor binary refuses a CPU below the floor and runs on one at it.

    Args:
        binary: The packaged x86_64 binary.
        mode: ``check_mode``'s value.

    Returns:
        False only when the check is blocking and fails. A missing qemu fails
        in CI and is skipped elsewhere.

    """
    if mode == "disabled":
        info(f"    SKIP: CPU floor check ({_KEY}.check: disabled)")
        return True
    qemu = _qemu()
    if qemu is None:
        if not is_ci():
            warn(f"    SKIP: CPU floor check, {_QEMU} is not on PATH")
            return True
        problems = [f"{_QEMU} is not on PATH and qemu-user did not install"]
    else:
        problems = floor_problems(qemu, binary)
    if not problems:
        info(
            f"    OK: CPU floor: refuses -cpu {_BELOW_FLOOR} with a message, "
            f"runs on -cpu {_AT_FLOOR}"
        )
        return True
    report = error if mode == "blocking" else warn
    for problem in problems:
        report(f"    CPU floor: {problem}")
    return mode != "blocking"
