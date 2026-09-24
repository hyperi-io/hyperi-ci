# Project:   HyperI CI
# File:      tests/unit/test_text_encoding_pinned.py
# Purpose:   Every text-mode subprocess and file call in src/ pins its encoding
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Guard on the pinned-encoding rule.

A text-mode call with no ``encoding=`` follows the locale. A subprocess decode
with no ``errors=`` is strict, so one invalid UTF-8 byte from a child raises
``UnicodeDecodeError``. Ruff's PLW1514 sees only file calls on a visible
``Path(...)`` and no subprocess calls at all, so this AST scan holds the line.

The rules:

- ``subprocess.run`` / ``Popen`` / ``check_output`` / ``check_call`` / ``call``
  in text mode needs ``encoding=`` and ``errors=``.
- ``read_text()`` and a text-mode ``open()`` need ``encoding=``.
- ``write_text()`` and a text-mode ``open()`` for writing also need ``newline=``.
- ``tempfile.NamedTemporaryFile`` and friends in text mode need ``encoding=``.

Binary-mode calls and calls with a ``**kwargs`` splat are not judged, and nor is
``importlib.metadata``'s ``distribution(...).read_text(filename)``, which takes
no encoding and reads UTF-8 itself.
"""

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src" / "hyperi_ci"

_SUBPROCESS_FUNCS = frozenset({"run", "check_output", "Popen", "check_call", "call"})
_TEMPFILE_FUNCS = frozenset(
    {"NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile"}
)
# ``open`` on these modules is not a builtin-style file open.
_OTHER_OPENS = frozenset({"tarfile", "zipfile", "gzip", "bz2", "lzma", "os"})


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


def _subprocess_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Return the names this module binds to ``subprocess`` and to its functions."""
    modules: set[str] = set()
    functions: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                if alias.name in _SUBPROCESS_FUNCS:
                    functions.add(alias.asname or alias.name)
    return modules, functions


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
    call: ast.Call, modules: set[str], functions: set[str]
) -> tuple[str, tuple[str, ...] | None]:
    """Return the call's kind and the kwargs it must carry (None: not judged)."""
    func = call.func
    if isinstance(func, ast.Attribute):
        name = func.attr
        owner = func.value.id if isinstance(func.value, ast.Name) else None
        is_subprocess = owner in modules and name in _SUBPROCESS_FUNCS
    else:
        name = getattr(func, "id", "")
        owner = None
        is_subprocess = name in functions

    if is_subprocess:
        text = _subprocess_text_mode(call)
        return "subprocess", ("encoding", "errors") if text else None
    if isinstance(func, ast.Attribute) and name == "read_text":
        return "read_text", None if _is_distribution_read(func) else ("encoding",)
    if isinstance(func, ast.Attribute) and name == "write_text":
        return "write_text", ("encoding", "newline")
    if name in _TEMPFILE_FUNCS:
        mode = _mode(call, 0, "w+b")
        return "tempfile", None if mode and "b" in mode else ("encoding",)
    if name == "open" and owner not in _OTHER_OPENS:
        # Builtin and io.open take the mode second, Path.open takes it first.
        position = 1 if isinstance(func, ast.Name) or owner == "io" else 0
        return "open", _open_requirement(_mode(call, position, "r"))
    return name, None


def scan(source: str) -> list[tuple[int, str]]:
    """Return ``(line, finding)`` for each text-mode call missing a pinned kwarg."""
    tree = ast.parse(source)
    modules, functions = _subprocess_names(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if any(keyword.arg is None for keyword in node.keywords):
            continue
        kind, required = _requirement(node, modules, functions)
        missing = [name for name in required or () if _kwarg(node, name) is None]
        if missing:
            found.append((node.lineno, f"{kind} missing {', '.join(missing)}"))
    return sorted(found)


def test_src_pins_every_text_encoding() -> None:
    unpinned: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        relative = path.relative_to(_ROOT)
        for line, finding in scan(path.read_text(encoding="utf-8")):
            unpinned.append(f"{relative}:{line}: {finding}")
    assert not unpinned, (
        'Pin encoding="utf-8" on every text-mode call. Subprocess calls also '
        'take errors="replace". Writes also take newline="\\n".\n' + "\n".join(unpinned)
    )


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
        ("path.read_text()", ["read_text missing encoding"]),
        ('distribution("hyperi-ci").read_text("direct_url.json")', []),
        ('path.write_text(body, encoding="utf-8")', ["write_text missing newline"]),
        ("open(path)", ["open missing encoding"]),
        ('open(path, "a", encoding="utf-8")', ["open missing newline"]),
        ("open(path, mode)", ["open missing encoding"]),
        ('open(path, "rb")', []),
        ('path.open("w")', ["open missing encoding, newline"]),
        ('path.open("rb")', []),
        ('tarfile.open(fileobj=data, mode="r:gz")', []),
        ('tempfile.NamedTemporaryFile(mode="w")', ["tempfile missing encoding"]),
        ("tempfile.NamedTemporaryFile()", []),
    ],
)
def test_scan_rules(source: str, expected: list[str]) -> None:
    assert [finding for _, finding in scan(source)] == expected
