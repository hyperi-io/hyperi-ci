# Project:   HyperI CI
# File:      tests/unit/test_workflow_interfaces.py
# Purpose:   Tests for the reusable-workflow/composite interface compat gate
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Interface backward-compat gate (issue #31).

A consumer pins a caller (`python-ci.yml@<sha>`) but its siblings are written
`@main`, so the transitive graph floats. If a sibling's `workflow_call` /
composite interface regresses, the pinned caller's graph fails to compile at
startup (0 jobs). This gate fails hyperi-ci's own CI when an interface
regresses vs the last release, so the break never reaches a consumer.
"""

import importlib.util
import subprocess
import urllib.error
from email.message import Message
from pathlib import Path

import pytest

# What the shared `fake_urlopen` fixture hands back: outcomes, URLs asked, sleeps.
_Urlopen = tuple[list[Exception | bytes], list[str], list[float]]

_SPEC = importlib.util.spec_from_file_location(
    "check_workflow_interfaces",
    Path(__file__).resolve().parents[2] / "scripts" / "check-workflow-interfaces.py",
)
assert _SPEC is not None and _SPEC.loader is not None  # always resolves for a real file
cwi = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cwi)


_REUSABLE = """
name: x
on:
  workflow_call:
    inputs:
      language:
        type: string
        required: true
      next-version:
        type: string
        default: ""
    secrets:
      TOKEN:
        required: true
    outputs:
      version:
        value: ${{ jobs.plan.outputs.v }}
"""

_COMPOSITE = """
name: setup
description: d
inputs:
  language:
    required: true
  python-version:
    required: false
    default: "3.12"
runs:
  using: composite
  steps: []
"""


class TestParseInterface:
    def test_parses_reusable_workflow(self) -> None:
        iface = cwi.parse_interface(_REUSABLE)
        assert iface["kind"] == "workflow"
        assert iface["inputs"]["language"]["required"] is True
        assert iface["inputs"]["next-version"]["required"] is False
        assert iface["inputs"]["next-version"]["has_default"] is True
        assert "TOKEN" in iface["secrets"]
        assert "version" in iface["outputs"]

    def test_parses_composite(self) -> None:
        iface = cwi.parse_interface(_COMPOSITE)
        assert iface["kind"] == "composite"
        assert iface["inputs"]["language"]["required"] is True
        assert iface["inputs"]["python-version"]["has_default"] is True
        assert iface["secrets"] == {}


class TestBreakingDeltas:
    def _wf(self, inputs=None, secrets=None, outputs=None) -> dict:
        return {
            "kind": "workflow",
            "inputs": inputs or {},
            "secrets": secrets or {},
            "outputs": set(outputs or []),
        }

    def test_no_change_is_clean(self) -> None:
        old = self._wf(inputs={"a": {"required": False, "has_default": True}})
        assert cwi.breaking_deltas(old, old) == []

    def test_added_optional_input_is_clean(self) -> None:
        old = self._wf(inputs={"a": {"required": False, "has_default": True}})
        new = self._wf(
            inputs={
                "a": {"required": False, "has_default": True},
                "b": {"required": False, "has_default": True},
            }
        )
        assert cwi.breaking_deltas(old, new) == []

    def test_new_required_input_is_breaking(self) -> None:
        old = self._wf()
        new = self._wf(inputs={"b": {"required": True, "has_default": False}})
        deltas = cwi.breaking_deltas(old, new)
        assert any("b" in d and "required" in d for d in deltas)

    def test_removed_input_is_breaking(self) -> None:
        old = self._wf(inputs={"a": {"required": False, "has_default": True}})
        new = self._wf()
        assert any("a" in d for d in cwi.breaking_deltas(old, new))

    def test_optional_to_required_is_breaking(self) -> None:
        old = self._wf(inputs={"a": {"required": False, "has_default": True}})
        new = self._wf(inputs={"a": {"required": True, "has_default": False}})
        assert any("a" in d for d in cwi.breaking_deltas(old, new))

    def test_removed_output_is_breaking(self) -> None:
        old = self._wf(outputs=["v"])
        new = self._wf()
        assert any("v" in d for d in cwi.breaking_deltas(old, new))

    def test_new_required_secret_is_breaking(self) -> None:
        old = self._wf()
        new = self._wf(secrets={"TOKEN": {"required": True}})
        assert any("TOKEN" in d for d in cwi.breaking_deltas(old, new))

    def test_removed_secret_is_breaking(self) -> None:
        old = self._wf(secrets={"TOKEN": {"required": True}})
        new = self._wf()
        assert any("TOKEN" in d for d in cwi.breaking_deltas(old, new))

    def test_new_optional_input_with_default_clean(self) -> None:
        old = self._wf()
        new = self._wf(inputs={"b": {"required": False, "has_default": True}})
        assert cwi.breaking_deltas(old, new) == []


class TestRemovedPipelineFiles:
    """A composite/workflow present at the last release but deleted now breaks
    a pinned caller's `@main` reference (404 at startup) -- flag it."""

    def test_flags_deleted_file(self) -> None:
        old = {
            ".github/workflows/rust-ci.yml",
            ".github/actions/setup-runtime/action.yml",
        }
        cur = {".github/workflows/rust-ci.yml"}
        assert cwi.removed_pipeline_files(old, cur) == [
            ".github/actions/setup-runtime/action.yml"
        ]

    def test_none_when_all_present(self) -> None:
        s = {".github/workflows/rust-ci.yml"}
        assert cwi.removed_pipeline_files(s, s) == []

    def test_added_file_not_flagged(self) -> None:
        old = {".github/workflows/rust-ci.yml"}
        cur = {".github/workflows/rust-ci.yml", ".github/workflows/new.yml"}
        assert cwi.removed_pipeline_files(old, cur) == []


def _workflow(steps: str) -> str:
    """A workflow whose one job runs `steps`, given at step-list indentation."""
    return "on: push\njobs:\n  j:\n    runs-on: x\n    steps:\n" + steps


def _line_of(text: str, needle: str) -> int:
    """1-based number of the first line containing `needle`."""
    return next(n for n, line in enumerate(text.splitlines(), 1) if needle in line)


def _args(text: str) -> list[tuple[str, ...]]:
    return [call.args for call in cwi.cli_invocations("w.yml", text).invocations]


def _opts(names: set[str], value_taking: set[str] | None = None) -> cwi.CommandOptions:
    """A `CommandOptions` for one command path, for building `_PUBLISHED`."""
    return cwi.CommandOptions(
        names=frozenset(names), value_taking=frozenset(value_taking or set())
    )


# What a published release exposes, keyed the way `_DUMP_OPTIONS` prints it.
_PUBLISHED = {
    "": _opts({"--help", "--version", "-V"}),
    "run": _opts(
        {"--help", "--project-dir", "-C", "--tier"}, {"--project-dir", "-C", "--tier"}
    ),
    "stamp-version": _opts({"--help", "--project-dir", "-C"}, {"--project-dir", "-C"}),
    "tag-head": _opts({"--help", "--bump"}, {"--bump"}),
    "release-notify": _opts(
        {"--help", "--outcome", "--run-url"}, {"--outcome", "--run-url"}
    ),
    "publish": _opts({"--help", "--bump", "-b"}, {"--bump", "-b"}),
    "publish binaries": _opts({"--help", "--dry-run", "--no-dry-run"}),
}


def _gaps(text: str) -> list[str]:
    scan = cwi.cli_invocations("w.yml", text)
    return cwi.cli_invocation_gaps(scan.invocations, _PUBLISHED)


class TestFindingCliCalls:
    """Every shape a workflow uses to call the published CLI is read as the shell would."""

    def test_a_plain_scalar_call(self) -> None:
        text = _workflow(
            '      - run: ${{ env.HYPERCI_INSTALL }} stamp-version "$V" -C dir\n'
        )
        scan = cwi.cli_invocations("w.yml", text)
        assert [c.args for c in scan.invocations] == [
            ("stamp-version", "$V", "-C", "dir")
        ]
        assert scan.invocations[0].line == _line_of(text, "stamp-version")

    def test_a_folded_block_is_one_command(self) -> None:
        text = _workflow(
            "      - run: >-\n"
            "          ${{ env.HYPERCI_INSTALL }} release-notify\n"
            '          "$RELEASE_VERSION"\n'
            "          --outcome failure\n"
            '          --run-url "${{ github.server_url }}/${{ github.run_id }}"\n'
        )
        scan = cwi.cli_invocations("w.yml", text)
        assert [c.args for c in scan.invocations] == [
            (
                "release-notify",
                "$RELEASE_VERSION",
                "--outcome",
                "failure",
                "--run-url",
                "<expr>",
            )
        ]
        assert scan.invocations[0].line == _line_of(text, "release-notify")

    def test_a_literal_block_with_continuations_and_several_calls(self) -> None:
        text = _workflow(
            "      - run: |\n"
            '          if [ -z "$(git tag --list)" ]; then\n'
            '            ${{ env.HYPERCI_INSTALL }} tag-head --bump "$V"\n'
            "          else\n"
            "            npx semantic-release\n"
            "          fi\n"
            "          ${{ env.HYPERCI_INSTALL }} release-notify \\\n"
            "            --outcome success && echo done\n"
        )
        scan = cwi.cli_invocations("w.yml", text)
        assert [c.args for c in scan.invocations] == [
            ("tag-head", "--bump", "$V"),
            ("release-notify", "--outcome", "success"),
        ]
        assert [c.line for c in scan.invocations] == [
            _line_of(text, "tag-head"),
            _line_of(text, "release-notify"),
        ]

    def test_two_calls_on_one_line(self) -> None:
        text = _workflow(
            "      - run: ${{ env.HYPERCI_INSTALL }} run build; "
            "${{ env.HYPERCI_INSTALL }} run test --tier=full\n"
        )
        assert _args(text) == [("run", "build"), ("run", "test", "--tier=full")]

    def test_an_option_inside_an_expression_is_read(self) -> None:
        """`--tier` reaches the CLI on one branch of the expression."""
        text = _workflow(
            "      - run: ${{ env.HYPERCI_INSTALL }} run test "
            "${{ needs.plan.outputs.tier == 'full' && '--tier full' || '' }}\n"
        )
        assert _args(text) == [("run", "test", "--tier", "full")]

    def test_a_shell_variable_and_an_unpinned_uvx_are_calls(self) -> None:
        text = _workflow(
            "      - run: $HYPERCI_INSTALL run quality\n"
            "      - run: uvx --python 3.14 --refresh hyperi-ci gate-check\n"
        )
        assert _args(text) == [("run", "quality"), ("gate-check",)]

    def test_a_pinned_or_local_cli_is_not_a_call(self) -> None:
        """Only the latest PyPI release is what the gate compares against."""
        text = _workflow(
            "      - run: uvx hyperi-ci==2.0.0 run quality\n"
            "      - run: uvx --from git+https://x/y@b hyperi-ci run quality\n"
            "      - run: uv run --no-sources hyperi-ci run quality\n"
        )
        assert _args(text) == []

    def test_a_quoted_mention_is_not_a_call(self) -> None:
        text = _workflow(
            '      - run: echo "use ${{ env.HYPERCI_INSTALL }} run --bogus"\n'
        )
        assert _args(text) == []

    def test_an_apostrophe_in_a_comment_does_not_open_a_quote(self) -> None:
        text = _workflow(
            "      - run: |\n"
            "          # don't stamp twice\n"
            '          ${{ env.HYPERCI_INSTALL }} stamp-version "$V"\n'
        )
        assert _args(text) == [("stamp-version", "$V")]

    def test_composite_steps_are_read_and_a_non_shell_step_is_not(self) -> None:
        text = (
            "runs:\n"
            "  using: composite\n"
            "  steps:\n"
            "    - shell: bash\n"
            "      run: |\n"
            '        "$HYPERCI_INSTALL" install-native-deps python\n'
            "    - shell: python\n"
            "      run: print('$HYPERCI_INSTALL run --x')\n"
        )
        assert _args(text) == [("install-native-deps", "python")]

    def test_an_unsplittable_line_is_reported_not_dropped(self) -> None:
        text = _workflow("      - run: ${{ env.HYPERCI_INSTALL }} run 'build\n")
        scan = cwi.cli_invocations("w.yml", text)
        assert scan.invocations == ()
        assert len(scan.unchecked) == 1
        line = _line_of(text, "'build")
        assert scan.unchecked[0].startswith(f"w.yml:{line}:")


class TestTheCliGate:
    """A workflow may only use a subcommand or option the PUBLISHED CLI has.

    Workflows float `@main` and reach a consumer instantly, the CLI arrives
    only on a release. Anything added in the same commit as its caller is
    missing on every runner until the next publish (issues #181 and #443).
    """

    def test_an_option_the_release_lacks_fails(self) -> None:
        """The #443 shape: main has `--no-stamp-cmd`, the release does not."""
        text = _workflow(
            '      - run: ${{ env.HYPERCI_INSTALL }} stamp-version "$V" '
            "--no-stamp-cmd\n"
        )
        gaps = _gaps(text)
        assert gaps == [
            f"w.yml:{_line_of(text, 'stamp-version')}: `hyperi-ci stamp-version` "
            "passes `--no-stamp-cmd`, which the published CLI does not accept"
        ]

    def test_published_options_pass_in_every_spelling(self) -> None:
        text = _workflow(
            "      - run: ${{ env.HYPERCI_INSTALL }} run test --tier=full -C dir\n"
            "      - run: ${{ env.HYPERCI_INSTALL }} --version\n"
            "      - run: ${{ env.HYPERCI_INSTALL }} stamp-version -Cdir --help\n"
        )
        assert _gaps(text) == []

    def test_an_unknown_short_flag_fails(self) -> None:
        text = _workflow("      - run: ${{ env.HYPERCI_INSTALL }} run test -Z\n")
        assert [g.split(": ", 1)[1] for g in _gaps(text)] == [
            "`hyperi-ci run` passes `-Z`, which the published CLI does not accept"
        ]

    def test_arguments_after_a_double_dash_are_not_checked(self) -> None:
        text = _workflow(
            "      - run: ${{ env.HYPERCI_INSTALL }} run test -- --anything -x\n"
        )
        assert _gaps(text) == []

    def test_a_value_taking_option_s_word_is_not_read_as_a_subcommand(self) -> None:
        """issue #474: `publish --bump patch` -- `patch` is `--bump`'s value,
        not a subcommand of the `publish` group."""
        text = _workflow(
            "      - run: ${{ env.HYPERCI_INSTALL }} publish --bump patch\n"
        )
        assert _gaps(text) == []

    def test_a_fused_short_option_s_value_is_not_skipped_again(self) -> None:
        """`-bpatch` already carries its value; the next word must still be
        read, not swallowed as a second value."""
        text = _workflow(
            "      - run: ${{ env.HYPERCI_INSTALL }} publish -bpatch frobnicate\n"
        )
        assert [g.split(": ", 1)[1] for g in _gaps(text)] == [
            "calls `hyperi-ci publish frobnicate`, absent from the published CLI"
        ]

    def test_an_equals_form_value_is_not_skipped_again(self) -> None:
        """`--bump=patch` carries its value inline; the next word must still
        be read, not swallowed as a second value."""
        text = _workflow(
            "      - run: ${{ env.HYPERCI_INSTALL }} publish --bump=patch frobnicate\n"
        )
        assert [g.split(": ", 1)[1] for g in _gaps(text)] == [
            "calls `hyperi-ci publish frobnicate`, absent from the published CLI"
        ]

    def test_a_nested_command_checks_its_own_options(self) -> None:
        text = _workflow(
            "      - run: ${{ env.HYPERCI_INSTALL }} publish binaries --no-dry-run\n"
            "      - run: ${{ env.HYPERCI_INSTALL }} publish binaries --tier x\n"
        )
        assert [g.split(": ", 1)[1] for g in _gaps(text)] == [
            "`hyperi-ci publish binaries` passes `--tier`, which the published "
            "CLI does not accept"
        ]

    def test_a_missing_subcommand_is_reported(self) -> None:
        text = _workflow("      - run: ${{ env.HYPERCI_INSTALL }} gate-check\n")
        assert [g.split(": ", 1)[1] for g in _gaps(text)] == [
            "calls `hyperi-ci gate-check`, absent from the published CLI"
        ]

    def test_a_missing_nested_subcommand_is_reported(self) -> None:
        text = _workflow("      - run: ${{ env.HYPERCI_INSTALL }} publish docs\n")
        assert [g.split(": ", 1)[1] for g in _gaps(text)] == [
            "calls `hyperi-ci publish docs`, absent from the published CLI"
        ]

    def test_a_hidden_command_counts_as_published(self) -> None:
        """`--help` hides `tag-head`, so the table must not come from scraping it."""
        text = _workflow(
            '      - run: ${{ env.HYPERCI_INSTALL }} tag-head --bump "$V"\n'
        )
        assert _gaps(text) == []

    def test_a_subcommand_from_an_expression_is_not_silently_passed(self) -> None:
        text = _workflow(
            "      - run: ${{ env.HYPERCI_INSTALL }} ${{ inputs.command }}\n"
        )
        assert [g.split(": ", 1)[1] for g in _gaps(text)] == [
            "the subcommand after `hyperi-ci` comes from an expression, so the "
            "gate cannot check it"
        ]

    def test_every_location_is_named(self) -> None:
        text = _workflow(
            "      - run: ${{ env.HYPERCI_INSTALL }} new-thing\n"
            "      - run: ${{ env.HYPERCI_INSTALL }} new-thing\n"
        )
        assert len(_gaps(text)) == 2


class TestTheOptionTable:
    def test_parses_the_dump(self) -> None:
        table = cwi.parse_option_table(
            '{"": {"--help": false}, "run": {"--tier": true, "-C": true}}'
        )
        assert table == {
            "": cwi.CommandOptions(
                names=frozenset({"--help"}), value_taking=frozenset()
            ),
            "run": cwi.CommandOptions(
                names=frozenset({"--tier", "-C"}),
                value_taking=frozenset({"--tier", "-C"}),
            ),
        }

    @pytest.mark.parametrize(
        "text",
        [
            "[]",
            '{"run": "--tier"}',
            '{"run": ["--tier"]}',
            '{"run": {"--tier": "yes"}}',
            "not json",
        ],
    )
    def test_rejects_anything_else(self, text: str) -> None:
        with pytest.raises(ValueError):
            cwi.parse_option_table(text)


class TestWhatCountsAsPublished:
    """uv answers "what is the latest" from a cached index, PyPI does not."""

    def test_an_unreachable_pypi_returns_none(self, fake_urlopen: _Urlopen) -> None:
        """None is the skip path; a wrong version would fail a clean PR."""
        assert cwi.latest_published_version() is None

    def test_a_malformed_payload_returns_none(self, fake_urlopen: _Urlopen) -> None:
        outcomes, _, _ = fake_urlopen
        outcomes.append(b"{}")
        assert cwi.latest_published_version() is None

    def test_the_version_comes_from_pypi(self, fake_urlopen: _Urlopen) -> None:
        outcomes, _, _ = fake_urlopen
        outcomes.append(b'{"info": {"version": "9.9.9"}}')
        assert cwi.latest_published_version() == "9.9.9"

    def test_one_5xx_is_retried_not_skipped(self, fake_urlopen: _Urlopen) -> None:
        """A single PyPI 503 must not turn the gate into a skip."""
        outcomes, asked, _ = fake_urlopen
        outcomes.append(
            urllib.error.HTTPError(cwi._PYPI_JSON, 503, "busy", Message(), None)
        )
        outcomes.append(b'{"info": {"version": "9.9.9"}}')
        assert cwi.latest_published_version() == "9.9.9"
        assert len(asked) == 2

    def test_an_unresolvable_version_skips_rather_than_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cwi, "latest_published_version", lambda: None)
        assert cwi.published_cli_options() is None


class _FakeRun:
    """Stand-in for `subprocess.run`, always returning one fixed result."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self._result = subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr=stderr
        )

    def __call__(self, *args: object, **kwargs: object) -> subprocess.CompletedProcess:
        return self._result


class TestTheDumpFailureDistinction:
    """issue #474: tell "uvx could not fetch the wheel" apart from "the dump
    snippet ran and raised" -- only the second must fail the gate."""

    def test_a_wheel_install_failure_skips(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No marker in stderr means uvx itself failed -- that is a skip."""
        monkeypatch.setattr(cwi, "latest_published_version", lambda: "9.9.9")
        monkeypatch.setattr(
            cwi.subprocess,
            "run",
            _FakeRun(returncode=1, stderr="no matching distribution"),
        )
        assert cwi.published_cli_options() is None

    def test_a_snippet_failure_raises_not_skips(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cwi, "latest_published_version", lambda: "9.9.9")
        monkeypatch.setattr(
            cwi.subprocess,
            "run",
            _FakeRun(
                returncode=cwi._DUMP_FAILURE_CODE,
                stderr=f"{cwi._DUMP_FAILURE_MARKER}: AttributeError('is_flag')",
            ),
        )
        with pytest.raises(cwi.CliDumpError, match="is_flag"):
            cwi.published_cli_options()

    def test_an_exit_code_collision_without_the_marker_still_skips(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """uv could exit 3 for its own unrelated reason -- only the marker
        proves the snippet ran and raised."""
        monkeypatch.setattr(cwi, "latest_published_version", lambda: "9.9.9")
        monkeypatch.setattr(
            cwi.subprocess,
            "run",
            _FakeRun(returncode=cwi._DUMP_FAILURE_CODE, stderr=""),
        )
        assert cwi.published_cli_options() is None

    def test_unparseable_output_on_success_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exit 0 with output the gate cannot parse is a dump bug, not a skip."""
        monkeypatch.setattr(cwi, "latest_published_version", lambda: "9.9.9")
        monkeypatch.setattr(
            cwi.subprocess, "run", _FakeRun(returncode=0, stdout="not json")
        )
        with pytest.raises(cwi.CliDumpError):
            cwi.published_cli_options()

    def test_a_clean_run_returns_the_table(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cwi, "latest_published_version", lambda: "9.9.9")
        monkeypatch.setattr(
            cwi.subprocess,
            "run",
            _FakeRun(returncode=0, stdout='{"": {"--help": false}}'),
        )
        assert cwi.published_cli_options() == {
            "": cwi.CommandOptions(
                names=frozenset({"--help"}), value_taking=frozenset()
            )
        }


_RETIREMENT = """
retired:
  - file: .github/workflows/go-ci.yml
    kind: secret
    name: JFROG_TOKEN
    reason: Nothing reads it and no caller passes it.
    checked: 2026-09-23
"""


def _entry(**overrides: object) -> str:
    """A retirement file with one entry; a None override drops that field."""
    fields: dict[str, object] = {
        "file": ".github/workflows/go-ci.yml",
        "kind": "secret",
        "name": "JFROG_TOKEN",
        "reason": "Nothing reads it and no caller passes it.",
        "checked": "2026-09-23",
    }
    fields.update(overrides)
    body = "\n".join(f"    {k}: {v}" for k, v in fields.items() if v is not None)
    return f"retired:\n  -\n{body}\n"


class TestTheCliGateOnADumpFailure:
    """issue #474: done when a snippet that raises fails the gate (exit 1)."""

    def test_a_dump_failure_fails_the_gate(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(
            cwi, "workflow_cli_invocations", lambda root: cwi.InvocationScan((), ())
        )

        def _raise() -> dict[str, cwi.CommandOptions]:
            raise cwi.CliDumpError("the option dump raised: AttributeError('is_flag')")

        monkeypatch.setattr(cwi, "published_cli_options", _raise)
        assert cwi._cli_gate() == 1
        assert "is_flag" in capsys.readouterr().out

    def test_an_unreachable_pypi_still_skips(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(
            cwi, "workflow_cli_invocations", lambda root: cwi.InvocationScan((), ())
        )
        monkeypatch.setattr(cwi, "published_cli_options", lambda: None)
        assert cwi._cli_gate() == 0
        assert "unreachable -- skipping" in capsys.readouterr().out


class TestRetirementRecords:
    """An interface nothing consumes must be removable without a major bump.

    The gate compares interface to interface, so every removal reads as a
    regression. A record clears one, and carries the evidence that earned it.
    """

    def test_a_listed_removal_is_not_a_regression(self) -> None:
        old = {
            "kind": "workflow",
            "inputs": {},
            "secrets": {"JFROG_TOKEN": {"required": False}},
            "outputs": set(),
        }
        new = {"kind": "workflow", "inputs": {}, "secrets": {}, "outputs": set()}
        retired = frozenset({("secret", "JFROG_TOKEN")})
        assert cwi.breaking_deltas(old, new, retired) == []

    def test_an_unlisted_removal_in_the_same_file_still_fails(self) -> None:
        """Clearing one member must not clear its neighbours."""
        old = {
            "kind": "workflow",
            "inputs": {},
            "secrets": {
                "JFROG_TOKEN": {"required": False},
                "NPM_TOKEN": {"required": False},
            },
            "outputs": set(),
        }
        new = {"kind": "workflow", "inputs": {}, "secrets": {}, "outputs": set()}
        deltas = cwi.breaking_deltas(old, new, frozenset({("secret", "JFROG_TOKEN")}))
        assert deltas == ["secret 'NPM_TOKEN' removed"]

    def test_a_record_does_not_clear_a_different_kind(self) -> None:
        """A retired secret must not excuse an input of the same name."""
        old = {
            "kind": "workflow",
            "inputs": {"TOKEN": {"required": False, "has_default": True}},
            "secrets": {},
            "outputs": set(),
        }
        new = {"kind": "workflow", "inputs": {}, "secrets": {}, "outputs": set()}
        deltas = cwi.breaking_deltas(old, new, frozenset({("secret", "TOKEN")}))
        assert len(deltas) == 1
        assert "input 'TOKEN'" in deltas[0]

    def test_a_valid_record_parses(self) -> None:
        records = cwi.parse_retirements(_RETIREMENT)
        assert len(records) == 1
        assert records[0].file == ".github/workflows/go-ci.yml"
        assert records[0].member == ("secret", "JFROG_TOKEN")
        assert records[0].checked == "2026-09-23"

    @pytest.mark.parametrize("field", ["reason", "checked", "file", "kind", "name"])
    def test_a_record_missing_a_field_is_rejected(self, field: str) -> None:
        """No reason means no evidence, which is a bypass, not a retirement."""
        with pytest.raises(ValueError, match=field):
            cwi.parse_retirements(_entry(**{field: None}))

    def test_an_empty_reason_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="reason"):
            cwi.parse_retirements(_entry(reason='"   "'))

    def test_an_unknown_kind_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="parameter"):
            cwi.parse_retirements(_entry(kind="parameter"))

    def test_an_unknown_field_is_rejected(self) -> None:
        """Catches a typo that would otherwise drop the field it meant."""
        with pytest.raises(ValueError, match="resaon"):
            cwi.parse_retirements(_entry(resaon="typo"))

    def test_a_non_list_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="list"):
            cwi.parse_retirements("retired: {}")

    def test_an_absent_file_means_no_retirements(self, tmp_path: Path) -> None:
        """Checking everything is the safe direction to fail in."""
        assert cwi.load_retirements(tmp_path / "nope.yaml") == []


class TestRetirementState:
    """Every record is reported, so a gate never quietly stops checking."""

    def _record(self) -> object:
        return cwi.Retirement(
            file=".github/workflows/go-ci.yml",
            kind="secret",
            name="JFROG_TOKEN",
            reason="Nothing reads it.",
            checked="2026-09-23",
        )

    def _iface(self, *secrets: str) -> dict:
        return {
            "kind": "workflow",
            "inputs": {},
            "secrets": {s: {"required": False} for s in secrets},
            "outputs": set(),
        }

    def test_still_declared_is_pending(self) -> None:
        state = cwi.retirement_state(
            self._record(), self._iface("JFROG_TOKEN"), self._iface("JFROG_TOKEN")
        )
        assert state == "pending"

    def test_gone_from_the_tree_only_is_retired(self) -> None:
        state = cwi.retirement_state(
            self._record(), self._iface(), self._iface("JFROG_TOKEN")
        )
        assert state == "retired"

    def test_gone_from_both_is_prunable(self) -> None:
        """Once a release ships without it the record stops doing anything."""
        state = cwi.retirement_state(self._record(), self._iface(), self._iface())
        assert state == "prunable"

    def test_a_file_with_no_interface_counts_as_absent(self) -> None:
        assert cwi.retirement_state(self._record(), None, None) == "prunable"


class TestTheShippedRetirementFile:
    """The repo's own records must parse and point at real declarations."""

    def test_it_parses(self) -> None:
        """Empty is a valid state: a record is pruned once its removal ships."""
        assert isinstance(cwi.load_retirements(cwi._RETIREMENTS), list)

    def test_every_record_names_a_file_that_exists(self) -> None:
        """A typo'd path reads as prunable, which would hide the mistake."""
        root = Path(cwi._ROOT)
        for record in cwi.load_retirements(cwi._RETIREMENTS):
            assert (root / record.file).is_file(), record.file

    def test_no_record_is_still_declared(self) -> None:
        """A pending record pre-authorises a removal nobody has made yet."""
        root = Path(cwi._ROOT)
        for record in cwi.load_retirements(cwi._RETIREMENTS):
            iface = cwi.parse_interface(
                (root / record.file).read_text(encoding="utf-8")
            )
            assert not cwi._declares(iface, record.kind, record.name), record.name
