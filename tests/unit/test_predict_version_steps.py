# Project:   HyperI CI
# File:      tests/unit/test_predict_version_steps.py
# Purpose:   Run the predict-version gate steps for real, per event
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Execute the composite's gate, derive and tier steps under bash.

String assertions on the step body cannot show what a scheduled run decides.
These render each step's ``${{ }}`` expressions for one event and run it in a
throwaway repo whose HEAD carries ``Release: true``, so a schedule branch that
fell through to the trailer check would publish and fail the test.

``python3`` on the step's PATH is this interpreter by default. The config
readers run on whatever the composite's ``reader`` step picks, and a step that
reads its answer runs the real ``reader`` step first under the same PATH and
environment. :class:`TestAPython3WithoutPyYAML` puts a python3 with no PyYAML
first on the PATH to prove the answer no longer depends on the runner's own,
and :class:`TestUvCannotSupplyPyYAML` breaks uv to prove Plan still completes.
"""

import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from hyperi_ci.versions import tool_version

ACTION_DIR = (
    Path(__file__).resolve().parents[2] / ".github" / "actions" / "predict-version"
)
_EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None,
    reason="needs bash and git",
)

# The config readers run under uv. Tests that reach one need it on the PATH.
needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")

# HOME moves into the throwaway repo, so uv would otherwise start a cold cache
# and fetch PyYAML afresh for every test.
_UV_CACHE = (
    subprocess.run(
        ["uv", "cache", "dir"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    ).stdout.strip()
    if shutil.which("uv")
    else ""
)

#: The composite's own input default, which versions.yaml keeps in step.
_INPUTS = {"inputs.pyyaml-version": tool_version("pyyaml")}

#: The interpreter the reader step hands every config-reading step.
_READER = "steps.reader.outputs.python"

_MAIN = "refs/heads/main"
#: The central default.releaserc.json declares beta a prerelease branch.
_BETA = "refs/heads/beta"
_FEATURE = "refs/heads/feature/x"


def _steps() -> dict[str, dict]:
    action = yaml.safe_load((ACTION_DIR / "action.yml").read_text(encoding="utf-8"))
    return {s["id"]: s for s in action["runs"]["steps"] if "id" in s}


def _render(text: str, values: dict[str, str]) -> str:
    def lookup(match: re.Match[str]) -> str:
        expression = match.group(1)
        if expression not in values:
            raise KeyError(f"no test value for ${{{{ {expression} }}}}")
        return values[expression]

    return _EXPRESSION.sub(lookup, text)


def _python3_shim(repo: Path) -> Path:
    shim_dir = repo / ".shim"
    shim_dir.mkdir(exist_ok=True)
    shim = shim_dir / "python3"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    return shim_dir


def _run_step(
    step_id: str,
    values: dict[str, str],
    repo: Path,
    env: dict[str, str] | None = None,
    path_first: Path | None = None,
    returncode: int = 0,
) -> tuple[dict[str, str], str]:
    values = {**_INPUTS, **values}
    step = _steps()[step_id]
    if _READER in str(step.get("env")) and _READER not in values:
        reader, _ = _run_step("reader", {}, repo, env=env, path_first=path_first)
        values[_READER] = reader["python"]
    script = _render(str(step["run"]), values)
    output = repo / ".github_output"
    output.write_text("", encoding="utf-8")
    step_env = {
        key: _render(str(val), values) for key, val in (step.get("env") or {}).items()
    }
    result = subprocess.run(
        ["bash", "-e", "-c", script],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={
            "PATH": os.pathsep.join(
                [
                    str(path_first or _python3_shim(repo)),
                    os.environ.get("PATH", ""),
                ]
            ),
            "HOME": str(repo),
            **({"UV_CACHE_DIR": _UV_CACHE} if _UV_CACHE else {}),
            "GITHUB_OUTPUT": str(output),
            "GITHUB_ACTION_PATH": str(ACTION_DIR),
            "GITHUB_WORKSPACE": str(repo),
            # The runner sets GITHUB_REF to github.ref, and helpers read it.
            **({"GITHUB_REF": values["github.ref"]} if "github.ref" in values else {}),
            **step_env,
            **(env or {}),
        },
        check=False,
    )
    assert result.returncode == returncode, result.stdout + result.stderr
    written = dict(
        line.split("=", 1)
        for line in output.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    return written, result.stdout


@pytest.fixture
def released_head(tmp_path: Path) -> Path:
    """A repo on main whose HEAD commit carries the release trailer."""

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(tmp_path),
                "GIT_CONFIG_NOSYSTEM": "1",
            },
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "ci@example.invalid")
    git("config", "user.name", "CI")
    (tmp_path / "README").write_text("x\n", encoding="utf-8")
    git("add", "README")
    git("commit", "-q", "-m", "fix: a thing\n\nRelease: true")
    return tmp_path


def _gate_run(
    event: str,
    repo: Path,
    *,
    ref: str = "refs/heads/main",
    tag: str = "",
    from_head: str = "",
    bump: str = "auto",
) -> tuple[dict[str, str], str]:
    return _run_step(
        "gate",
        {
            "github.event_name": event,
            "github.ref": ref,
            "inputs.tag": tag,
            "inputs.from-head": from_head,
            "inputs.bump": bump,
        },
        repo,
    )


def _gate(event: str, repo: Path) -> dict[str, str]:
    return _gate_run(event, repo)[0]


def _derive(event: str, will_publish: str, repo: Path) -> dict[str, str]:
    return _run_step(
        "derive",
        {
            "steps.gate.outputs.will-publish": will_publish,
            "github.event_name": event,
            "inputs.branch-build": "",
            "github.ref": "refs/heads/main",
            "steps.worthy.outputs.release-worthy": "",
            "steps.predict.outputs.version || steps.firstparent.outputs.version || steps.forced.outputs.version || steps.tagged.outputs.version": "",
        },
        repo,
    )[0]


def _tier(
    event: str,
    will_publish: str,
    requested: str,
    repo: Path,
    path_first: Path | None = None,
) -> dict[str, str]:
    return _run_step(
        "tier",
        {
            "github.event_name": event,
            "steps.gate.outputs.will-publish": will_publish,
            "inputs.test-tier": requested,
        },
        repo,
        path_first=path_first,
    )[0]


def _targets(repo: Path, path_first: Path | None = None) -> tuple[str, str]:
    written, stdout = _run_step("targets", {}, repo, path_first=path_first)
    return written["rust-targets"], stdout


def test_the_steps_python3_is_this_interpreter(tmp_path: Path) -> None:
    result = subprocess.run(
        ["python3", "-c", "import sys, yaml; print(sys.executable)"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={
            "PATH": f"{_python3_shim(tmp_path)}{os.pathsep}{os.environ.get('PATH', '')}"
        },
        check=True,
    )
    assert result.stdout.strip() == sys.executable


class TestAScheduledRun:
    """A schedule tests at full and never builds or publishes."""

    def test_a_push_with_the_trailer_publishes(self, released_head: Path) -> None:
        # The control: this repo WOULD publish on a push, so the schedule
        # case below is proving something.
        assert _gate("push", released_head)["will-publish"] == "true"

    def test_a_schedule_never_publishes_whatever_the_trailer(
        self, released_head: Path
    ) -> None:
        assert _gate("schedule", released_head)["will-publish"] == "false"

    def test_a_schedule_runs_checks_and_no_build(self, released_head: Path) -> None:
        outputs = _derive("schedule", "false", released_head)
        assert outputs["run-checks"] == "true"
        assert outputs["run-build"] == "false"
        assert outputs["run-arm64-check"] == "false"

    def test_a_schedule_runs_full(self, released_head: Path) -> None:
        assert _tier("schedule", "false", "core", released_head)["test-tier"] == "full"


class TestAMergeQueueEntry:
    """A queue entry is what main fast-forwards to, so it runs the checks."""

    def test_a_queue_entry_runs_checks_and_no_build(self, released_head: Path) -> None:
        outputs = _derive("merge_group", "false", released_head)
        assert outputs["run-checks"] == "true"
        assert outputs["run-build"] == "false"


class TestTheOtherEvents:
    def test_a_pr_runs_core(self, released_head: Path) -> None:
        assert (
            _tier("pull_request", "false", "core", released_head)["test-tier"] == "core"
        )

    def test_a_release_runs_core_until_the_repo_opts_in(
        self, released_head: Path
    ) -> None:
        outputs = _tier("push", "true", "core", released_head)
        assert outputs["test-tier"] == "core"
        assert outputs["full-required-for-release"] == "false"

    def test_an_opted_in_release_runs_full(self, released_head: Path) -> None:
        (released_head / ".hyperi-ci.yaml").write_text(
            "test:\n  full:\n    required_for_release: true\n", encoding="utf-8"
        )
        outputs = _tier("push", "true", "core", released_head)
        assert outputs["test-tier"] == "full"
        assert outputs["full-required-for-release"] == "true"

    def test_a_dispatch_asking_for_full_runs_full(self, released_head: Path) -> None:
        outputs = _tier("workflow_dispatch", "false", "full", released_head)
        assert outputs["test-tier"] == "full"

    def test_a_non_worthy_push_still_skips_the_checks(
        self, released_head: Path
    ) -> None:
        # The schedule clause must not widen run-checks for anything else.
        outputs = _derive("push", "false", released_head)
        assert outputs["run-checks"] == "false"


class TestATagDispatch:
    """A `tag` dispatch re-publishes the tag's own version (issue #352)."""

    def test_a_tag_dispatch_publishes(self, released_head: Path) -> None:
        outputs, _ = _gate_run("workflow_dispatch", released_head, tag="v1.0.4")
        assert outputs["will-publish"] == "true"

    def test_a_tag_dispatch_publishes_from_a_feature_branch(
        self, released_head: Path
    ) -> None:
        # The tag already names the commit, so the dispatch ref does not matter.
        outputs, _ = _gate_run(
            "workflow_dispatch", released_head, ref=_FEATURE, tag="v1.0.4"
        )
        assert outputs["will-publish"] == "true"

    def test_the_version_is_the_tags_own(self, released_head: Path) -> None:
        # The tree says 1.0.3, as a tagged commit does before the commit-back.
        (released_head / "VERSION").write_text("1.0.3\n", encoding="utf-8")
        outputs, _ = _run_step("tagged", {"inputs.tag": "v1.0.4"}, released_head)
        assert outputs == {"version": "1.0.4"}

    def test_a_prerelease_tag_keeps_its_label(self, released_head: Path) -> None:
        outputs, _ = _run_step("tagged", {"inputs.tag": "v1.2.0-beta.1"}, released_head)
        assert outputs == {"version": "1.2.0-beta.1"}

    def test_a_tag_that_names_no_version_fails(self, released_head: Path) -> None:
        output = released_head / ".github_output"
        output.write_text("", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(ACTION_DIR / "tag_version.py")],
            cwd=released_head,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={"RELEASE_TAG": "1.0.4", "GITHUB_OUTPUT": str(output)},
            check=False,
        )
        assert result.returncode == 1
        assert "::error" in result.stdout
        assert output.read_text(encoding="utf-8") == ""

    def test_only_a_tag_dispatch_runs_the_step(self) -> None:
        condition = str(_steps()["tagged"]["if"])
        assert "steps.gate.outputs.will-publish == 'true'" in condition
        assert "github.event_name == 'workflow_dispatch'" in condition
        assert "inputs.tag != ''" in condition

    def test_the_version_output_reads_the_step(self) -> None:
        action = yaml.safe_load((ACTION_DIR / "action.yml").read_text(encoding="utf-8"))
        assert "steps.tagged.outputs.version" in action["outputs"]["version"]["value"]
        assert "steps.tagged.outputs.version" in str(_steps()["derive"]["env"])


class TestAFromHeadDispatch:
    """A from-head dispatch releases only from a release branch (issue #471).

    beta is the central config's declared prerelease branch. A forced bump
    is cut by tag-head through the GitHub API, which ignores the release
    config, so the gate is the only thing that can refuse it.
    """

    @pytest.mark.parametrize(
        ("ref", "bump"),
        [
            (_MAIN, "auto"),
            (_MAIN, "patch"),
            (_MAIN, "2.0.1"),
            (_BETA, "auto"),
        ],
    )
    def test_a_release_branch_publishes(
        self, released_head: Path, ref: str, bump: str
    ) -> None:
        outputs, stdout = _gate_run(
            "workflow_dispatch", released_head, ref=ref, from_head="true", bump=bump
        )
        assert outputs == {"will-publish": "true"}
        assert "::warning" not in stdout

    @pytest.mark.parametrize("bump", ["auto", "patch", "minor", "2.0.1"])
    def test_a_feature_branch_publishes_nothing(
        self, released_head: Path, bump: str
    ) -> None:
        outputs, stdout = _gate_run(
            "workflow_dispatch",
            released_head,
            ref=_FEATURE,
            from_head="true",
            bump=bump,
        )
        assert outputs == {"will-publish": "false"}
        assert f"::warning::from-head dispatch on ref '{_FEATURE}'" in stdout
        assert "validate-only" in stdout

    @pytest.mark.parametrize("bump", ["patch", "minor", "2.0.1"])
    def test_a_forced_bump_on_a_prerelease_branch_publishes_nothing(
        self, released_head: Path, bump: str
    ) -> None:
        # The forced step computes a plain X.Y.Z, so it would cut a stable
        # release off beta.
        outputs, stdout = _gate_run(
            "workflow_dispatch", released_head, ref=_BETA, from_head="true", bump=bump
        )
        assert outputs == {"will-publish": "false"}
        assert f"::warning::Forced bump '{bump}' on prerelease branch" in stdout
        assert "bump=auto" in stdout

    def test_the_repo_config_names_the_prerelease_branch(
        self, released_head: Path
    ) -> None:
        (released_head / ".releaserc.json").write_text(
            '{"branches": ["main", {"name": "next", "prerelease": true}],'
            ' "plugins": ["@semantic-release/exec"]}\n',
            encoding="utf-8",
        )
        next_ref, _ = _gate_run(
            "workflow_dispatch",
            released_head,
            ref="refs/heads/next",
            from_head="true",
        )
        beta_ref, _ = _gate_run(
            "workflow_dispatch", released_head, ref=_BETA, from_head="true"
        )
        assert next_ref == {"will-publish": "true"}
        assert beta_ref == {"will-publish": "false"}

    def test_a_tag_does_not_unlock_from_head_on_a_feature_branch(
        self, released_head: Path
    ) -> None:
        # tag-head runs on from-head alone, so a tag beside it must not
        # carry a forced bump past the branch rule.
        outputs, _ = _gate_run(
            "workflow_dispatch",
            released_head,
            ref=_FEATURE,
            tag="v1.0.4",
            from_head="true",
            bump="patch",
        )
        assert outputs == {"will-publish": "false"}


class TestAForcedBump:
    """The dispatch's `bump` input is data, never shell."""

    def test_an_explicit_version_is_used_verbatim(self, released_head: Path) -> None:
        outputs, _ = _run_step("forced", {"inputs.bump": "2.0.1"}, released_head)
        assert outputs == {"version": "2.0.1"}

    def test_a_quote_in_the_input_runs_nothing(self, released_head: Path) -> None:
        subprocess.run(
            ["git", "tag", "v1.0.0"],
            cwd=released_head,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        planted = released_head / "planted"
        bump = f"patch'; touch {planted}; echo '"
        _, stdout = _run_step(
            "forced", {"inputs.bump": bump}, released_head, returncode=1
        )
        assert not planted.exists()
        assert "::error::Invalid bump" in stdout


_SEMREL_GATED = "steps.firstparent.outputs.first-parent != 'true'"


def _fork(repo: Path) -> Path:
    (repo / ".hyperi-ci.yaml").write_text("classification: fork\n", encoding="utf-8")
    return repo


@needs_uv
class TestAForkRelease:
    """A fork versions from its first-parent commits, and nothing else does."""

    def test_a_sync_merge_of_upstream_feat_gives_a_patch(
        self, tmp_path: Path, make_fork_history: Callable[..., Path]
    ) -> None:
        repo = _fork(make_fork_history(tmp_path))
        outputs, stdout = _run_step("firstparent", {}, repo)
        assert outputs == {"first-parent": "true", "version": "0.2.9"}
        assert "::notice title=fork release::Predicted next version: v0.2.9" in stdout

    def test_a_repo_that_is_not_a_fork_is_left_to_semantic_release(
        self, tmp_path: Path, make_fork_history: Callable[..., Path]
    ) -> None:
        repo = make_fork_history(tmp_path)
        outputs, stdout = _run_step("firstparent", {}, repo)
        assert outputs == {"first-parent": "false"}
        assert stdout == ""

    def test_a_fork_with_nothing_of_its_own_fails(
        self, tmp_path: Path, make_fork_history: Callable[..., Path]
    ) -> None:
        repo = _fork(make_fork_history(tmp_path))
        for args in (
            ["tag", "v0.2.9"],
            ["commit", "--allow-empty", "-q", "-m", "docs: x"],
        ):
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
        outputs, stdout = _run_step("firstparent", {}, repo, returncode=1)
        assert outputs == {}
        assert stdout.startswith("::error title=fork release::No release-worthy")

    def test_a_misspelt_classification_warns_and_falls_back(
        self, tmp_path: Path, make_fork_history: Callable[..., Path]
    ) -> None:
        repo = make_fork_history(tmp_path)
        (repo / ".hyperi-ci.yaml").write_text(
            "classification: frok\n", encoding="utf-8"
        )
        outputs, stdout = _run_step("firstparent", {}, repo)
        assert outputs == {"first-parent": "false"}
        assert stdout.startswith("::warning title=fork release::Unknown classification")

    def test_the_step_runs_only_for_a_stable_release_from_main(self) -> None:
        condition = str(_steps()["firstparent"]["if"])
        assert "steps.gate.outputs.will-publish == 'true'" in condition
        assert "github.ref == 'refs/heads/main'" in condition
        assert "inputs.bump == 'auto'" in condition

    def test_semantic_release_stands_down_for_a_fork(self) -> None:
        action = yaml.safe_load((ACTION_DIR / "action.yml").read_text(encoding="utf-8"))
        steps = action["runs"]["steps"]
        names = [s.get("name", "") for s in steps]
        first_parent = [s.get("id") for s in steps].index("firstparent")
        setup = names.index(
            "Setup semantic-release (shared toolchain -- single source of truth)"
        )
        assert first_parent < setup
        assert _SEMREL_GATED in str(steps[setup]["if"])
        assert _SEMREL_GATED in str(_steps()["predict"]["if"])

    def test_the_version_output_reads_the_step(self) -> None:
        action = yaml.safe_load((ACTION_DIR / "action.yml").read_text(encoding="utf-8"))
        expected = "steps.firstparent.outputs.version"
        assert expected in action["outputs"]["version"]["value"]
        assert expected in str(_steps()["derive"]["env"])


_AMD64 = "x86_64-unknown-linux-gnu"
_ARM64 = "aarch64-unknown-linux-gnu"


@pytest.fixture(scope="module")
def bare_python3(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A bin directory whose python3 has no PyYAML, as on the ARC vanilla image."""
    if shutil.which("uv") is None:
        pytest.skip("needs uv")
    venv = tmp_path_factory.mktemp("bare") / "venv"
    subprocess.run(
        ["uv", "venv", "--quiet", "--python", sys.executable, str(venv)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    bin_dir = venv / ("Scripts" if os.name == "nt" else "bin")
    probe = subprocess.run(
        [str(bin_dir / "python3"), "-c", "import yaml"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    # Guards the guard: a python3 that has PyYAML proves nothing below.
    assert probe.returncode != 0, "the bare python3 can import yaml"
    # Shadow any yq on the host too, or the fallback answers in PyYAML's place.
    fake_yq = bin_dir / "yq"
    fake_yq.write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
    fake_yq.chmod(fake_yq.stat().st_mode | stat.S_IXUSR)
    return bin_dir


def _rust_repo(repo: Path, config: str) -> Path:
    (repo / "Cargo.toml").write_text(
        '[package]\nname = "thing"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (repo / ".hyperi-ci.yaml").write_text(config, encoding="utf-8")
    return repo


@needs_uv
class TestAPython3WithoutPyYAML:
    """Every config read in Plan comes out right on a python3 with no PyYAML.

    The ARC vanilla image has python3 but neither PyYAML nor yq. Before the
    readers ran under uv, the tier there came out core for a repo asking for
    full, the arm64 check read nothing, and the Rust matrix built every target
    (issue #291). ``UV_PYTHON_PREFERENCE=only-system`` makes uv take that
    python3 as the base, so the PyYAML the step asks for is what reads the file.
    """

    ENV = {"UV_PYTHON_PREFERENCE": "only-system"}

    def _step(
        self, step_id: str, values: dict[str, str], repo: Path, bare: Path
    ) -> tuple[dict[str, str], str]:
        return _run_step(step_id, values, repo, env=self.ENV, path_first=bare)

    def test_the_project_tier_is_read(
        self, released_head: Path, bare_python3: Path
    ) -> None:
        (released_head / ".hyperi-ci.yaml").write_text(
            "test:\n  tier: full\n", encoding="utf-8"
        )
        outputs, stdout = self._step(
            "tier",
            {
                "github.event_name": "pull_request",
                "steps.gate.outputs.will-publish": "false",
                "inputs.test-tier": "",
            },
            released_head,
            bare_python3,
        )
        assert outputs["test-tier"] == "full", stdout
        assert "test.tier: full" in stdout

    def test_the_release_opt_in_is_read(
        self, released_head: Path, bare_python3: Path
    ) -> None:
        (released_head / ".hyperi-ci.yaml").write_text(
            "test:\n  full:\n    required_for_release: true\n", encoding="utf-8"
        )
        outputs, _ = self._step(
            "tier",
            {
                "github.event_name": "push",
                "steps.gate.outputs.will-publish": "true",
                "inputs.test-tier": "",
            },
            released_head,
            bare_python3,
        )
        assert outputs == {"test-tier": "full", "full-required-for-release": "true"}

    def test_a_tier_typo_still_fails(
        self, released_head: Path, bare_python3: Path
    ) -> None:
        # A per-helper `uv run ... || python3 ...` re-ran this on the bare
        # python3, which cannot read the file, and passed on core.
        (released_head / ".hyperi-ci.yaml").write_text(
            "test:\n  tier: fulll\n", encoding="utf-8"
        )
        _, stdout = _run_step(
            "tier",
            {
                "github.event_name": "pull_request",
                "steps.gate.outputs.will-publish": "false",
                "inputs.test-tier": "",
            },
            released_head,
            env=self.ENV,
            path_first=bare_python3,
            returncode=1,
        )
        assert stdout.startswith("::error title=test tier::test.tier")
        assert "::warning" not in stdout

    def test_a_fork_declared_in_the_config_is_read(
        self,
        tmp_path: Path,
        bare_python3: Path,
        make_fork_history: Callable[..., Path],
    ) -> None:
        repo = _fork(make_fork_history(tmp_path))
        outputs, _ = self._step("firstparent", {}, repo, bare_python3)
        assert outputs == {"first-parent": "true", "version": "0.2.9"}

    def test_the_rust_targets_are_read(
        self, released_head: Path, bare_python3: Path
    ) -> None:
        _rust_repo(
            released_head,
            f"build:\n  rust:\n    targets:\n      - {_AMD64}\n",
        )
        outputs, stdout = self._step("targets", {}, released_head, bare_python3)
        assert outputs == {"rust-targets": _AMD64}
        assert f"::notice title=rust targets::{_AMD64}" in stdout

    @pytest.mark.parametrize(
        ("targets", "expected"),
        [([_AMD64], "false"), ([_AMD64, _ARM64], "true")],
    )
    def test_the_arm64_check_reads_the_targets(
        self,
        released_head: Path,
        bare_python3: Path,
        targets: list[str],
        expected: str,
    ) -> None:
        listed = "".join(f"      - {target}\n" for target in targets)
        _rust_repo(released_head, f"build:\n  rust:\n    targets:\n{listed}")
        outputs, _ = self._step(
            "derive",
            {
                "steps.gate.outputs.will-publish": "false",
                "github.event_name": "push",
                "inputs.branch-build": "",
                "github.ref": "refs/heads/main",
                "steps.worthy.outputs.release-worthy": "true",
                "steps.predict.outputs.version || steps.firstparent.outputs.version || steps.forced.outputs.version || steps.tagged.outputs.version": "",
            },
            released_head,
            bare_python3,
        )
        assert outputs["run-arm64-check"] == expected


@needs_uv
class TestUvCannotSupplyPyYAML:
    """Plan completes when uv cannot supply PyYAML, and says so.

    A PyPI, astral or GitHub download blip must not fail Plan for every
    consumer. ``offline`` is uv with no network and an empty cache; ``no-uv``
    is the setup-uv step having failed. Either way the reader step falls back
    to the runner's python3 and warns, and each helper then warns that the
    config could not be read on a python3 that has neither PyYAML nor yq.
    """

    CONFIG = (
        "test:\n  tier: full\n"
        f"build:\n  rust:\n    targets:\n      - {_AMD64}\n      - {_ARM64}\n"
    )

    @pytest.fixture(params=["offline", "no-uv"])
    def broken_uv(
        self,
        request: pytest.FixtureRequest,
        tmp_path_factory: pytest.TempPathFactory,
        bare_python3: Path,
    ) -> dict[str, str]:
        env = {"UV_PYTHON_PREFERENCE": "only-system"}
        scratch = tmp_path_factory.mktemp(request.param)
        if request.param == "offline":
            env |= {"UV_OFFLINE": "1", "UV_CACHE_DIR": str(scratch)}
        else:
            fake_uv = scratch / "uv"
            fake_uv.write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
            fake_uv.chmod(fake_uv.stat().st_mode | stat.S_IXUSR)
            env["PATH"] = os.pathsep.join(
                [str(scratch), str(bare_python3), os.environ.get("PATH", "")]
            )
        # Guards the guard: a uv that can still supply PyYAML proves nothing.
        # The probe sees what the step sees, and no VIRTUAL_ENV with PyYAML.
        probe_env = {
            "PATH": os.pathsep.join([str(bare_python3), os.environ.get("PATH", "")]),
            "HOME": str(scratch),
            **env,
        }
        uv = shutil.which("uv", path=probe_env["PATH"])
        assert uv is not None
        probe = subprocess.run(
            [uv, "run", "--no-project", "--no-config", "--python", "3", "--with"]
            + [f"pyyaml=={tool_version('pyyaml')}", "python", "-c", "import yaml"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=probe_env,
            # Away from this checkout's .venv, which uv would otherwise use.
            cwd=scratch,
            check=False,
        )
        assert probe.returncode != 0, "uv could still supply PyYAML"
        return env

    def test_the_reader_falls_back_to_python3_and_warns(
        self, released_head: Path, bare_python3: Path, broken_uv: dict[str, str]
    ) -> None:
        outputs, stdout = _run_step(
            "reader", {}, released_head, env=broken_uv, path_first=bare_python3
        )
        assert outputs == {"python": "python3"}
        assert stdout.startswith(
            "::warning title=config readers::uv could not provide PyYAML"
        )

    def test_the_tier_step_completes_and_warns(
        self, released_head: Path, bare_python3: Path, broken_uv: dict[str, str]
    ) -> None:
        (released_head / ".hyperi-ci.yaml").write_text(self.CONFIG, encoding="utf-8")
        outputs, stdout = _run_step(
            "tier",
            {
                "github.event_name": "pull_request",
                "steps.gate.outputs.will-publish": "false",
                "inputs.test-tier": "",
            },
            released_head,
            env=broken_uv,
            path_first=bare_python3,
        )
        assert outputs == {"test-tier": "core", "full-required-for-release": "false"}
        assert "::warning title=test tier core::" in stdout
        assert ".hyperi-ci.yaml could not be read" in stdout

    def test_the_targets_step_completes_and_warns(
        self, released_head: Path, bare_python3: Path, broken_uv: dict[str, str]
    ) -> None:
        _rust_repo(released_head, self.CONFIG)
        outputs, stdout = _run_step(
            "targets", {}, released_head, env=broken_uv, path_first=bare_python3
        )
        assert outputs == {"rust-targets": ""}
        assert "::warning title=rust targets::.hyperi-ci.yaml could not be read" in (
            stdout
        )
        assert "every target builds" in stdout

    def test_the_arm64_check_completes_and_stays_off(
        self, released_head: Path, bare_python3: Path, broken_uv: dict[str, str]
    ) -> None:
        _rust_repo(released_head, self.CONFIG)
        outputs, _ = _run_step(
            "derive",
            {
                "steps.gate.outputs.will-publish": "false",
                "github.event_name": "push",
                "inputs.branch-build": "",
                "github.ref": "refs/heads/main",
                "steps.worthy.outputs.release-worthy": "true",
                "steps.predict.outputs.version || steps.firstparent.outputs.version || steps.forced.outputs.version || steps.tagged.outputs.version": "",
            },
            released_head,
            env=broken_uv,
            path_first=bare_python3,
        )
        assert outputs["run-checks"] == "true"
        assert outputs["run-arm64-check"] == "false"


@needs_uv
class TestTheRustTargetsStep:
    """What the Rust matrix is handed, and when it warns."""

    def test_no_list_means_every_target_and_says_nothing(
        self, released_head: Path
    ) -> None:
        _rust_repo(released_head, "language: rust\n")
        assert _targets(released_head) == ("", "")

    def test_the_list_is_space_separated(self, released_head: Path) -> None:
        _rust_repo(
            released_head,
            f"build:\n  rust:\n    targets:\n      - {_AMD64}\n      - {_ARM64}\n",
        )
        assert _targets(released_head)[0] == f"{_AMD64} {_ARM64}"

    def test_another_language_gets_an_empty_list_and_no_warning(
        self, released_head: Path
    ) -> None:
        # Only rust-ci.yml reads this, so a python repo's broken config is
        # the tier step's to report, not this one's.
        (released_head / ".hyperi-ci.yaml").write_text("x: [\n", encoding="utf-8")
        assert _targets(released_head) == ("", "")

    def test_an_unreadable_config_warns_and_builds_every_target(
        self, released_head: Path
    ) -> None:
        _rust_repo(released_head, "build: [unclosed\n")
        targets, stdout = _targets(released_head)
        assert targets == ""
        assert stdout.startswith("::warning title=rust targets::.hyperi-ci.yaml")
        assert "every target builds" in stdout
        assert len(stdout.splitlines()) == 1

    def test_a_list_that_is_not_a_list_warns(self, released_head: Path) -> None:
        _rust_repo(released_head, f"build:\n  rust:\n    targets: {_AMD64}\n")
        targets, stdout = _targets(released_head)
        assert targets == ""
        assert "build.rust.targets must be a list" in stdout
