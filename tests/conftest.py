# Project:   HyperI CI
# File:      tests/conftest.py
# Purpose:   Shared fixtures -- keep the suite off the developer's real config
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

import functools
import shutil
import struct
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Self

import pytest
from scalo.logger import setup as setup_logger

from hyperi_ci import channel, common
from hyperi_ci.common import is_ci, run_cmd
from hyperi_ci.container import stage as container_stage


@functools.cache
def repo_local_git_env() -> tuple[str, ...]:
    """The environment variables that bind git to one repository, as git lists them."""
    if shutil.which("git") is None:
        return ()
    result = run_cmd(
        ["git", "rev-parse", "--local-env-vars"], capture=True, check=False
    )
    return tuple(result.stdout.split())


def _clear_repo_local_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every variable that binds git to one repository."""
    for name in repo_local_git_env():
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def clear_git_env() -> Callable[[pytest.MonkeyPatch], None]:
    """The helper the autouse fixture runs, for a test that proves it."""
    return _clear_repo_local_git_env


@pytest.fixture(autouse=True)
def detached_from_the_calling_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every git call a test makes off the repository running the suite.

    Under `git rebase --exec` or a git hook, git exports GIT_DIR, and a test
    that runs `git init <tmp>` re-initialises THAT repository instead. A linked
    worktree's GIT_DIR turns the main checkout bare.
    """
    _clear_repo_local_git_env(monkeypatch)


def _write_elf(
    path: Path, sections: list[str], *, is_64: bool = True, little: bool = True
) -> Path:
    """Write a minimal valid ELF file whose section table carries ``sections``.

    Enough structure for a section-table reader -- header, name string table,
    section headers -- and nothing a loader would need.
    """
    order = "<" if little else ">"
    names = ["", *sections, ".shstrtab"]
    strtab = b""
    name_offsets: list[int] = []
    for name in names:
        name_offsets.append(len(strtab))
        strtab += name.encode("ascii") + b"\0"

    ehsize = 64 if is_64 else 52
    shentsize = 64 if is_64 else 40
    strtab_offset = ehsize
    shoff = strtab_offset + len(strtab)
    shnum = len(names)
    shstrndx = shnum - 1

    ident = b"\x7fELF" + bytes([2 if is_64 else 1, 1 if little else 2, 1]) + bytes(9)
    header_layout = order + ("HHIQQQIHHHHHH" if is_64 else "HHIIIIIHHHHHH")
    header = ident + struct.pack(
        header_layout,
        2,
        62,
        1,
        0,
        0,
        shoff,
        0,
        ehsize,
        0,
        0,
        shentsize,
        shnum,
        shstrndx,
    )

    section_layout = order + ("IIQQQQIIQQ" if is_64 else "IIIIIIIIII")
    table = b""
    for index, name_offset in enumerate(name_offsets):
        is_strtab = index == shstrndx
        sh_type = 3 if is_strtab else (0 if index == 0 else 7)
        table += struct.pack(
            section_layout,
            name_offset,
            sh_type,
            0,
            0,
            strtab_offset if is_strtab else 0,
            len(strtab) if is_strtab else 0,
            0,
            0,
            1,
            0,
        )

    path.write_bytes(header + strtab + table)
    return path


@pytest.fixture
def make_elf() -> Callable[..., Path]:
    """Factory for real ELF files with a chosen section table."""
    return _write_elf


def _fork_history(
    root: Path,
    *,
    own: tuple[str, ...] = ("fix: own fix",),
    upstream_feature: str = "feat: upstream feature",
) -> Path:
    """Build a fork's main at ``root``: v0.2.8, then a sync merge of upstream.

    Upstream adds a ``feat:`` and a breaking ``feat!:`` and tags ``v9.0.0``,
    all reachable from main only through the merge's second parent. ``own``
    are the fork's commits before the merge and ``upstream_feature`` is the
    message of upstream's first commit; a ``docs:`` commit follows it.
    """

    def git(*args: str) -> None:
        run_cmd(["git", *args], capture=True, check=True, cwd=root)

    def commit(message: str) -> None:
        git("commit", "--allow-empty", "-q", "-m", message)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "ci@example.invalid")
    git("config", "user.name", "CI")
    git("config", "commit.gpgsign", "false")
    git("config", "tag.gpgsign", "false")
    commit("chore: upstream root")
    git("branch", "upstream")
    commit("fix: first fork release")
    git("tag", "v0.2.8")

    git("checkout", "-q", "upstream")
    commit(upstream_feature)
    commit("feat(api)!: upstream breaking change")
    git("tag", "v9.0.0")

    git("checkout", "-q", "main")
    for message in own:
        commit(message)
    git(
        "merge",
        "--no-ff",
        "-q",
        "upstream",
        "-m",
        "Merge pull request #121 from hyperi-io/sync-upstream",
    )
    commit("docs: own docs\n\nRelease: true")
    return root


@pytest.fixture
def make_fork_history() -> Callable[..., Path]:
    """Factory for a real fork repo whose sync merge brings upstream feat: commits."""
    return _fork_history


class _Response:
    """Context-manager stand-in for what ``urlopen`` returns."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


@pytest.fixture
def fake_urlopen(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[Exception | bytes], list[str], list[float]]:
    """Answer every ``urlopen`` from a script, and skip the retry backoff.

    Returns ``(outcomes, asked, sleeps)``. Each request takes the next outcome:
    an exception is raised, bytes are served as the body. Once ``outcomes`` is
    empty every request is refused, the way it is on a machine with no route.
    ``asked`` records each URL requested and ``sleeps`` each backoff skipped.
    """
    outcomes: list[Exception | bytes] = []
    asked: list[str] = []
    sleeps: list[float] = []

    def urlopen(request: urllib.request.Request, timeout: float) -> _Response:
        asked.append(request.full_url)
        if not outcomes:
            raise urllib.error.URLError("no route to host")
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _Response(outcome)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(common.time, "sleep", sleeps.append)
    return outcomes, asked, sleeps


@pytest.fixture(autouse=True, scope="session")
def plain_logger() -> None:
    """Log as a terminal would, even when the suite runs inside GitHub Actions.

    common.py picks the logger's sink once, at import. Under Actions that sink
    turns every warn() and error() a test's fixture provokes into a real
    ::warning:: or ::error:: annotation on the run, so a green suite printed
    hundreds of them and filled the per-step annotation cap. A test that needs
    the Actions sink starts its own process with its own environment.
    """
    setup_logger(ci_mode=False, scrub_config=common.SCRUB_CONFIG)


@pytest.fixture(autouse=True)
def isolated_channel_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point auto-update channel/freeze state at tmp_path for every test.

    Without this a developer who has run `hyperi-ci autoupdate freeze` (or has
    hyperi-ai frozen) sees auto-update tests fail on their machine and pass in
    CI. The state is two files in the homedir, so the only safe default is to
    redirect both tools' directories.

    Returns:
        The redirected hyperi-ci config directory.

    """
    ci_dir = tmp_path / "config-hyperi-ci"
    ai_dir = tmp_path / "config-hyperi-ai"
    monkeypatch.setattr(channel, "CONFIG_DIR", ci_dir)
    monkeypatch.setattr(channel, "AI_CONFIG_DIR", ai_dir)
    return ci_dir


@pytest.fixture(autouse=True)
def no_inherited_quality_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the run-mode switches the calling shell or run set.

    `hyperi-ci check --strict` exports HYPERCI_QUALITY_STRICT before its test
    stage, which turned every warn-mode test blocking, and an exported
    HYPERCI_CLASSIFICATION turns every repo a test builds into that category.
    """
    for name in (
        "HYPERCI_QUALITY_STRICT",
        "HYPERCI_QUALITY_SKIP",
        "HYPERCI_CLASSIFICATION",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_registry_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer the release build's registry lookup with "not published".

    A release-mode container test would otherwise ask the real registry. A test
    of the lookup patches it back.
    """
    monkeypatch.setattr(container_stage, "published_image", lambda _ref: None)


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    """Name every skipped test in CI, where a skip is a coverage gap.

    A skip is invisible in a green run -- "6 skipped" scrolls past and nobody
    reads which six. In CI that matters: every tool is meant to be present, so
    a test skipping for a missing one is coverage that silently went away. On
    a developer's machine it is an environment gap and stays quiet.

    Reported, not failed. Turning these red unattended would break main for a
    node package nobody asked for; naming them makes the gap knowable, which
    is the half that was missing.
    """
    if not is_ci():
        return
    skipped = terminalreporter.stats.get("skipped", [])
    if not skipped:
        return
    print(
        f"::warning title=hyperi-ci {len(skipped)} test(s) skipped in CI::"
        f"A skipped test is untested code, not a passing one. "
        f"Every tool is meant to be present on a runner."
    )
    for report in skipped:
        reason = ""
        if isinstance(getattr(report, "longrepr", None), tuple):
            reason = str(report.longrepr[2])
        print(f"::warning::skipped: {report.nodeid} -- {reason}")
