# Project:   HyperI CI
# File:      tests/unit/test_chart_assemble.py
# Purpose:   Tests for assembling a thin Helm chart from a deployment contract
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

import json
import shutil
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml
from typer.testing import CliRunner, Result

from hyperi_ci import config as config_module
from hyperi_ci.cli import app
from hyperi_ci.native_tools import _linux_arch
from hyperi_ci.release import assemble
from hyperi_ci.release.charts import ChartError

DIGEST = "sha256:" + "ab" * 32
IMAGE = f"ghcr.io/hyperi-io/dfe-loader:v1.4.2@{DIGEST}"
REGISTRY = "oci://ghcr.io/hyperi-io/charts"
LIBRARY_DIR = Path(__file__).parent / "data" / "scalo-service"

needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not on PATH")


def _contract(**extra: object) -> dict:
    """A v4 contract whose dials sit behind a $ref, an anyOf and a plain object."""
    seconds = {"type": "integer", "minimum": 1}
    buffer = {
        "type": "object",
        "properties": {
            "flush_rows": {
                "type": "integer",
                "default": 20000,
                "minimum": 1,
                "maximum": 10000000,
                "x-scalo-dial": "big",
            },
            "flush_age_secs": {"$ref": "#/$defs/Seconds", "x-scalo-dial": "big"},
        },
    }
    hold = {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]}
    grpc = {
        "type": "object",
        "properties": {"max_hold_ms": hold | {"x-scalo-dial": "big"}},
    }
    level = {"type": "string", "enum": ["info", "debug"], "default": "info"}
    config_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "buffer": {"$ref": "#/$defs/Buffer"},
            "grpc": {"anyOf": [{"$ref": "#/$defs/Grpc"}, {"type": "null"}]},
            "kafka": {"type": "object", "properties": {"brokers": {"type": "array"}}},
            "logging": {
                "type": "object",
                "properties": {"level": level | {"x-scalo-dial": "small"}},
            },
        },
        "$defs": {"Buffer": buffer, "Grpc": grpc, "Seconds": seconds},
    }
    health = {
        "liveness_path": "/livez",
        "readiness_path": "/readyz",
        "metrics_path": "/metrics",
    }
    return {
        "schema_version": 4,
        "app_name": "dfe-loader",
        "description": "Loads rows into ClickHouse",
        "metrics_port": 9090,
        "health": health,
        "env_prefix": "DFE_LOADER",
        "metric_prefix": "loader",
        "image_registry": "ghcr.io/hyperi-io",
        "config_schema": config_schema,
        **extra,
    }


def _assemble(out: Path, contract: dict | None = None, image: str = IMAGE) -> Path:
    return assemble.assemble(
        _contract() if contract is None else contract,
        LIBRARY_DIR,
        out,
        version="1.4.2",
        image=image,
        library="0.1.0",
        registry=REGISTRY,
    )


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


class TestDials:
    def test_dials_are_found_through_refs_and_any_of(self) -> None:
        dials = assemble.find_dials(_contract()["config_schema"])
        assert list(dials) == [
            "buffer.flush_age_secs",
            "buffer.flush_rows",
            "grpc.max_hold_ms",
            "logging.level",
        ]
        assert dials["buffer.flush_age_secs"] == {
            "type": "integer",
            "minimum": 1,
            "x-scalo-dial": "big",
        }

    def test_a_marker_that_is_not_big_or_small_fails(self) -> None:
        schema = {"properties": {"a": {"type": "integer", "x-scalo-dial": "huge"}}}
        with pytest.raises(ChartError, match="config.a"):
            assemble.find_dials(schema)

    def test_values_schema_nests_the_dials_under_config(self) -> None:
        config_schema = _contract()["config_schema"]
        schema = assemble.values_schema(
            config_schema, assemble.find_dials(config_schema)
        )
        config = schema["properties"]["config"]
        assert schema["$schema"] == config_schema["$schema"]
        assert set(config["properties"]) == {"buffer", "grpc", "logging"}
        assert config["properties"]["buffer"]["properties"]["flush_rows"][
            "maximum"
        ] == (10000000)

    def test_values_schema_keeps_constraints_and_leaves_other_keys_open(self) -> None:
        config_schema = _contract()["config_schema"]
        schema = assemble.values_schema(
            config_schema, assemble.find_dials(config_schema)
        )
        ok = {"config": {"buffer": {"flush_rows": 5}, "kafka": {"brokers": ["k:9092"]}}}
        jsonschema.validate(ok, schema)
        with pytest.raises(jsonschema.ValidationError, match="less than the minimum"):
            jsonschema.validate({"config": {"buffer": {"flush_rows": 0}}}, schema)

    def test_the_skeleton_schema_is_the_base_and_keeps_its_constraints(
        self,
    ) -> None:
        config_schema = _contract()["config_schema"]
        base = json.loads(
            (LIBRARY_DIR / "skeleton" / "values.schema.json").read_text(
                encoding="utf-8"
            )
        )
        schema = assemble.values_schema(
            config_schema, assemble.find_dials(config_schema), base
        )
        assert set(schema["properties"]) == {
            "config",
            "configOverrides",
            "extraEnv",
            "fullnameOverride",
            "image",
            "podLabels",
            "replicaCount",
        }
        assert schema["$defs"] == base["$defs"]
        jsonschema.validate(
            {
                "replicaCount": 2,
                "podLabels": {"team": "data"},
                "config": {"buffer": {"flush_rows": 5}},
            },
            schema,
        )
        for bad in (
            {"replicaCount": -1},
            {"podLabels": {"team": 1}},
            {"config": {"buffer": {"flush_rows": 0}}},
        ):
            with pytest.raises(jsonschema.ValidationError):
                jsonschema.validate(bad, schema)
        assert "config" not in base["properties"]

    def test_a_skeleton_schema_that_declares_config_fails(self) -> None:
        base = {"type": "object", "properties": {"config": {"type": "object"}}}
        with pytest.raises(ChartError, match="declares config"):
            assemble.values_schema({}, {}, base)


class TestAssemble:
    def test_chart_yaml_fields(self, tmp_path: Path) -> None:
        chart = _assemble(tmp_path)
        meta = yaml.safe_load((chart / "Chart.yaml").read_text(encoding="utf-8"))
        assert meta == {
            "apiVersion": "v2",
            "name": "dfe-loader",
            "description": "Loads rows into ClickHouse",
            "type": "application",
            "version": "1.4.2",
            "appVersion": "v1.4.2",
            "dependencies": [
                {"name": "scalo-service", "repository": REGISTRY, "version": "0.1.0"}
            ],
        }

    def test_a_contract_without_a_description_keeps_the_skeletons(
        self, tmp_path: Path
    ) -> None:
        chart = _assemble(tmp_path, _contract(description=""))
        meta = yaml.safe_load((chart / "Chart.yaml").read_text(encoding="utf-8"))
        skeleton = yaml.safe_load(
            (LIBRARY_DIR / "skeleton" / "Chart.yaml").read_text(encoding="utf-8")
        )
        assert meta["description"] == skeleton["description"]

    def test_the_chart_carries_contract_skeleton_and_values(
        self, tmp_path: Path
    ) -> None:
        chart = _assemble(tmp_path)
        assert sorted(_tree(chart)) == [
            "Chart.yaml",
            "files/contract.json",
            "templates/configmap.yaml",
            "templates/deployment.yaml",
            "values.schema.json",
            "values.yaml",
        ]
        contract = json.loads(
            (chart / "files/contract.json").read_text(encoding="utf-8")
        )
        assert contract == _contract()
        values_text = (chart / "values.yaml").read_text(encoding="utf-8")
        assert yaml.safe_load(values_text) == {
            "config": {},
            "image": {"digest": DIGEST},
        }
        assert "# config.buffer.flush_rows: 20000  # big\n" in values_text
        assert "# config.grpc.max_hold_ms:  # big\n" in values_text
        assert "kafka" not in values_text

    def test_a_rerun_gives_identical_bytes(self, tmp_path: Path) -> None:
        first = _assemble(tmp_path / "a")
        reordered = dict(reversed(list(_contract().items())))
        second = _assemble(tmp_path / "b", reordered)
        assert _tree(first) == _tree(second)

    def test_a_contract_failing_the_schema_fails_with_every_finding(
        self, tmp_path: Path
    ) -> None:
        contract = _contract(metrics_port="9090")
        del contract["health"]
        with pytest.raises(ChartError) as caught:
            _assemble(tmp_path, contract)
        assert "$.metrics_port" in str(caught.value)
        assert "'health' is a required property" in str(caught.value)
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize(
        "name", ["", "Dfe_Loader", "../escaped", "dfe/loader", "-loader", "a" * 64]
    )
    def test_an_app_name_that_is_not_a_dns_label_fails(
        self, tmp_path: Path, name: str
    ) -> None:
        out = tmp_path / "out"
        out.mkdir()
        with pytest.raises(ChartError, match="not a lowercase DNS label"):
            _assemble(out, _contract(app_name=name), f"ghcr.io/hyperi-io/x:v1@{DIGEST}")
        assert sorted(p.name for p in tmp_path.iterdir()) == ["out"]
        assert list(out.iterdir()) == []

    def test_an_image_from_another_repository_fails(self, tmp_path: Path) -> None:
        image = f"ghcr.io/someone-else/dfe-loader:v1.4.2@{DIGEST}"
        with pytest.raises(ChartError, match="ghcr.io/hyperi-io/dfe-loader"):
            _assemble(tmp_path, image=image)
        assert list(tmp_path.iterdir()) == []

    def test_a_trailing_slash_on_the_contract_registry_is_ignored(
        self, tmp_path: Path
    ) -> None:
        chart = _assemble(tmp_path, _contract(image_registry="ghcr.io/hyperi-io/"))
        assert chart.name == "dfe-loader"

    def test_a_contract_without_an_image_registry_fails(self, tmp_path: Path) -> None:
        contract = _contract()
        del contract["image_registry"]
        for registry in (None, ""):
            if registry is not None:
                contract["image_registry"] = registry
            with pytest.raises(ChartError, match="no image_registry"):
                _assemble(tmp_path, contract)
        assert list(tmp_path.iterdir()) == []

    def test_a_schema_version_the_library_lacks_fails(self, tmp_path: Path) -> None:
        with pytest.raises(ChartError, match="schema_version 9"):
            _assemble(tmp_path, _contract(schema_version=9))

    def test_an_image_without_a_digest_fails(self, tmp_path: Path) -> None:
        with pytest.raises(ChartError, match="sha256"):
            assemble.assemble(
                _contract(),
                LIBRARY_DIR,
                tmp_path,
                version="1.4.2",
                image="ghcr.io/hyperi-io/dfe-loader:v1.4.2",
                library="0.1.0",
                registry=REGISTRY,
            )

    @needs_helm
    def test_the_library_renders_the_charts_own_contract(self, tmp_path: Path) -> None:
        chart = _assemble(tmp_path)
        shutil.copytree(LIBRARY_DIR, chart / "charts" / "scalo-service")
        rendered = subprocess.run(
            ["helm", "template", "rel", str(chart)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        assert rendered.returncode == 0, rendered.stderr
        docs = {d["kind"]: d for d in yaml.safe_load_all(rendered.stdout) if d}
        assert docs["ConfigMap"]["metadata"]["name"] == "dfe-loader-config"
        container = docs["Deployment"]["spec"]["template"]["spec"]["containers"][0]
        assert container["image"] == IMAGE


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config_module, "_config_cache", None)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    root = tmp_path / "repo"
    (root / "deploy").mkdir(parents=True)
    (root / "deploy" / "contract.json").write_text(
        json.dumps(_contract()), encoding="utf-8"
    )
    return root


def _configure(root: Path, contract: str | None, library: str | None = "0.1.0") -> None:
    helm = {"enabled": True, "contract": contract, "library": library}
    (root / ".hyperi-ci.yaml").write_text(
        yaml.safe_dump({"release": {"helm": helm}}), encoding="utf-8"
    )


def _invoke(root: Path, *extra: str) -> Result:
    return CliRunner().invoke(
        app,
        [
            "chart",
            "assemble",
            "-C",
            str(root),
            "--image",
            IMAGE,
            "--version",
            "1.4.2",
            "--library-dir",
            str(LIBRARY_DIR),
            *extra,
        ],
    )


class TestCli:
    def test_a_committed_contract_is_assembled_outside_the_repo(
        self, repo: Path
    ) -> None:
        _configure(repo, "deploy/contract.json")
        before = _tree(repo)
        result = _invoke(repo)
        assert result.exit_code == 0, result.output
        chart = Path(result.stdout.strip())
        assert (chart / "Chart.yaml").is_file()
        assert not chart.is_relative_to(repo)
        assert _tree(repo) == before

    def test_an_emitted_contract_comes_from_generate_artefacts(
        self, repo: Path, tmp_path: Path
    ) -> None:
        _configure(repo, "emit")
        producer = tmp_path / "dfe-loader"
        producer.write_text(
            f"#!{sys.executable}\n"
            "import json, pathlib, sys\n"
            "assert sys.argv[1] == 'generate-artefacts'\n"
            "out = pathlib.Path(sys.argv[sys.argv.index('--output-dir') + 1])\n"
            "out.mkdir(parents=True)\n"
            f"(out / 'deployment-contract.json').write_text({json.dumps(json.dumps(_contract()))})\n",
            encoding="utf-8",
        )
        producer.chmod(0o755)
        result = _invoke(
            repo, "--binary", str(producer), "--output-dir", str(tmp_path / "out")
        )
        assert result.exit_code == 0, result.output
        assert result.stdout.strip() == str((tmp_path / "out" / "dfe-loader").resolve())

    def test_a_failing_producer_fails_the_assembly(self, repo: Path) -> None:
        _configure(repo, "emit")
        result = _invoke(repo, "--binary", "false")
        assert result.exit_code == 1
        assert result.stdout == ""

    def test_an_output_dir_inside_the_repo_is_refused(self, repo: Path) -> None:
        _configure(repo, "deploy/contract.json")
        result = _invoke(repo, "--output-dir", str(repo / "chart"))
        assert result.exit_code == 1
        assert not (repo / "chart").exists()

    def test_no_contract_assembles_nothing(self, repo: Path) -> None:
        _configure(repo, None)
        result = _invoke(repo)
        assert result.exit_code == 0
        assert result.stdout == ""

    def test_a_contract_without_a_library_version_fails(self, repo: Path) -> None:
        _configure(repo, "deploy/contract.json", library=None)
        assert _invoke(repo).exit_code == 1

    def test_a_contract_path_outside_the_repo_fails(self, repo: Path) -> None:
        _configure(repo, "../elsewhere.json")
        assert _invoke(repo).exit_code == 1


@pytest.mark.skipif(_linux_arch() is None, reason="dist/ binaries are Linux-only")
def test_a_rust_producer_runs_the_host_dist_binary(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "dfe-loader"\n\n[dependencies]\n'
        'scalo = { version = "2", features = ["deployment"] }\n',
        encoding="utf-8",
    )
    binary = tmp_path / "dist" / f"dfe-loader-linux-{_linux_arch()}"
    binary.parent.mkdir()
    binary.write_bytes(b"")
    assert assemble.producer_command(tmp_path) == [str(binary)]
