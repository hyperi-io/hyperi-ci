# Project:   HyperI CI
# File:      src/hyperi_ci/languages/python/pytest_args.py
# Purpose:   Read the pytest arguments and ini settings a project already sets
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""The pytest arguments and ini settings a project already sets itself.

pytest reads ``addopts`` and every other ini key from one configuration
file, then ``PYTEST_ADDOPTS``, then the command line, and for a
single-valued option the last value wins. hyperi-ci reads the same sources
to leave alone a setting the project has already made, and to extend an
``addopts``-style one rather than replace it.
"""

import configparser
import os
import shlex
import tomllib
from pathlib import Path

# pytest's own search order. The first file holding pytest configuration is
# the only one it reads, so a later file's settings never apply.
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


def _toml_table(path: Path) -> dict[str, object] | None:
    """Return the pytest options table a TOML file holds.

    None when the file holds no pytest config pytest itself would read, which
    tells the caller to keep searching.
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    if path.name in _ALWAYS_CONFIG:
        table = data.get("pytest")
        return table if isinstance(table, dict) else {}
    tool = data.get("tool", {})
    pytest_table = tool.get("pytest") if isinstance(tool, dict) else None
    if not isinstance(pytest_table, dict):
        return None
    native = {k: v for k, v in pytest_table.items() if k != "ini_options"}
    if native:
        return native
    ini_options = pytest_table.get("ini_options")
    return ini_options if isinstance(ini_options, dict) else None


def _ini_table(path: Path) -> dict[str, object] | None:
    """Return the pytest section an ini-style file holds, same None rule as above."""
    section = _INI_SECTION[path.name]
    # pytest does no %-interpolation, so a literal % in a value must survive.
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error):
        return None
    if parser.has_section(section):
        return dict(parser.items(section))
    return {} if path.name in _ALWAYS_CONFIG else None


def _config_table(root: Path) -> dict[str, object] | None:
    """Return the key/value table of the configuration file pytest would read.

    None when no file in the search order holds a pytest section.
    """
    for name in _CONFIG_FILES:
        path = root / name
        if not path.is_file():
            continue
        table = _toml_table(path) if path.suffix == ".toml" else _ini_table(path)
        if table is not None:
            return table
    return None


def config_file_addopts(root: Path) -> list[str]:
    """Return the ``addopts`` of the configuration file pytest would read.

    Args:
        root: The project directory pytest runs from.

    Returns:
        Split arguments, empty when no file sets any.

    """
    table = _config_table(root)
    return _split((table or {}).get("addopts"))


def ini_value(root: Path, key: str) -> str | None:
    """Return one ini key from the configuration file pytest would read.

    Args:
        root: The project directory pytest runs from.
        key: The ini key to read, e.g. ``tmp_path_retention_policy``.

    Returns:
        The value as a string (TOML's own type, stringified, for
        pyproject.toml / pytest.toml), None when the file sets nothing for it.

    """
    table = _config_table(root)
    if not table:
        return None
    value = table.get(key)
    return None if value is None else str(value)


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


def _attached_value(token: str, name: str) -> str | None:
    """Return the value joined to an option in one token, None when not joined."""
    if name.startswith("--"):
        return token[len(name) + 1 :] if token.startswith(f"{name}=") else None
    if token.startswith(name) and not token.startswith("--") and token != name:
        return token[len(name) :]
    return None


def option_values(tokens: list[str], *names: str) -> list[str]:
    """Return every value given for an option, in order.

    Args:
        tokens: Split pytest arguments.
        *names: The option's spellings. A long one (``--durations``) also
            takes ``=value``, a short one (``-r``) an attached value (``-rfE``).

    Returns:
        The values, the last being the one pytest uses.

    """
    values: list[str] = []
    for index, token in enumerate(tokens):
        if token in names:
            if index + 1 < len(tokens):
                values.append(tokens[index + 1])
            continue
        for name in names:
            attached = _attached_value(token, name)
            if attached is not None:
                values.append(attached)
                break
    return values
