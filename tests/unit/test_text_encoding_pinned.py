# Project:   HyperI CI
# File:      tests/unit/test_text_encoding_pinned.py
# Purpose:   Text-mode subprocess and file calls in src/ and scripts/ pin their encoding
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Guard on the pinned-encoding rule.

A text-mode call with no ``encoding=`` follows the locale. A subprocess decode
with no ``errors=`` is strict, so one invalid UTF-8 byte from a child raises
``UnicodeDecodeError``. Ruff's PLW1514 sees only file calls on a visible
``Path(...)`` and no subprocess calls at all, so this AST scan holds the line
over ``src/hyperi_ci/`` and ``scripts/``.

The rules:

- ``subprocess.run`` / ``Popen`` / ``check_output`` / ``check_call`` / ``call``
  in text mode needs ``encoding=`` and ``errors=``. ``getoutput`` and
  ``getstatusoutput`` are always text mode, so they always need both.
- ``os.popen`` is flagged outright. It takes no encoding, so use
  ``subprocess.run`` with one.
- ``read_text()`` and a text-mode ``open()`` or ``os.fdopen()`` need
  ``encoding=``.
- ``write_text()`` and a text-mode ``open()`` or ``os.fdopen()`` for writing also
  need ``newline=``.
- ``tempfile.NamedTemporaryFile`` and friends follow the ``open()`` rules once
  given a text mode. Their default mode is binary.

What the scan cannot see, and so does not judge:

- a call with a ``**kwargs`` splat, which may carry the pins;
- ``functools.partial(subprocess.run, ...)``, whose pins may arrive at the
  eventual call;
- ``newline=`` on a call whose mode is a variable, since a read and a write look
  the same. ``encoding=`` is still required there;
- ``importlib.metadata``'s ``distribution(...).read_text(filename)``, which
  takes no encoding and reads UTF-8 itself.
"""

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCANNED = (_ROOT / "src" / "hyperi_ci", _ROOT / "scripts")

_SUBPROCESS_FUNCS = frozenset({"run", "check_output", "Popen", "check_call", "call"})
_SUBPROCESS_SHELL_FUNCS = frozenset({"getoutput", "getstatusoutput"})
# Positional index of each tempfile factory's mode argument.
_TEMPFILE_MODE_POSITION = {
    "NamedTemporaryFile": 0,
    "TemporaryFile": 0,
    "SpooledTemporaryFile": 1,
}
# Import aliases of these resolve back to the module, so ``sp.run`` is seen.
_TRACKED_MODULES = frozenset({"io", "os", "subprocess"})
# ``open`` on these modules is not a builtin-style file open.
_OTHER_OPENS = frozenset({"tarfile", "zipfile", "gzip", "bz2", "lzma", "os"})
_POPEN_FINDING = "os.popen takes no encoding: use subprocess.run"


def _kwarg(call: ast.Call, name: str) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _mode(call: ast.Call, position: int, default: str) -> str | None:
    """Return the call's constant mode string, or None when it is not a literal."""
    node = call.args[position] if len(call.args) > position else _kwarg(call, "mode")
    if node is None:
        return default
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _bindings(
    tree: ast.Module,
) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    """Map local names to the tracked modules and to functions imported from them."""
    modules: dict[str, str] = {}
    functions: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _TRACKED_MODULES:
                    modules[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module in _TRACKED_MODULES:
            for alias in node.names:
                functions[alias.asname or alias.name] = (node.module, alias.name)
    return modules, functions


def _target(
    call: ast.Call,
    modules: dict[str, str],
    functions: dict[str, tuple[str, str]],
) -> tuple[str | None, str]:
    """Return the call's module (its receiver's name when untracked) and name."""
    func = call.func
    if isinstance(func, ast.Attribute):
        if not isinstance(func.value, ast.Name):
            return None, func.attr
        owner = func.value.id
        return modules.get(owner, owner), func.attr
    name = getattr(func, "id", "")
    return functions.get(name, (None, name))


def _subprocess_text_mode(call: ast.Call) -> bool:
    for name in ("text", "universal_newlines"):
        node = _kwarg(call, name)
        if node is not None and not (isinstance(node, ast.Constant) and not node.value):
            return True
    return _kwarg(call, "encoding") is not None or _kwarg(call, "errors") is not None


def _is_distribution_read(func: ast.Attribute) -> bool:
    """``distribution(...).read_text(filename)`` takes no encoding and reads UTF-8."""
    receiver = func.value
    if not isinstance(receiver, ast.Call):
        return False
    target = receiver.func
    name = (
        target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
    )
    return name == "distribution"


def _open_requirement(mode: str | None) -> tuple[str, ...] | None:
    if mode is None:
        return ("encoding",)
    if "b" in mode:
        return None
    if any(flag in mode for flag in "wax+"):
        return ("encoding", "newline")
    return ("encoding",)


def _requirement(
    call: ast.Call,
    modules: dict[str, str],
    functions: dict[str, tuple[str, str]],
) -> tuple[str, tuple[str, ...] | None]:
    """Return the call's kind and the kwargs it must carry (None: not judged)."""
    module, name = _target(call, modules, functions)
    func = call.func

    if module == "subprocess" and name in _SUBPROCESS_SHELL_FUNCS:
        return "subprocess", ("encoding", "errors")
    if module == "subprocess" and name in _SUBPROCESS_FUNCS:
        text = _subprocess_text_mode(call)
        return "subprocess", ("encoding", "errors") if text else None
    if isinstance(func, ast.Attribute) and name == "read_text":
        return "read_text", None if _is_distribution_read(func) else ("encoding",)
    if isinstance(func, ast.Attribute) and name == "write_text":
        return "write_text", ("encoding", "newline")
    if name in _TEMPFILE_MODE_POSITION:
        mode = _mode(call, _TEMPFILE_MODE_POSITION[name], "w+b")
        return "tempfile", _open_requirement(mode)
    if module == "os" and name == "fdopen":
        return "fdopen", _open_requirement(_mode(call, 1, "r"))
    if name == "open" and module not in _OTHER_OPENS:
        # Builtin and io.open take the mode second, Path.open takes it first.
        position = 1 if isinstance(func, ast.Name) or module == "io" else 0
        return "open", _open_requirement(_mode(call, position, "r"))
    return name, None


def scan(source: str) -> list[tuple[int, str]]:
    """Return ``(line, finding)`` for each text-mode call missing a pinned kwarg."""
    tree = ast.parse(source)
    modules, functions = _bindings(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _target(node, modules, functions) == ("os", "popen"):
            found.append((node.lineno, _POPEN_FINDING))
            continue
        if any(keyword.arg is None for keyword in node.keywords):
            continue
        kind, required = _requirement(node, modules, functions)
        missing = [name for name in required or () if _kwarg(node, name) is None]
        if missing:
            found.append((node.lineno, f"{kind} missing {', '.join(missing)}"))
    return sorted(found)


def test_src_and_scripts_pin_every_text_encoding() -> None:
    unpinned: list[str] = []
    for base in _SCANNED:
        for path in sorted(base.rglob("*.py")):
            relative = path.relative_to(_ROOT)
            for line, finding in scan(path.read_text(encoding="utf-8")):
                unpinned.append(f"{relative}:{line}: {finding}")
    assert not unpinned, (
        'Pin encoding="utf-8" on every text-mode call. Subprocess calls also '
        'take errors="replace". Writes also take newline="\\n".\n' + "\n".join(unpinned)
    )


def test_every_scanned_root_holds_python() -> None:
    # A mistyped root yields no files, and the scan above would then pass on nothing.
    assert all(any(base.rglob("*.py")) for base in _SCANNED)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "import subprocess\nsubprocess.run(cmd, text=True)",
            ["subprocess missing encoding, errors"],
        ),
        (
            'import subprocess\nsubprocess.run(cmd, text=True, encoding="utf-8")',
            ["subprocess missing errors"],
        ),
        (
            "import subprocess\n"
            'subprocess.run(cmd, text=True, encoding="utf-8", errors="replace")',
            [],
        ),
        (
            "import subprocess as sp\nsp.Popen(cmd, universal_newlines=True)",
            ["subprocess missing encoding, errors"],
        ),
        (
            "from subprocess import check_output\ncheck_output(cmd, text=True)",
            ["subprocess missing encoding, errors"],
        ),
        ("import subprocess\nsubprocess.run(cmd, capture_output=True)", []),
        ("import subprocess\nsubprocess.run(cmd, text=False)", []),
        ("import subprocess\nsubprocess.run(cmd, text=True, **kwargs)", []),
        ("run(cmd, text=True)", []),
        (
            "import subprocess\nsubprocess.getoutput(cmd)",
            ["subprocess missing encoding, errors"],
        ),
        (
            "import subprocess\n"
            'subprocess.getoutput(cmd, encoding="utf-8", errors="replace")',
            [],
        ),
        (
            "from subprocess import getstatusoutput as gso\ngso(cmd)",
            ["subprocess missing encoding, errors"],
        ),
        (
            "from subprocess import getstatusoutput\n"
            'getstatusoutput(cmd, encoding="utf-8", errors="replace")',
            [],
        ),
        ("import os\nos.popen(cmd)", [_POPEN_FINDING]),
        ('import os\nos.popen(cmd, "w", **kwargs)', [_POPEN_FINDING]),
        ("from os import popen\npopen(cmd)", [_POPEN_FINDING]),
        ('import os\nos.fdopen(fd, "w")', ["fdopen missing encoding, newline"]),
        ('import os\nos.fdopen(fd, "w", encoding="utf-8", newline="\\n")', []),
        ("import os\nos.fdopen(fd)", ["fdopen missing encoding"]),
        ('import os\nos.fdopen(fd, encoding="utf-8")', []),
        (
            'from os import fdopen\nfdopen(fd, mode="a")',
            ["fdopen missing encoding, newline"],
        ),
        ('import os\nos.fdopen(fd, "wb")', []),
        ("import os\nos.open(path, flags)", []),
        ("path.read_text()", ["read_text missing encoding"]),
        ('distribution("hyperi-ci").read_text("direct_url.json")', []),
        ('path.write_text(body, encoding="utf-8")', ["write_text missing newline"]),
        ("open(path)", ["open missing encoding"]),
        ('open(path, "a", encoding="utf-8")', ["open missing newline"]),
        ("open(path, mode)", ["open missing encoding"]),
        ('open(path, mode, encoding="utf-8")', []),
        ('open(path, "rb")', []),
        ('path.open("w")', ["open missing encoding, newline"]),
        ('path.open("rb")', []),
        ('tarfile.open(fileobj=data, mode="r:gz")', []),
        (
            'tempfile.NamedTemporaryFile(mode="w")',
            ["tempfile missing encoding, newline"],
        ),
        (
            'tempfile.NamedTemporaryFile("w", encoding="utf-8")',
            ["tempfile missing newline"],
        ),
        ('tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\\n")', []),
        ("tempfile.NamedTemporaryFile()", []),
        ("tempfile.SpooledTemporaryFile(1024)", []),
        (
            'tempfile.SpooledTemporaryFile(1024, "w")',
            ["tempfile missing encoding, newline"],
        ),
        (
            "import functools, subprocess\n"
            "functools.partial(subprocess.run, text=True)",
            [],
        ),
    ],
)
def test_scan_rules(source: str, expected: list[str]) -> None:
    assert [finding for _, finding in scan(source)] == expected
