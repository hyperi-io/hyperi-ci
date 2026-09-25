# Project:   HyperI CI
# File:      src/hyperi_ci/languages/python/pytest_args.py
# Purpose:   Read the pytest arguments a project already passes itself
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""The pytest arguments a project already passes itself.

pytest reads ``addopts`` from one configuration file, then ``PYTEST_ADDOPTS``,
then the command line, and for a single-valued option the last value wins.
hyperi-ci reads the same sources to leave alone an option the project has
already set, and to extend one rather than replace it.
"""

import configparser
import os
import shlex
import tomllib
from pathlib import Path

# pytest's own search order. The first file holding pytest configuration is
# the only one it reads, so a later file's addopts never apply.
_CONFIG_FILES = (
    "pytest.toml",
    ".pytest.toml",
    "pytest.ini",
    ".pytest.ini",
    "pyproject.toml",
    "tox.ini",
    "setup.cfg",
)

_INI_SECTION = {
    "pytest.ini": "pytest",
    ".pytest.ini": "pytest",
    "tox.ini": "pytest",
    "setup.cfg": "tool:pytest",
}

# pytest treats these as its configuration file even when they set nothing.
_ALWAYS_CONFIG = {"pytest.toml", ".pytest.toml", "pytest.ini", ".pytest.ini"}


def _split(raw: object) -> list[str]:
    """Split an ``addopts`` value, a list or a shell-quoted string."""
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if not isinstance(raw, str):
        return []
    try:
        return shlex.split(raw)
    except ValueError:
        return raw.split()


def _toml_addopts(path: Path) -> list[str] | None:
    """Return ``addopts`` from a TOML file, None when it holds no pytest config."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    if path.name in _ALWAYS_CONFIG:
        table = data.get("pytest")
        return _split(table.get("addopts")) if isinstance(table, dict) else []
    tool = data.get("tool", {})
    pytest_table = tool.get("pytest") if isinstance(tool, dict) else None
    if not isinstance(pytest_table, dict):
        return None
    native = {k: v for k, v in pytest_table.items() if k != "ini_options"}
    if native:
        return _split(native.get("addopts"))
    ini_options = pytest_table.get("ini_options")
    if isinstance(ini_options, dict):
        return _split(ini_options.get("addopts"))
    return None


def _ini_addopts(path: Path) -> list[str] | None:
    """Return ``addopts`` from an ini-style file, None when it holds no pytest config."""
    section = _INI_SECTION[path.name]
    # pytest does no %-interpolation, so a literal % in addopts must survive.
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error):
        return None
    if parser.has_section(section):
        return _split(parser.get(section, "addopts", fallback=""))
    return [] if path.name in _ALWAYS_CONFIG else None


def config_file_addopts(root: Path) -> list[str]:
    """Return the ``addopts`` of the configuration file pytest would read.

    Args:
        root: The project directory pytest runs from.

    Returns:
        Split arguments, empty when no file sets any.

    """
    for name in _CONFIG_FILES:
        path = root / name
        if not path.is_file():
            continue
        if path.suffix == ".toml":
            found = _toml_addopts(path)
        else:
            found = _ini_addopts(path)
        if found is not None:
            return found
    return []


def project_args(args: list[str], root: Path | None = None) -> list[str]:
    """Return every argument pytest will see before hyperi-ci adds its own.

    Args:
        args: The arguments hyperi-ci has assembled from the project's config.
        root: Project directory; defaults to the working directory.

    Returns:
        The config file's ``addopts``, then ``PYTEST_ADDOPTS``, then ``args``,
        the order pytest applies them in.

    """
    base = root if root is not None else Path.cwd()
    env = _split(os.environ.get("PYTEST_ADDOPTS", ""))
    return [*config_file_addopts(base), *env, *args]


def option_values(tokens: list[str], long: str, short: str | None = None) -> list[str]:
    """Return every value given for an option, in order.

    Args:
        tokens: Split pytest arguments.
        long: The long spelling, e.g. ``--durations``.
        short: The short spelling, e.g. ``-r``, which also takes an attached
            value (``-rfE``).

    Returns:
        The values, the last being the one pytest uses.

    """
    values: list[str] = []
    for index, token in enumerate(tokens):
        if token in (long, short):
            if index + 1 < len(tokens):
                values.append(tokens[index + 1])
        elif token.startswith(f"{long}="):
            values.append(token[len(long) + 1 :])
        elif short and token.startswith(short) and not token.startswith("--"):
            values.append(token[len(short) :])
    return values
