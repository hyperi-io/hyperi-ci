# Project:   HyperI CI
# File:      tests/unit/test_charts.py
# Purpose:   Tests for publishing committed Helm charts to an OCI registry
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from hyperi_ci import config as config_module
from hyperi_ci.cli import app
from hyperi_ci.config import CIConfig, load_config
from hyperi_ci.release import binaries, charts

DIGEST = "sha256:" + "ab" * 32
OTHER = "sha256:" + "cd" * 32
REGISTRY = "oci://ghcr.io/hyperi-io/charts"

needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not on PATH")


def _chart(root: Path, rel: str, name: str, **extra: object) -> Path:
    path = root / rel
    (path / "templates").mkdir(parents=True)
    body = {"apiVersion": "v2", "name": name, "version": "0.0.0", **extra}
    (path / "Chart.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")
    (path / "templates" / "cm.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n", encoding="utf-8"
    )
    return path


def _packaged_chart_yaml(tgz: Path, name: str) -> dict:
    with tarfile.open(tgz) as archive:
        member = archive.extractfile(f"{name}/Chart.yaml")
        assert member is not None
        return yaml.safe_load(member.read())


def _enabled(charts_list: list[str]) -> CIConfig:
    return CIConfig(
        _raw={"release": {"helm": {"enabled": True, "charts": charts_list}}}
    )


@pytest.fixture
def dfe_infra_shape(tmp_path: Path) -> Path:
    """Two app charts depending on a library chart through file://."""
    _chart(tmp_path, "helm/library/common", "common", type="library")
    dep = {
        "name": "common",
        "version": "0.0.0",
        "repository": "file://../../library/common",
    }
    _chart(tmp_path, "helm/charts/api", "api", dependencies=[dep])
    _chart(tmp_path, "helm/charts/web", "web", appVersion="1.4.2")
    return tmp_path


class TestResolveCharts:
    def test_a_glob_expands_to_every_chart_dir(self, dfe_infra_shape: Path) -> None:
        found = charts.resolve_charts(["helm/charts/*"], dfe_infra_shape)
        assert [c.name for c in found] == ["api", "web"]

    def test_the_file_dependency_is_carried(self, dfe_infra_shape: Path) -> None:
        api = charts.resolve_charts(["helm/charts/api"], dfe_infra_shape)[0]
        assert api.has_deps
        assert api.file_deps == ((dfe_infra_shape / "helm/library/common").resolve(),)

    def test_a_library_chart_is_skipped(self, dfe_infra_shape: Path) -> None:
        found = charts.resolve_charts(["helm/**/*"], dfe_infra_shape)
        assert "common" not in [c.name for c in found]

    def test_a_chart_listed_twice_is_published_once(
        self, dfe_infra_shape: Path
    ) -> None:
        found = charts.resolve_charts(
            ["helm/charts/web", "helm/charts/*"], dfe_infra_shape
        )
        assert [c.name for c in found] == ["web", "api"]

    def test_an_explicit_dir_without_chart_yaml_fails(self, tmp_path: Path) -> None:
        (tmp_path / "deploy").mkdir()
        with pytest.raises(charts.ChartError, match="has no Chart.yaml"):
            charts.resolve_charts(["deploy"], tmp_path)

    def test_a_glob_matching_nothing_fails(self, tmp_path: Path) -> None:
        with pytest.raises(charts.ChartError, match="no application chart"):
            charts.resolve_charts(["helm/charts/*"], tmp_path)

    def test_only_library_charts_fails(self, dfe_infra_shape: Path) -> None:
        with pytest.raises(charts.ChartError, match="no application chart"):
            charts.resolve_charts(["helm/library/*"], dfe_infra_shape)

    def test_a_file_dependency_outside_the_repo_is_refused(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        _chart(tmp_path, "outside", "outside")
        dep = {
            "name": "outside",
            "version": "0.0.0",
            "repository": "file://../../../outside",
        }
        _chart(repo, "charts/app", "app", dependencies=[dep])
        with pytest.raises(charts.ChartError, match="resolves outside"):
            charts.resolve_charts(["charts/app"], repo)


@needs_helm
class TestPackageWithRealHelm:
    def test_app_version_is_set_only_when_missing(
        self, dfe_infra_shape: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        scratch = tmp_path_factory.mktemp("scratch")
        found = charts.resolve_charts(["helm/charts/*"], dfe_infra_shape)
        packaged = {
            c.name: charts.package(c, "2.2.0-rc.14", dfe_infra_shape, scratch)
            for c in found
        }

        api = _packaged_chart_yaml(packaged["api"], "api")
        web = _packaged_chart_yaml(packaged["web"], "web")
        assert api["version"] == "2.2.0-rc.14"
        assert api["appVersion"] == "v2.2.0-rc.14"
        assert web["version"] == "2.2.0-rc.14"
        assert web["appVersion"] == "1.4.2"

    def test_the_file_dependency_is_bundled(
        self, dfe_infra_shape: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        scratch = tmp_path_factory.mktemp("scratch")
        api = charts.resolve_charts(["helm/charts/api"], dfe_infra_shape)[0]
        tgz = charts.package(api, "1.0.0", dfe_infra_shape, scratch)
        with tarfile.open(tgz) as archive:
            assert "api/charts/common/Chart.yaml" in archive.getnames()

    def test_the_checkout_is_left_untouched(self, dfe_infra_shape: Path) -> None:
        def git(*args: str) -> str:
            return subprocess.run(
                ["git", *args],
                cwd=dfe_infra_shape,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            ).stdout

        git("init", "-q")
        git("add", "-A")
        git("-c", "user.name=t", "-c", "user.email=t@t.io", "commit", "-qm", "seed")
        before = git("status", "--porcelain", "--ignored")

        rc, results = charts.publish_charts(
            _enabled(["helm/charts/*"]),
            dfe_infra_shape,
            registry=REGISTRY,
            version="1.0.0",
            dry_run=True,
        )

        assert rc == 0
        assert [r.chart for r in results] == ["api", "web"]
        assert git("status", "--porcelain", "--ignored") == before == ""

    def test_cli_json_output_is_pure_json(self, dfe_infra_shape: Path) -> None:
        result = CliRunner().invoke(
            app,
            [
                "publish-charts",
                "-C",
                str(dfe_infra_shape),
                "--charts",
                "helm/charts/api",
                "--charts",
                "helm/charts/web",
                "--registry",
                REGISTRY,
                "--version",
                "2.2.1",
                "--dry-run",
                "--output",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == [
            {
                "chart": n,
                "version": "2.2.1",
                "digest": None,
                "ref": None,
                "signed": False,
            }
            for n in ("api", "web")
        ]


class FakeHelm:
    """Stands in for the registry half of helm and records each call."""

    def __init__(self, existing: dict[str, str] | None = None) -> None:
        self.existing = existing or {}
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str, registry: str = "") -> tuple[int, str]:
        self.calls.append(args)
        if args[:2] == ("show", "chart"):
            name = args[2].rsplit("/", 1)[-1]
            if name in self.existing and "--devel" not in args:
                return 0, f"Pulled: x\nDigest: {self.existing[name]}\napiVersion: v2\n"
            return 1, "Error: not found"
        if args[0] == "push":
            return 0, f"Pushed: ghcr.io/hyperi-io/charts/x:1.0.0\nDigest: {DIGEST}\n"
        raise AssertionError(f"unexpected helm call {args}")

    @property
    def pushes(self) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c[0] == "push"]


@pytest.fixture
def fake_helm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeHelm:
    fake = FakeHelm()
    monkeypatch.setattr(charts, "_helm", fake)
    monkeypatch.setattr(charts, "_ensure_helm", lambda: True)
    monkeypatch.setattr(
        charts,
        "package",
        lambda chart, version, root, scratch: tmp_path / f"{chart.name}.tgz",
    )
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("RUNNER_TEMP", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return fake


class TestPublish:
    def test_disabled_does_nothing(self, fake_helm: FakeHelm, tmp_path: Path) -> None:
        rc, results = charts.publish_charts(
            CIConfig(_raw={}), tmp_path, version="1.0.0"
        )
        assert (rc, results, fake_helm.calls) == (0, [], [])

    def test_the_digest_comes_from_the_push_output(
        self, fake_helm: FakeHelm, dfe_infra_shape: Path
    ) -> None:
        rc, results = charts.publish_charts(
            _enabled(["helm/charts/web"]),
            dfe_infra_shape,
            registry=REGISTRY,
            version="1.0.0",
        )
        assert rc == 0
        assert results[0].digest == DIGEST
        assert results[0].ref == f"ghcr.io/hyperi-io/charts/web@{DIGEST}"
        assert results[0].signed is False

    def test_an_existing_version_is_reused_not_pushed(
        self, fake_helm: FakeHelm, dfe_infra_shape: Path
    ) -> None:
        fake_helm.existing = {"web": OTHER}
        rc, results = charts.publish_charts(
            _enabled(["helm/charts/*"]),
            dfe_infra_shape,
            registry=REGISTRY,
            version="1.0.0",
        )
        assert rc == 0
        assert {r.chart: r.digest for r in results} == {"api": DIGEST, "web": OTHER}
        assert [Path(p[1]).name for p in fake_helm.pushes] == ["api.tgz"]

    def test_dry_run_never_pushes(
        self, fake_helm: FakeHelm, dfe_infra_shape: Path
    ) -> None:
        rc, results = charts.publish_charts(
            _enabled(["helm/charts/*"]),
            dfe_infra_shape,
            registry=REGISTRY,
            version="1.0.0",
            dry_run=True,
        )
        assert rc == 0
        assert [r.digest for r in results] == [None, None]
        assert fake_helm.calls == []

    def test_the_version_flag_is_used_verbatim(
        self,
        fake_helm: FakeHelm,
        dfe_infra_shape: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HYPERCI_VERSION", "9.9.9")
        _, results = charts.publish_charts(
            CIConfig(_raw={}),
            dfe_infra_shape,
            charts=["helm/charts/web"],
            registry=REGISTRY,
            version="2.2.0-rc.14",
        )
        assert results[0].version == "2.2.0-rc.14"

    def test_the_release_version_is_the_default(
        self,
        fake_helm: FakeHelm,
        dfe_infra_shape: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HYPERCI_VERSION", "3.1.0")
        _, results = charts.publish_charts(
            _enabled(["helm/charts/web"]), dfe_infra_shape, registry=REGISTRY
        )
        assert results[0].version == "3.1.0"

    def test_a_non_oci_registry_fails(
        self, fake_helm: FakeHelm, tmp_path: Path
    ) -> None:
        rc, _ = charts.publish_charts(
            _enabled(["x"]), tmp_path, registry="https://ghcr.io", version="1.0.0"
        )
        assert rc == 1

    def test_a_failed_push_fails_the_publish(
        self,
        fake_helm: FakeHelm,
        dfe_infra_shape: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            charts,
            "_helm",
            lambda *a, registry="": (
                (1, "denied") if a[0] == "push" else (1, "not found")
            ),
        )
        rc, _ = charts.publish_charts(
            _enabled(["helm/charts/web"]),
            dfe_infra_shape,
            registry=REGISTRY,
            version="1.0.0",
        )
        assert rc == 1

    def test_the_table_reaches_the_summary_and_the_release_body(
        self,
        fake_helm: FakeHelm,
        dfe_infra_shape: Path,
        tmp_path_factory: pytest.TempPathFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner_temp = tmp_path_factory.mktemp("runner")
        summary = runner_temp / "summary.md"
        monkeypatch.setenv("RUNNER_TEMP", str(runner_temp))
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        monkeypatch.chdir(dfe_infra_shape)

        charts.publish_charts(
            _enabled(["helm/charts/web"]),
            dfe_infra_shape,
            registry=REGISTRY,
            version="1.0.0",
        )

        row = f"| web | 1.0.0 | `{DIGEST}` |"
        assert row in summary.read_text(encoding="utf-8")
        with binaries._release_notes_flags("1.0.0", CIConfig(_raw={})) as flags:
            assert row in Path(flags[1]).read_text(encoding="utf-8")


class TestConfig:
    def test_the_shipped_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config_module, "_config_cache", None)
        config = load_config(reload=True, project_dir=tmp_path)
        assert config.get("release.helm.enabled") is False
        assert config.get("release.helm.charts") == []
        assert config.get("release.helm.registry") == REGISTRY
