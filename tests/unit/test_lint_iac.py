# Project:   HyperI CI
# File:      tests/unit/test_lint_iac.py
# Purpose:   Tests for the lint-iac orchestrator, its CLI verbs and its gates
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Tests for hyperi_ci.quality.lint_iac.

The orchestration tests stub the dimension runners and pin order, isolation and
exit codes. ``TestGatesBlock`` is the in-repo counterpart of a ``.ci-negative``
case: each plants one defect in a tree, runs the real tool, and asserts the
gate FAILS. Each skips where its tool is not installed.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyperi_ci.cli import app
from hyperi_ci.config import CIConfig
from hyperi_ci.quality import checkov, lint_iac
from hyperi_ci.quality import findings as fdg


def _cfg(raw: dict | None = None) -> CIConfig:
    return CIConfig(_raw=raw or {})


def _not_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL", "BUILDKITE"):
        monkeypatch.delenv(name, raising=False)


_RANDOM_SECRET = (
    "apiVersion: v1\nkind: Secret\nmetadata:\n  name: s\n"
    "stringData:\n  pw: {{ randAlphaNum 12 | quote }}\n"
)


def _chart(root: Path, template: str | None = None) -> Path:
    chart = root / "c"
    (chart / "templates").mkdir(parents=True)
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: c\nversion: 0.1.0\n", encoding="utf-8"
    )
    if template is not None:
        (chart / "templates" / "t.yaml").write_text(template, encoding="utf-8")
    return chart


@pytest.fixture
def stubbed(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace every runner with a recorder; ``rcs`` sets a dimension's exit code."""
    state: dict = {"order": [], "rcs": {}}

    def _make(name: str):  # noqa: ANN202
        def _runner(ctx: object) -> tuple[int, int]:
            state["order"].append(name)
            state["ctx"] = ctx
            return 1, state["rcs"].get(name, 0)

        return _runner

    runners = {name: _make(name) for name in lint_iac.DIMENSIONS}
    monkeypatch.setattr(lint_iac, "_RUNNERS", runners)
    return state


class TestOrchestration:
    def test_every_dimension_runs_in_order(self, stubbed: dict, tmp_path: Path) -> None:
        assert lint_iac.run(tmp_path, _cfg()) == 0
        assert stubbed["order"] == list(lint_iac.DIMENSIONS)

    def test_a_failing_gate_fails_the_run_and_the_rest_still_run(
        self, stubbed: dict, tmp_path: Path
    ) -> None:
        stubbed["rcs"]["helm"] = 1
        assert lint_iac.run(tmp_path, _cfg()) == 1
        assert stubbed["order"] == list(lint_iac.DIMENSIONS)

    def test_a_subset_runs_only_those(self, stubbed: dict, tmp_path: Path) -> None:
        lint_iac.run(tmp_path, _cfg(), dimensions=lint_iac.COMPOSE_DIMENSIONS)
        assert stubbed["order"] == ["compose"]

    def test_an_unknown_dimension_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="nope"):
            lint_iac.run(tmp_path, _cfg(), dimensions=("helm", "nope"))

    def test_runs_from_the_root_and_restores_the_cwd(
        self, stubbed: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[Path] = []
        stubbed_runners = dict(lint_iac._RUNNERS)
        stubbed_runners["helm"] = lambda ctx: (seen.append(Path.cwd()), (0, 0))[1]
        monkeypatch.setattr(lint_iac, "_RUNNERS", stubbed_runners)
        before = Path.cwd()
        lint_iac.run(tmp_path, _cfg(), dimensions=("helm",))
        assert seen == [tmp_path.resolve()]
        assert Path.cwd() == before

    def test_findings_are_counted_per_dimension(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _two(ctx: object) -> tuple[int, int]:
            finding = fdg.Finding("t", "p", None, "warning", "r", "m")
            fdg.surface("t", [finding, finding])
            return 1, 0

        monkeypatch.setattr(lint_iac, "_RUNNERS", {**lint_iac._RUNNERS, "helm": _two})
        captured: list[list[lint_iac.Outcome]] = []
        monkeypatch.setattr(lint_iac, "_report", captured.append)
        lint_iac.run(tmp_path, _cfg(), dimensions=("helm",))
        assert captured[0][0].findings == 2


class TestManifestDiscoveryRunsOnce:
    def test_manifests_and_kube_linter_share_one_discovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[Path] = []

        def _discover(root: Path, *, exclude_dirs: object = ()) -> list[Path]:
            calls.append(root)
            return []

        monkeypatch.setattr(lint_iac, "discover_manifests", _discover)
        monkeypatch.setattr(lint_iac.kube_linter, "run", lambda *a, **k: 0)
        lint_iac.run(tmp_path, _cfg(), dimensions=("manifests", "kube-linter"))
        assert len(calls) == 1


class TestGuardrailSettings:
    def test_timeout_and_memory_come_from_config(
        self, stubbed: dict, tmp_path: Path
    ) -> None:
        cfg = _cfg({"iac": {"timeout_seconds": 30, "memory_limit_mb": 512}})
        lint_iac.run(tmp_path, cfg, dimensions=("helm",))
        assert stubbed["ctx"].timeout == 30
        assert stubbed["ctx"].memory_limit_bytes == 512 * 1024 * 1024

    @pytest.mark.parametrize("bad", [0, -5, "ten", True, 1.5])
    def test_a_bad_timeout_falls_back_to_the_default(
        self, stubbed: dict, tmp_path: Path, bad: object
    ) -> None:
        lint_iac.run(
            tmp_path, _cfg({"iac": {"timeout_seconds": bad}}), dimensions=("helm",)
        )
        assert stubbed["ctx"].timeout == 600


class TestGeneratedEntries:
    def test_string_command_is_split_without_a_shell(self) -> None:
        entries, problems = lint_iac.generated_entries(
            [{"paths": ["a.json"], "command": "python3 gen.py --out 'a b'"}]
        )
        assert problems == []
        assert entries[0].command == ("python3", "gen.py", "--out", "a b")

    @pytest.mark.parametrize(
        "raw",
        [
            "not a list",
            [{"command": "x"}],
            [{"paths": [], "command": "x"}],
            [{"paths": ["a"], "command": ""}],
            [{"paths": ["a"], "command": [1]}],
            ["just a string"],
        ],
    )
    def test_malformed_entries_are_problems(self, raw: object) -> None:
        entries, problems = lint_iac.generated_entries(raw)
        assert entries == []
        assert problems

    def test_unset_is_nothing(self) -> None:
        assert lint_iac.generated_entries(None) == ([], [])


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _repo(tmp_path: Path, out: str) -> Path:
    (tmp_path / "gen.py").write_text(
        "from pathlib import Path\nPath('out.txt').write_text('v2\\n')\n",
        encoding="utf-8",
    )
    (tmp_path / "out.txt").write_text(out, encoding="utf-8")
    return _committed(tmp_path)


class TestGenerated:
    @pytest.mark.parametrize(
        ("committed", "command", "mode", "rc"),
        [
            ("v1\n", "python3 gen.py", "blocking", 1),  # stale
            ("v2\n", "python3 gen.py", "blocking", 0),  # current
            ("v1\n", "python3 gen.py", "warn", 0),
            ("v1\n", "false", "blocking", 1),  # the command fails
        ],
    )
    def test_regenerates_in_scratch_and_gates_on_a_diff(
        self, tmp_path: Path, committed: str, command: str, mode: str, rc: int
    ) -> None:
        root = _repo(tmp_path, committed)
        before = _tree_state(root)
        entry = {"paths": ["out.txt"], "command": command}
        cfg = _cfg({"quality": {"iac_generated": mode}, "iac": {"generated": [entry]}})
        assert lint_iac.run(root, cfg, dimensions=("generated",)) == rc
        assert _tree_state(root) == before

    def test_a_malformed_entry_fails_whatever_the_mode(self, tmp_path: Path) -> None:
        cfg = _cfg({"quality": {"iac_generated": "warn"}, "iac": {"generated": [{}]}})
        assert lint_iac.run(tmp_path, cfg, dimensions=("generated",)) == 1


class TestMissingTools:
    @pytest.mark.parametrize(
        ("ci", "kubeconform", "rc"),
        [(False, "blocking", 0), (True, "blocking", 1), (True, "warn", 0)],
    )
    def test_missing_helm_fails_only_a_blocking_gate_in_ci(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        ci: bool,
        kubeconform: str,
        rc: int,
    ) -> None:
        monkeypatch.setattr(lint_iac, "ci_binary", lambda name: None)
        _not_ci(monkeypatch)
        if ci:
            monkeypatch.setenv("CI", "true")
        _chart(tmp_path)
        cfg = _cfg({"quality": {"kubeconform": kubeconform}})
        assert lint_iac.run(tmp_path, cfg, dimensions=("helm",)) == rc


class TestCli:
    def test_lint_iac_is_a_verb(self) -> None:
        result = CliRunner().invoke(app, ["lint-iac", "--help"])
        assert result.exit_code == 0

    @pytest.mark.parametrize("flag", ["-C", "--project-dir"])
    def test_project_dir_names_the_root(
        self, flag: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        roots: list[Path] = []
        monkeypatch.setattr(
            lint_iac, "run", lambda root, cfg, **kw: roots.append(root) or 0
        )
        result = CliRunner().invoke(app, ["lint-iac", flag, str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert roots == [tmp_path]

    def test_project_dir_and_a_different_directory_are_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        roots: list[Path] = []
        monkeypatch.setattr(
            lint_iac, "run", lambda root, cfg, **kw: roots.append(root) or 0
        )
        other = tmp_path / "other"
        result = CliRunner().invoke(app, ["lint-iac", str(other), "-C", str(tmp_path)])
        assert result.exit_code == 2
        assert roots == []

    @pytest.mark.parametrize(
        ("verb", "dims"),
        [
            ("lint-manifests", lint_iac.MANIFEST_DIMENSIONS),
            ("lint-compose", lint_iac.COMPOSE_DIMENSIONS),
        ],
    )
    def test_deprecated_verbs_run_their_slice_with_one_notice(
        self,
        verb: str,
        dims: tuple[str, ...],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[tuple[str, ...]] = []
        notices: list[str] = []
        monkeypatch.setattr(
            lint_iac, "run", lambda root, cfg, **kw: calls.append(kw["dimensions"]) or 0
        )
        monkeypatch.setattr(
            "hyperi_ci.common.announce", lambda msg, title, **kw: notices.append(msg)
        )
        result = CliRunner().invoke(app, [verb, str(tmp_path)])
        assert result.exit_code == 0
        assert calls == [dims]
        assert len(notices) == 1
        assert "lint-iac" in notices[0]


needs = {
    tool: pytest.mark.skipif(shutil.which(tool) is None, reason=f"{tool} not installed")
    for tool in ("tofu", "helm", "kubeconform", "kube-linter")
}


# No securityContext and no resources, so kube-linter's defaults must fire.
_BARE_DEPLOYMENT = (
    "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n"
    "spec:\n  selector:\n    matchLabels: {app: web}\n  template:\n"
    "    metadata:\n      labels: {app: web}\n    spec:\n"
    "      containers:\n        - name: web\n          image: nginx:1.27\n"
)


class TestKubeLinterReports:
    """kube-linter run through lint-iac never reports a silent zero."""

    @pytest.fixture(autouse=True)
    def _local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _not_ci(monkeypatch)
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    @staticmethod
    def _found(
        monkeypatch: pytest.MonkeyPatch, root: Path, dims: tuple[str, ...]
    ) -> list[fdg.Finding]:
        seen: list[fdg.Finding] = []
        real = fdg.surface

        def _record(
            tool: str,
            found: list[fdg.Finding],
            *,
            sarif_path: str | Path | None = None,
        ) -> int:
            if tool == "kube-linter":
                seen.extend(found)
            return real(tool, found, sarif_path=sarif_path)

        monkeypatch.setattr(fdg, "surface", _record)
        cfg = _cfg({"quality": {"kubeconform": "warn"}})
        lint_iac.run(root, cfg, dimensions=dims)
        return seen

    @needs["kube-linter"]
    def test_a_plain_manifest_gets_the_default_checks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "deploy.yaml").write_text(_BARE_DEPLOYMENT, encoding="utf-8")
        found = self._found(monkeypatch, tmp_path, ("kube-linter",))
        assert "run-as-non-root" in [f.rule for f in found]

    @needs["helm"]
    @needs["kube-linter"]
    def test_a_chart_with_no_values_file_is_linted_as_rendered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _chart(tmp_path, _BARE_DEPLOYMENT)
        found = self._found(monkeypatch, tmp_path, ("helm", "kube-linter"))
        assert "run-as-non-root" in [f.rule for f in found]
        assert "kube-linter/load-failed" not in [f.rule for f in found]
        assert {f.path for f in found} == {"c"}

    @needs["kube-linter"]
    def test_a_chart_kube_linter_cannot_load_is_a_finding(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _chart(tmp_path, _BARE_DEPLOYMENT)
        found = self._found(monkeypatch, tmp_path, ("kube-linter",))
        assert [f.rule for f in found] == ["kube-linter/load-failed"]


class TestGatesBlock:
    """Each case plants one defect and asserts the gate fails on it."""

    @pytest.fixture(autouse=True)
    def _local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _not_ci(monkeypatch)

    @needs["tofu"]
    @pytest.mark.parametrize(
        ("text", "rc"),
        [
            ('variable "x" {\ndefault="a"\n}\n', 1),  # fmt drift
            ('output "x" {\n  value = var.missing\n}\n', 1),  # invalid reference
            (
                'variable "x" {\n  default = "a"\n}\n\noutput "x" {\n  value = var.x\n}\n',
                0,
            ),
        ],
    )
    def test_tofu_gate(self, tmp_path: Path, text: str, rc: int) -> None:
        (tmp_path / "main.tf").write_text(text, encoding="utf-8")
        assert lint_iac.run(tmp_path, _cfg(), dimensions=("tofu",)) == rc
        assert not (tmp_path / ".terraform").exists()
        assert not (tmp_path / ".terraform.lock.hcl").exists()

    @needs["helm"]
    @needs["kubeconform"]
    @pytest.mark.parametrize(
        ("quality", "rc"),
        [({}, 1), ({"render_stable": "warn", "kubeconform": "warn"}, 0)],
    )
    def test_unstable_chart_render(
        self, tmp_path: Path, quality: dict, rc: int
    ) -> None:
        _chart(tmp_path, _RANDOM_SECRET)
        cfg = _cfg({"quality": quality})
        assert lint_iac.run(tmp_path, cfg, dimensions=("helm",)) == rc

    @needs["kubeconform"]
    def test_unknown_field_fails_under_strict(self, tmp_path: Path) -> None:
        (tmp_path / "cm.yaml").write_text(
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n"
            "data:\n  a: b\nnotAField: true\n",
            encoding="utf-8",
        )
        assert lint_iac.run(tmp_path, _cfg(), dimensions=("manifests",)) == 1
        relaxed = _cfg(
            {"quality": {"kubeconform": {"mode": "blocking", "strict": False}}}
        )
        assert lint_iac.run(tmp_path, relaxed, dimensions=("manifests",)) == 0
        as_text = _cfg({"quality": {"kubeconform": {"strict": "no"}}})
        assert lint_iac.run(tmp_path, as_text, dimensions=("manifests",)) == 0

    @needs["kubeconform"]
    def test_a_manifest_no_kustomization_lists_is_still_validated(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "apps").mkdir()
        (tmp_path / "kustomization.yaml").write_text(
            "resources:\n  - apps/a.yaml\n", encoding="utf-8"
        )
        deployment = (
            "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {name}\n"
            "spec:\n  replicas: {replicas}\n  selector:\n    matchLabels: {{a: b}}\n"
            "  template:\n    metadata:\n      labels: {{a: b}}\n    spec:\n"
            "      containers:\n        - name: c\n          image: x:1\n"
        )
        (tmp_path / "apps" / "a.yaml").write_text(
            deployment.format(name="a", replicas=1), encoding="utf-8"
        )
        (tmp_path / "apps" / "b.yaml").write_text(
            deployment.format(name="b", replicas='"three"'), encoding="utf-8"
        )
        assert lint_iac.run(tmp_path, _cfg(), dimensions=("manifests",)) == 1
        (tmp_path / "apps" / "b.yaml").unlink()
        assert lint_iac.run(tmp_path, _cfg(), dimensions=("manifests",)) == 0

    @needs["helm"]
    def test_helm_test_hooks_do_not_count_as_drift(self, tmp_path: Path) -> None:
        _chart(
            tmp_path,
            "apiVersion: v1\nkind: Pod\nmetadata:\n"
            "  name: t-{{ randAlphaNum 5 | lower }}\n"
            "  annotations: {helm.sh/hook: test}\n"
            "spec:\n  containers:\n    - name: t\n      image: x:1\n",
        )
        cfg = _cfg({"quality": {"kubeconform": "warn"}})
        assert lint_iac.run(tmp_path, cfg, dimensions=("helm",)) == 0


def _tree_state(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--ignored"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout


def _committed(root: Path) -> Path:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "tree")
    return root


def _two_charts(root: Path) -> None:
    """Lay out an app chart that pulls a library chart in through ``file://``."""
    lib = root / "charts-src" / "lib"
    (lib / "templates").mkdir(parents=True)
    (lib / "Chart.yaml").write_text(
        "apiVersion: v2\nname: lib\nversion: 0.1.0\ntype: library\n", encoding="utf-8"
    )
    (lib / "templates" / "_helpers.tpl").write_text(
        '{{- define "lib.name" -}}lib{{- end -}}\n', encoding="utf-8"
    )
    app = root / "charts-src" / "app"
    (app / "templates").mkdir(parents=True)
    (app / "Chart.yaml").write_text(
        "apiVersion: v2\nname: app\nversion: 0.1.0\ndependencies:\n"
        "  - name: lib\n    version: 0.1.0\n    repository: file://../lib\n",
        encoding="utf-8",
    )
    (app / "templates" / "cm.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n"
        '  name: {{ include "lib.name" . }}\n',
        encoding="utf-8",
    )


class _FakeCheckov:
    """Stands in for checkov, building Helm deps in the dir it scans as checkov does."""

    def __call__(self, cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        scanned = Path(cmd[cmd.index("-d") + 1])
        out_dir = Path(cmd[cmd.index("--output-file-path") + 1])
        app = scanned / "charts-src" / "app"
        (app / "Chart.lock").write_text("generated: now\n", encoding="utf-8")
        (app / "charts").mkdir()
        (app / "charts" / "lib-0.1.0.tgz").write_bytes(b"tgz")
        result = {
            "ruleId": "CKV_K8S_21",
            "level": "error",
            "message": {"text": "The default namespace should not be used"},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {
                            "uri": "tmpab12cd34/charts-src/app/templates/cm.yaml"
                        },
                        "region": {"startLine": 1},
                    }
                }
            ],
        }
        sarif = {"runs": [{"tool": {"driver": {"rules": []}}, "results": [result]}]}
        (out_dir / "results_sarif.sarif").write_text(
            json.dumps(sarif), encoding="utf-8"
        )
        return subprocess.CompletedProcess(cmd, 0, "", "")


class TestTreeUnchanged:
    """lint-iac writes nothing into the tree it lints, ignored files included."""

    @pytest.fixture(autouse=True)
    def _local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _not_ci(monkeypatch)

    @needs["helm"]
    def test_a_missing_chart_dependency_is_built_in_scratch(
        self, tmp_path: Path
    ) -> None:
        for name, deps in (
            ("lib", ""),
            (
                "app",
                "dependencies:\n  - name: lib\n    version: 0.1.0\n"
                "    repository: file://../lib\n",
            ),
        ):
            chart = tmp_path / "charts-src" / name
            (chart / "templates").mkdir(parents=True)
            (chart / "Chart.yaml").write_text(
                f"apiVersion: v2\nname: {name}\nversion: 0.1.0\n{deps}",
                encoding="utf-8",
            )
            (chart / "templates" / "cm.yaml").write_text(
                f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {name}\n",
                encoding="utf-8",
            )
        root = _committed(tmp_path)
        before = _tree_state(root)
        cfg = _cfg({"quality": {"kubeconform": "warn"}})
        assert lint_iac.run(root, cfg, dimensions=("helm",)) == 0
        assert _tree_state(root) == before
        assert not (root / "charts-src" / "app" / "Chart.lock").exists()

    @needs["tofu"]
    def test_tofu_init_writes_nothing_beside_the_module(self, tmp_path: Path) -> None:
        (tmp_path / "modules" / "m").mkdir(parents=True)
        (tmp_path / "modules" / "m" / "main.tf").write_text(
            'variable "x" {\n  default = "a"\n}\n', encoding="utf-8"
        )
        (tmp_path / "env").mkdir()
        (tmp_path / "env" / "main.tf").write_text(
            'module "m" {\n  source = "../modules/m" # local\n}\n', encoding="utf-8"
        )
        root = _committed(tmp_path)
        before = _tree_state(root)
        assert lint_iac.run(root, _cfg(), dimensions=("tofu",)) == 0
        assert _tree_state(root) == before

    @needs["tofu"]
    def test_a_file_the_module_reads_from_its_parent_is_staged(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "worker").mkdir()
        (tmp_path / "worker" / "index.ts").write_text("x\n", encoding="utf-8")
        (tmp_path / "terraform").mkdir()
        (tmp_path / "terraform" / "main.tf").write_text(
            'output "h" {\n  value = filemd5("${path.module}/../worker/index.ts")\n}\n',
            encoding="utf-8",
        )
        root = _committed(tmp_path)
        before = _tree_state(root)
        assert lint_iac.run(root, _cfg(), dimensions=("tofu",)) == 0
        assert _tree_state(root) == before

    def test_checkov_writes_its_helm_dependency_build_into_a_copy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _two_charts(tmp_path)
        root = _committed(tmp_path)
        before = _tree_state(root)
        monkeypatch.setattr(checkov, "_base_cmd", lambda: ["checkov"])
        monkeypatch.setattr(checkov, "run_cmd", _FakeCheckov())
        surfaced: list[fdg.Finding] = []
        monkeypatch.setattr(
            fdg, "surface", lambda tool, found, **kw: surfaced.extend(found) or 0
        )

        assert lint_iac.run(root, _cfg(), dimensions=("checkov",)) == 0
        assert _tree_state(root) == before
        assert [f.path for f in surfaced] == ["charts-src/app/templates/cm.yaml"]

    def test_checkov_scans_a_copy_outside_git_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _two_charts(tmp_path)
        monkeypatch.setattr(checkov, "_base_cmd", lambda: ["checkov"])
        monkeypatch.setattr(checkov, "run_cmd", _FakeCheckov())
        assert lint_iac.run(tmp_path, _cfg(), dimensions=("checkov",)) == 0
        assert not (tmp_path / "charts-src" / "app" / "Chart.lock").exists()

    @pytest.mark.slow
    @needs["helm"]
    @pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
    def test_real_checkov_leaves_the_tree_and_names_the_template(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _two_charts(tmp_path)
        root = _committed(tmp_path)
        before = _tree_state(root)
        surfaced: list[fdg.Finding] = []
        monkeypatch.setattr(
            fdg, "surface", lambda tool, found, **kw: surfaced.extend(found) or 0
        )

        assert lint_iac.run(root, _cfg(), dimensions=("checkov",)) == 0
        assert _tree_state(root) == before
        assert surfaced
        assert all((root / f.path).is_file() for f in surfaced)

    @pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
    def test_ansible_installs_and_lints_without_touching_the_tree(
        self, tmp_path: Path
    ) -> None:
        project = tmp_path / "ansible"
        (project / "roles").mkdir(parents=True)
        (project / "playbooks").mkdir()
        (project / "ansible.cfg").write_text(
            "[defaults]\nroles_path = roles\ncollections_path = ./collections\n",
            encoding="utf-8",
        )
        (project / "requirements.yml").write_text("collections: []\n", encoding="utf-8")
        (project / "playbooks" / "site.yml").write_text(
            "---\n- name: Site\n  hosts: all\n  tasks:\n"
            "    - name: Ping\n      ansible.builtin.ping:\n",
            encoding="utf-8",
        )
        root = _committed(tmp_path)
        before = _tree_state(root)
        lint_iac.run(root, _cfg(), dimensions=("ansible",))
        assert _tree_state(root) == before
