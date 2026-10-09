# Project:   HyperI CI
# File:      tests/unit/test_gate_tool_runner.py
# Purpose:   Tests for the one runner every language's quality gate goes through
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""``run_gate_tool`` decides every per-language quality gate.

Each language once carried its own copy, and they drifted: TypeScript crashed
on a missing npx instead of gating (#538), and printed tool output to stdout
where every verdict went to stderr. The cases below run once per language,
with the command and resolution that language's handler really passes.
"""

import inspect
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from hyperi_ci import common, tools
from hyperi_ci.config import CIConfig
from hyperi_ci.languages import quality_common
from hyperi_ci.languages.golang import quality as golang_quality
from hyperi_ci.languages.python import quality as python_quality
from hyperi_ci.languages.quality_common import (
    WARN_OUTPUT_CAP,
    Via,
    emit_tool_output,
    run_gate_tool,
)
from hyperi_ci.languages.rust import quality as rust_quality
from hyperi_ci.languages.typescript import quality as typescript_quality
from hyperi_ci.quality import cargo_flags, osv_scanner

# One real call site per handler: its command and how that handler resolves it.
# TestTheTableIsWhatTheHandlersPass holds each row to the handler itself.
_CALLS: dict[str, tuple[str, list[str], Via]] = {
    "python": ("ruff format", ["ruff", "format", "--check", "."], "uv"),
    "rust": ("cargo fmt", ["cargo", "fmt", "--check"], "path"),
    "golang": ("gofmt", ["gofmt", "-l", "."], "path"),
    "typescript": ("eslint", ["npx", "eslint", "."], "path"),
}

_CLAP = "error: unexpected argument '--extend-exclude' found\n"
_SPAWN = "error: Failed to spawn: `ty`\n  Caused by: No such file or directory\n"

# cargo-audit 0.22's message when the RustSec database cannot be fetched.
_CARGO_AUDIT_NO_DB = (
    "error: error loading advisory database: failed to fetch advisory database\n"
)


class _Log:
    """What the runner logged, by level, and every command it ran."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.lines: list[tuple[str, str]] = []
        self.ran: list[list[str]] = []
        self.results: list[subprocess.CompletedProcess[str]] = []
        self.sleeps: list[float] = []
        monkeypatch.delenv("HYPERCI_QUALITY_STRICT", raising=False)
        self.in_ci(monkeypatch, False)
        monkeypatch.setattr(quality_common.time, "sleep", self.sleeps.append)
        monkeypatch.setattr(quality_common, "run_cmd", self._run)
        monkeypatch.setattr(python_quality, "warn", self._level("warn"))
        for level in ("info", "warn", "error", "success"):
            monkeypatch.setattr(quality_common, level, self._level(level))
        for level in ("warn", "error"):
            monkeypatch.setattr(tools, level, self._level(level))

    @staticmethod
    def in_ci(monkeypatch: pytest.MonkeyPatch, ci: bool) -> None:
        """Set CI for the runner and for the missing-tool path it hands off to."""
        monkeypatch.setattr(quality_common, "is_ci", lambda: ci)
        monkeypatch.setattr(tools, "is_ci", lambda: ci)

    def _level(self, level: str):
        return lambda msg: self.lines.append((level, msg))

    def _run(self, cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        self.ran.append(list(cmd))
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        return subprocess.CompletedProcess(
            cmd, result.returncode, result.stdout, result.stderr
        )

    def returns(self, rc: int, stdout: str = "", stderr: str = "") -> None:
        self.results.append(subprocess.CompletedProcess([], rc, stdout, stderr))

    def said(self, level: str) -> list[str]:
        return [msg for lvl, msg in self.lines if lvl == level]

    @property
    def text(self) -> str:
        return "\n".join(msg for _lvl, msg in self.lines)


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> _Log:
    return _Log(monkeypatch)


@pytest.fixture
def on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every tool is installed, uv included."""
    monkeypatch.setattr(quality_common.shutil, "which", lambda n: f"/usr/bin/{n}")


@pytest.fixture
def nothing_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """No tool is installed, and no uv to fetch one."""
    monkeypatch.setattr(quality_common.shutil, "which", lambda _n: None)


def _run(lang: str, mode: str, **kw: Any) -> bool:
    name, cmd, via = _CALLS[lang]
    return run_gate_tool(name, list(cmd), mode, via=via, **kw)


def _missing(lang: str) -> str:
    """The notice for ``lang``'s binary, naming the gate that needed it."""
    name, cmd, _via = _CALLS[lang]
    return f"`{cmd[0]}` is not installed - hyperi-ci needs it for the {name} gate."


@pytest.mark.parametrize("lang", _CALLS)
class TestEveryLanguage:
    """The gate reads the same in every language."""

    def test_disabled_runs_nothing(self, lang: str, log: _Log, on_path: None) -> None:
        assert _run(lang, "disabled") is True
        assert log.ran == []
        assert log.said("info") == [f"  {_CALLS[lang][0]}: disabled"]

    def test_a_missing_tool_fails_a_blocking_gate_in_ci(
        self,
        lang: str,
        log: _Log,
        nothing_on_path: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        log.in_ci(monkeypatch, True)
        assert _run(lang, "blocking") is False
        assert log.ran == []
        assert log.said("error") == [_missing(lang)]

    @pytest.mark.parametrize("mode", ["blocking", "warn"])
    def test_a_missing_tool_is_skipped_locally(
        self, lang: str, mode: str, log: _Log, nothing_on_path: None
    ) -> None:
        assert _run(lang, mode) is True
        assert log.ran == []
        assert log.said("warn") == [_missing(lang)]

    def test_a_missing_warn_tool_is_skipped_in_ci(
        self,
        lang: str,
        log: _Log,
        nothing_on_path: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        log.in_ci(monkeypatch, True)
        assert _run(lang, "warn") is True
        assert log.said("error") == []

    def test_a_clean_run_passes(self, lang: str, log: _Log, on_path: None) -> None:
        log.returns(0, "nothing to report\n")
        assert _run(lang, "blocking") is True
        assert log.ran == [_CALLS[lang][1]]
        assert log.said("success") == [f"  {_CALLS[lang][0]}: passed"]

    def test_a_blocking_finding_fails_and_shows_everything(
        self, lang: str, log: _Log, on_path: None
    ) -> None:
        findings = "\n".join(f"finding {i}" for i in range(200))
        log.returns(1, findings, "tool says why\n")
        assert _run(lang, "blocking") is False
        assert log.said("error") == [f"  {_CALLS[lang][0]}: failed"]
        shown = log.said("info")
        assert len(shown) == 201
        assert shown[-1] == "    tool says why"

    def test_a_warn_finding_passes_capped_with_its_stderr(
        self, lang: str, log: _Log, on_path: None
    ) -> None:
        findings = "\n".join(f"finding {i}" for i in range(111))
        log.returns(1, findings, "error: failed to discover a Python environment\n")
        assert _run(lang, "warn") is True
        name = _CALLS[lang][0]
        assert log.said("warn") == [f"  {name}: issues found (non-blocking)"]
        assert len(log.said("info")) == WARN_OUTPUT_CAP + 2 + 1
        assert "failed to discover a Python environment" in log.text

    def test_no_output_reaches_stdout(
        self,
        lang: str,
        log: _Log,
        on_path: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """TypeScript once printed tool output where the verdicts never went."""
        log.returns(1, "a finding\n", "a reason\n")
        _run(lang, "blocking")
        assert capsys.readouterr().out == ""


_DEFAULT_VIA = inspect.signature(run_gate_tool).parameters["via"].default


class _Calls:
    """Every gate call a handler makes, by tool name: its argv, ``via`` and options."""

    def __init__(self) -> None:
        self.seen: dict[str, tuple[list[str], str]] = {}
        self.options: dict[str, dict[str, Any]] = {}

    def record(self, name: str, cmd: list[str], _mode: str, **kw: Any) -> bool:
        self.seen[name] = (list(cmd), str(kw.get("via", _DEFAULT_VIA)))
        self.options[name] = kw
        return True


def _python_calls(mp: pytest.MonkeyPatch, tmp: Path, calls: _Calls) -> None:
    mp.chdir(tmp)
    mp.setattr(python_quality, "run_gate_tool", calls.record)
    mp.setattr(python_quality, "_ruff_format_takes_extend_exclude", lambda: True)
    python_quality.run(CIConfig(_raw={}))


def _golang_calls(mp: pytest.MonkeyPatch, tmp: Path, calls: _Calls) -> None:
    mp.chdir(tmp)
    mp.setattr(golang_quality, "run_gate_tool", calls.record)
    golang_quality.run(CIConfig(_raw={}))


def _rust_calls(mp: pytest.MonkeyPatch, tmp: Path, calls: _Calls) -> None:
    mp.chdir(tmp)
    (tmp / "deny.toml").write_text("", encoding="utf-8")
    done = subprocess.CompletedProcess([], 0, "", "")
    mp.setattr(rust_quality, "run_gate_tool", calls.record)
    mp.setattr(rust_quality, "run_cmd", lambda *_a, **_k: done)
    mp.setattr(rust_quality, "_has_lib_target", lambda *_a: True)
    mp.setattr(rust_quality, "_package_lib_map", lambda *_a: {})
    mp.setattr(rust_quality, "_run_matrix_pass", lambda *_a: True)
    mp.setattr(osv_scanner, "run", lambda *_a, **_k: True)
    mp.setattr(cargo_flags, "run", lambda *_a: 0)
    rust_quality.run(CIConfig(_raw={}))


def _typescript_calls(mp: pytest.MonkeyPatch, tmp: Path, calls: _Calls) -> None:
    mp.chdir(tmp)
    (tmp / "package.json").write_text('{"name": "t"}', encoding="utf-8")
    (tmp / "eslint.config.js").write_text("export default []\n", encoding="utf-8")
    mp.setattr(typescript_quality, "detect_package_manager", lambda: "npm")
    mp.setattr(typescript_quality, "ensure_pm_available", lambda _pm: True)
    mp.setattr(typescript_quality, "run_gate_tool", calls.record)
    mp.setattr(osv_scanner, "run", lambda *_a, **_k: True)
    typescript_quality.run(CIConfig(_raw={}))


_HANDLERS: dict[str, Callable[[pytest.MonkeyPatch, Path, _Calls], None]] = {
    "python": _python_calls,
    "golang": _golang_calls,
    "rust": _rust_calls,
    "typescript": _typescript_calls,
}


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> _Calls:
    monkeypatch.delenv("HYPERCI_QUALITY_SKIP", raising=False)
    monkeypatch.delenv("HYPERCI_QUALITY_STRICT", raising=False)
    monkeypatch.setattr(quality_common, "is_ci", lambda: False)
    return _Calls()


class TestTheTableIsWhatTheHandlersPass:
    """A copied ``via`` passes here while the handler resolves another way."""

    @pytest.mark.parametrize("lang", _CALLS)
    def test_each_row_matches_its_call_site(
        self,
        lang: str,
        calls: _Calls,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _HANDLERS[lang](monkeypatch, tmp_path, calls)
        name, cmd, via = _CALLS[lang]
        assert name in calls.seen, sorted(calls.seen)
        seen_cmd, seen_via = calls.seen[name]
        assert seen_cmd[: len(cmd)] == cmd
        assert seen_via == via

    @pytest.mark.parametrize("lang", ["golang", "rust", "typescript"])
    def test_only_python_resolves_through_uv(
        self,
        lang: str,
        calls: _Calls,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _HANDLERS[lang](monkeypatch, tmp_path, calls)
        assert calls.seen
        assert {via for _cmd, via in calls.seen.values()} == {"path"}

    def test_every_python_gate_names_its_resolution(
        self, calls: _Calls, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """No Python gate inherits a ``via`` from a helper's default."""
        monkeypatch.setattr(
            python_quality, "get_python_source_paths", lambda _c: ["src"]
        )
        _python_calls(monkeypatch, tmp_path, calls)
        assert {name: via for name, (_cmd, via) in calls.seen.items()} == {
            "ruff check (src)": "uv",
            "ruff format": "uv",
            "ty": "uv-with",
            "ruff security": "uv",
            "pip-audit": "uv",
            "ruff docstrings": "uv",
            "vulture": "uvx",
        }
        assert all("via" in options for options in calls.options.values())
        source_via = inspect.signature(python_quality._run_source_tool).parameters
        assert source_via["via"].default is inspect.Parameter.empty

    def test_cargo_audit_retries_an_unreachable_db(
        self, calls: _Calls, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _rust_calls(monkeypatch, tmp_path, calls)
        options = calls.options["cargo audit"]
        assert options["retry_unreachable"] is rust_quality._advisory_db_unreachable

    def test_cargo_deny_retries_an_unreachable_db(
        self, calls: _Calls, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _rust_calls(monkeypatch, tmp_path, calls)
        options = calls.options["cargo deny"]
        assert options["retry_unreachable"] is rust_quality._advisory_db_unreachable

    def test_gofmt_reads_its_listing_as_a_finding(
        self, calls: _Calls, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _golang_calls(monkeypatch, tmp_path, calls)
        assert calls.options["gofmt"].get("output_is_finding") is True


class TestOutputGoesThroughOneStream:
    """Ordering is the defect. Two streams cannot be kept in order."""

    def test_nothing_is_emitted_for_empty_output(self, log: _Log) -> None:
        emit_tool_output("vulture", "")
        emit_tool_output("vulture", "   \n  ")
        emit_tool_output("vulture", None)
        assert log.lines == []

    def test_it_does_not_reach_stdout(
        self, log: _Log, capsys: pytest.CaptureFixture[str]
    ) -> None:
        emit_tool_output("vulture", "a finding")
        assert capsys.readouterr().out == ""


class TestAdvisoryNoiseIsCapped:
    """111 findings at 60 percent confidence buried a real failure."""

    def test_a_warn_tool_is_capped_and_says_how_many_it_dropped(
        self, log: _Log
    ) -> None:
        findings = "\n".join(f"finding {i}" for i in range(111))
        emit_tool_output("vulture", findings, cap=WARN_OUTPUT_CAP)
        said = log.said("info")
        assert len(said) == WARN_OUTPUT_CAP + 2
        # It counts LINES. A tool that prints a code frame per finding would read
        # as ten times its real backlog if this said findings.
        assert "+85 more lines from vulture" in said[-2]

    def test_the_last_line_survives_the_cap(self, log: _Log) -> None:
        """ty and ruff end on their own count, the number a reader wants."""
        frames = "\n".join(f"frame line {i}" for i in range(9027))
        emit_tool_output("ty", f"{frames}\nFound 919 diagnostics", cap=WARN_OUTPUT_CAP)
        assert log.said("info")[-1].strip() == "Found 919 diagnostics"

    def test_one_line_over_the_cap_shows_everything(self, log: _Log) -> None:
        cap = WARN_OUTPUT_CAP
        emit_tool_output("ruff", "\n".join(map(str, range(cap + 1))), cap=cap)
        assert len(log.said("info")) == cap + 1
        assert "more lines from" not in log.text

    def test_output_under_the_cap_is_not_truncated(self, log: _Log) -> None:
        emit_tool_output("ruff", "one\ntwo", cap=WARN_OUTPUT_CAP)
        assert len(log.said("info")) == 2
        assert "more lines from" not in log.text

    def test_a_blocking_failure_is_never_capped(self, log: _Log) -> None:
        """This is the output someone has to act on to get the build back."""
        emit_tool_output("ruff", "\n".join(str(i) for i in range(200)))
        assert len(log.said("info")) == 200


def _vulture(mode: str) -> bool:
    return run_gate_tool(
        "vulture",
        ["vulture", "src/"],
        mode,
        via="uvx",
        spec="vulture==2.16",
        python="3.99",
    )


class TestAToolThatNeverStarted:
    """uv's own failure to start a tool is neither a pass nor a finding."""

    def test_uv_with_no_interpreter_reads_as_not_started(
        self, log: _Log, on_path: None
    ) -> None:
        # uv 0.12 with `--python 3.99` and downloads off, stderr verbatim.
        log.returns(
            2,
            stderr="error: No interpreter found for Python 3.99 in managed "
            "installations or search path\n",
        )
        assert _vulture("warn") is True
        assert log.said("warn") == ["  vulture: could not start, so it checked nothing"]

    def test_a_tool_that_could_not_start_is_not_a_finding(
        self, log: _Log, on_path: None
    ) -> None:
        log.returns(2, stderr=_SPAWN)
        assert _run("python", "warn") is True
        assert "could not start" in log.text
        assert "issues found" not in log.text

    def test_a_required_tool_that_could_not_start_fails_in_ci(
        self, log: _Log, on_path: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(quality_common, "is_ci", lambda: True)
        log.returns(2, stderr=_SPAWN)
        assert _run("python", "blocking") is False
        assert "  ruff format: could not start (required)" in log.said("error")


class TestArgumentRejectionIsNotAFinding:
    """A refused flag means the tool never ran, so it is not a finding (#146)."""

    def test_blocking_names_the_mismatch_not_a_failure(
        self, log: _Log, on_path: None
    ) -> None:
        log.returns(2, stderr=_CLAP)
        assert _run("python", "blocking") is False
        assert any("tool-version mismatch" in m for m in log.said("error"))
        assert not any(m.endswith("failed") for m in log.said("error"))

    def test_warn_mode_still_does_not_block(self, log: _Log, on_path: None) -> None:
        log.returns(2, stderr=_CLAP)
        assert _run("python", "warn") is True
        assert any("tool-version mismatch" in m for m in log.said("warn"))

    def test_a_real_finding_still_reads_as_failed(
        self, log: _Log, on_path: None
    ) -> None:
        log.returns(1, stderr="would reformat: x.py\n")
        assert _run("python", "blocking") is False
        assert log.said("error") == ["  ruff format: failed"]

    def test_a_rustc_diagnostic_is_a_finding(self, log: _Log, on_path: None) -> None:
        """rustc labels an extra call argument "unexpected argument" too."""
        log.returns(101, stderr="error[E0061]: unexpected argument #2 of type `u8`\n")
        assert _run("rust", "blocking") is False
        assert log.said("error") == ["  cargo fmt: failed"]


def _cargo_audit(mode: str, **kw: Any) -> bool:
    return run_gate_tool("cargo audit", ["cargo", "audit"], mode, **kw)


class TestAnUnreachableAdvisoryDb:
    """An unreachable DB is retried, then decided by the mode like a finding."""

    def test_a_real_finding_is_not_retried(self, log: _Log, on_path: None) -> None:
        log.returns(1, "Crate: time\nID: RUSTSEC-2020-0071\n")
        assert not _cargo_audit(
            "blocking", retry_unreachable=rust_quality._advisory_db_unreachable
        )
        assert len(log.ran) == 1
        assert log.sleeps == []

    def test_retry_then_a_blocking_gate_fails(self, log: _Log, on_path: None) -> None:
        log.returns(1, stderr=_CARGO_AUDIT_NO_DB)
        assert not _cargo_audit(
            "blocking", retry_unreachable=rust_quality._advisory_db_unreachable
        )
        assert len(log.ran) == common.URL_ATTEMPTS
        assert len(log.sleeps) == common.URL_ATTEMPTS - 1
        unreachable = f"advisory DB unreachable after {common.URL_ATTEMPTS} attempts"
        assert sum(unreachable in m for m in log.said("error")) == 1

    def test_retry_then_a_warn_gate_passes(self, log: _Log, on_path: None) -> None:
        log.returns(1, stderr=_CARGO_AUDIT_NO_DB)
        assert _cargo_audit(
            "warn", retry_unreachable=rust_quality._advisory_db_unreachable
        )
        assert any("advisory DB unreachable" in m for m in log.said("warn"))

    def test_retry_stops_once_the_db_loads(self, log: _Log, on_path: None) -> None:
        log.returns(1, stderr=_CARGO_AUDIT_NO_DB)
        log.returns(0)
        assert _cargo_audit(
            "blocking", retry_unreachable=rust_quality._advisory_db_unreachable
        )
        assert len(log.ran) == 2


def _gofmt(mode: str) -> bool:
    return run_gate_tool("gofmt", ["gofmt", "-l", "."], mode, output_is_finding=True)


class TestOutputIsAFinding:
    """``gofmt -l`` exits 0 while it lists the files that need formatting."""

    def test_a_listing_fails_a_blocking_gate(self, log: _Log, on_path: None) -> None:
        log.returns(0, "demo.go\n")
        assert _gofmt("blocking") is False
        assert log.said("error") == ["  gofmt: failed"]
        assert log.said("info") == ["    demo.go"]

    def test_a_listing_only_warns_under_warn(self, log: _Log, on_path: None) -> None:
        log.returns(0, "demo.go\n")
        assert _gofmt("warn") is True
        assert log.said("warn") == ["  gofmt: issues found (non-blocking)"]

    def test_no_listing_passes(self, log: _Log, on_path: None) -> None:
        log.returns(0, "\n")
        assert _gofmt("blocking") is True
        assert log.said("success") == ["  gofmt: passed"]

    def test_output_is_ignored_without_the_option(
        self, log: _Log, on_path: None
    ) -> None:
        log.returns(0, "demo.go\n")
        assert run_gate_tool("gofmt", ["gofmt", "-l", "."], "blocking") is True
