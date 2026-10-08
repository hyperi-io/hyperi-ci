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
import tarfile
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest
import yaml
from typer.testing import CliRunner, Result

from hyperi_ci import cli
from hyperi_ci import config as config_module
from hyperi_ci.cli import app
from hyperi_ci.native_tools import _linux_arch
from hyperi_ci.quality import checkov
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
        "config_mount_path": "/etc/dfe-loader",
        "config_schema": config_schema,
        **extra,
    }


def _raw(contract: dict) -> bytes:
    return json.dumps(contract).encode("utf-8")


def _assemble(
    out: Path, contract: dict | bytes | None = None, image: str = IMAGE
) -> Path:
    if contract is None:
        contract = _contract()
    return assemble.assemble(
        contract if isinstance(contract, bytes) else _raw(contract),
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
        assert set(schema["properties"]) == {*base["properties"], "config"}
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
            ".helmignore",
            ".hyperi-ci.yaml",
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
        assert _tree(_assemble(tmp_path / "a")) == _tree(_assemble(tmp_path / "b"))

    def test_the_derived_files_do_not_follow_the_contracts_key_order(
        self, tmp_path: Path
    ) -> None:
        reordered = _raw(dict(reversed(list(_contract().items()))))
        first = _tree(_assemble(tmp_path / "a"))
        second = _tree(_assemble(tmp_path / "b", reordered))
        assert first.pop("files/contract.json") == _raw(_contract())
        assert second.pop("files/contract.json") == reordered
        assert first == second

    def test_files_contract_json_is_the_bytes_given(self, tmp_path: Path) -> None:
        contract = _contract(description=f"L{chr(0xE4)}dt Zeilen in ClickHouse")
        raw = json.dumps(contract, ensure_ascii=False, separators=(",", ":"))
        chart = _assemble(tmp_path, raw.encode("utf-8"))
        assert (chart / "files" / "contract.json").read_bytes() == raw.encode("utf-8")

    @pytest.mark.parametrize("config_schema", ["absent", None, True])
    def test_a_contract_with_no_config_schema_has_no_dials(
        self, tmp_path: Path, config_schema: object
    ) -> None:
        contract = _contract(config_schema=config_schema)
        if config_schema == "absent":
            del contract["config_schema"]
        chart = _assemble(tmp_path, contract)
        values_text = (chart / "values.yaml").read_text(encoding="utf-8")
        assert "# config." not in values_text
        schema = json.loads((chart / "values.schema.json").read_text(encoding="utf-8"))
        assert schema["properties"]["config"] == {"type": "object"}

    def test_the_skeletons_schema_dialect_wins_over_the_contracts(
        self, tmp_path: Path
    ) -> None:
        contract = _contract()
        contract["config_schema"]["$schema"] = "http://json-schema.org/draft-07/schema#"
        chart = _assemble(tmp_path, contract)
        schema = json.loads((chart / "values.schema.json").read_text(encoding="utf-8"))
        base = json.loads(
            (LIBRARY_DIR / "skeleton" / "values.schema.json").read_text(
                encoding="utf-8"
            )
        )
        assert schema["$schema"] == base["$schema"]
        assert schema["$schema"] != contract["config_schema"]["$schema"]

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
        "name",
        [
            "",
            "Dfe_Loader",
            "../escaped",
            "dfe/loader",
            "-loader",
            "a" * 64,
            "1loader",
            "9",
        ],
    )
    def test_an_app_name_that_is_not_a_service_name_fails(
        self, tmp_path: Path, name: str
    ) -> None:
        out = tmp_path / "out"
        out.mkdir()
        with pytest.raises(ChartError, match="not a Kubernetes Service name"):
            _assemble(out, _contract(app_name=name), f"ghcr.io/hyperi-io/x:v1@{DIGEST}")
        assert sorted(p.name for p in tmp_path.iterdir()) == ["out"]
        assert list(out.iterdir()) == []

    def test_the_name_rule_holds_without_the_schemas_pattern(
        self, tmp_path: Path
    ) -> None:
        library = tmp_path / "library"
        shutil.copytree(LIBRARY_DIR, library)
        schema_file = library / "schema" / "deployment-contract.v4.schema.json"
        schema = json.loads(schema_file.read_text(encoding="utf-8"))
        del schema["properties"]["app_name"]["pattern"]
        schema_file.write_text(json.dumps(schema), encoding="utf-8")
        with pytest.raises(ChartError, match="not a Kubernetes Service name"):
            assemble.assemble(
                _raw(_contract(app_name="1loader")),
                library,
                tmp_path,
                version="1.4.2",
                image=f"ghcr.io/hyperi-io/1loader:v1@{DIGEST}",
                library="0.1.0",
                registry=REGISTRY,
            )

    @pytest.mark.parametrize("name", ["a", "a" + "b0-" * 20 + "cd"])
    def test_a_service_name_up_to_63_characters_assembles(
        self, tmp_path: Path, name: str
    ) -> None:
        image = f"ghcr.io/hyperi-io/{name}:v1@{DIGEST}"
        assert _assemble(tmp_path, _contract(app_name=name), image).name == name

    @pytest.mark.parametrize(
        ("field", "path"),
        [
            ({"metrics_port": 0}, "$.metrics_port"),
            ({"extra_ports": [{"name": "http", "port": 0}]}, "$.extra_ports[0].port"),
        ],
    )
    def test_a_port_below_one_fails_the_schema(
        self, tmp_path: Path, field: dict, path: str
    ) -> None:
        with pytest.raises(ChartError, match="less than the minimum") as caught:
            _assemble(tmp_path, _contract(**field))
        assert path in str(caught.value)
        assert list(tmp_path.iterdir()) == []

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
                _raw(_contract()),
                LIBRARY_DIR,
                tmp_path,
                version="1.4.2",
                image="ghcr.io/hyperi-io/dfe-loader:v1.4.2",
                library="0.1.0",
                registry=REGISTRY,
            )

    @pytest.mark.parametrize("tag", ["v1.2.3", "1.2.3", "1.10"])
    def test_app_version_is_the_tag_as_pushed(self, tmp_path: Path, tag: str) -> None:
        image = f"ghcr.io/hyperi-io/dfe-loader:{tag}@{DIGEST}"
        chart = _assemble(tmp_path, image=image)
        meta = yaml.safe_load((chart / "Chart.yaml").read_text(encoding="utf-8"))
        assert meta["appVersion"] == tag

    @needs_helm
    def test_the_library_renders_the_charts_own_contract(self, tmp_path: Path) -> None:
        chart = _built_chart(tmp_path)
        rendered = _helm_cli("template", "rel", str(chart))
        docs = {d["kind"]: d for d in yaml.safe_load_all(rendered) if d}
        assert docs["ConfigMap"]["metadata"]["name"] == "dfe-loader-config"
        container = docs["Deployment"]["spec"]["template"]["spec"]["containers"][0]
        assert container["image"] == IMAGE


def _helm_cli(*args: str) -> str:
    result = subprocess.run(
        ["helm", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _built_chart(tmp_path: Path) -> Path:
    """Assemble on the test library as a file:// dependency and build it in."""
    library = yaml.safe_load((LIBRARY_DIR / "Chart.yaml").read_text(encoding="utf-8"))
    chart = assemble.assemble(
        _raw(_contract()),
        LIBRARY_DIR,
        tmp_path,
        version="1.4.2",
        image=IMAGE,
        library=library["version"],
        registry=f"file://{LIBRARY_DIR}",
    )
    _helm_cli("dependency", "build", str(chart))
    return chart


def _library_copy(tmp_path: Path, lint_skip: str | None) -> Path:
    library = tmp_path / "library"
    shutil.copytree(LIBRARY_DIR, library)
    if lint_skip is None:
        (library / "lint-skip.yaml").unlink()
    else:
        (library / "lint-skip.yaml").write_text(lint_skip, encoding="utf-8")
    return library


def _assemble_on(library: Path, out: Path) -> Path:
    return assemble.assemble(
        _raw(_contract()),
        library,
        out,
        version="1.4.2",
        image=IMAGE,
        library="0.1.0",
        registry=REGISTRY,
    )


class TestLintSkip:
    @pytest.fixture
    def checkov_cmds(self, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
        cmds: list[list[str]] = []

        def _run(cmd: list[str], **_: object) -> SimpleNamespace:
            cmds.append(cmd)
            out = Path(cmd[cmd.index("--output-file-path") + 1])
            (out / "results_sarif.sarif").write_text(
                json.dumps({"runs": [{"tool": {"driver": {}}, "results": []}]}),
                encoding="utf-8",
            )
            return SimpleNamespace(stdout="", stderr="", returncode=0)

        monkeypatch.setattr(checkov, "_base_cmd", lambda: ["checkov"])
        monkeypatch.setattr(checkov, "run_cmd", _run)
        monkeypatch.delenv("HYPERCI_QUALITY_SKIP", raising=False)
        return cmds

    @staticmethod
    def _skipped(
        directory: Path, cmds: list[list[str]], monkeypatch: pytest.MonkeyPatch
    ) -> list[str]:
        monkeypatch.setattr(config_module, "_config_cache", None)
        assert cli._lint_iac(str(directory), None, ("checkov",)) == 0
        cmd = cmds.pop()
        return [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--skip-check"]

    def test_lint_iac_on_the_chart_skips_the_librarys_checkov_ids_only_there(
        self,
        tmp_path: Path,
        checkov_cmds: list[list[str]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = tmp_path / "repo"
        (repo / "deploy").mkdir(parents=True)
        (repo / "deploy" / "pod.yaml").write_text(
            "apiVersion: v1\nkind: Pod\nmetadata:\n  name: x\n", encoding="utf-8"
        )
        chart = _assemble(tmp_path / "out")
        expected = ["CKV2_K8S_6", "CKV_K8S_40"]
        assert self._skipped(chart, checkov_cmds, monkeypatch) == expected
        assert self._skipped(chart.parent, checkov_cmds, monkeypatch) == []
        assert self._skipped(repo, checkov_cmds, monkeypatch) == []

    def test_the_chart_config_keeps_the_reasons(self, tmp_path: Path) -> None:
        text = (_assemble(tmp_path) / ".hyperi-ci.yaml").read_text(encoding="utf-8")
        assert "#   CKV_K8S_40: The uid comes from the contract's security" in text
        assert yaml.safe_load(text) == {
            "quality": {"checkov": {"skip": ["CKV2_K8S_6", "CKV_K8S_40"]}}
        }

    @needs_helm
    def test_the_chart_config_stays_out_of_the_package(self, tmp_path: Path) -> None:
        chart = _built_chart(tmp_path / "chart")
        _helm_cli("package", str(chart), "-d", str(tmp_path / "pkg"))
        tgz = next((tmp_path / "pkg").glob("*.tgz"))
        with tarfile.open(tgz) as archive:
            names = archive.getnames()
        assert "dfe-loader/Chart.yaml" in names
        assert "dfe-loader/.hyperi-ci.yaml" not in names

    @pytest.mark.parametrize(
        "lint_skip", [None, "kube-linter:\n  no-read-only-root-fs: reason\n"]
    )
    def test_no_checkov_entry_writes_no_chart_config(
        self, tmp_path: Path, lint_skip: str | None
    ) -> None:
        chart = _assemble_on(_library_copy(tmp_path, lint_skip), tmp_path)
        assert not (chart / ".hyperi-ci.yaml").exists()
        assert not (chart / ".helmignore").exists()

    def test_a_skeleton_helmignore_is_extended(self, tmp_path: Path) -> None:
        library = _library_copy(tmp_path, "checkov:\n  CKV_K8S_40: reason\n")
        (library / "skeleton" / ".helmignore").write_text("tests/", encoding="utf-8")
        chart = _assemble_on(library, tmp_path)
        helmignore = (chart / ".helmignore").read_text(encoding="utf-8")
        assert helmignore == "tests/\n.hyperi-ci.yaml\n"

    @pytest.mark.parametrize(
        "lint_skip",
        [
            "- CKV_K8S_40\n",
            "checkov:\n  - CKV_K8S_40\n",
            "checkov:\n  'CKV_K8S_40,CKV_K8S_1': reason\n",
            "checkov:\n  CKV_K8S_40: ''\n",
        ],
    )
    def test_a_malformed_lint_skip_fails_before_writing(
        self, tmp_path: Path, lint_skip: str
    ) -> None:
        library = _library_copy(tmp_path, lint_skip)
        out = tmp_path / "out"
        out.mkdir()
        with pytest.raises(ChartError, match="lint-skip.yaml"):
            _assemble_on(library, out)
        assert list(out.iterdir()) == []


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
        assert (chart / "files" / "contract.json").read_bytes() == (
            repo / "deploy" / "contract.json"
        ).read_bytes()
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


class TestPulledLibrary:
    @pytest.fixture
    def helm_calls(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
        calls: list[tuple] = []

        def _helm(*args: str, registry: str = "") -> tuple[int, str]:
            calls.append((*args, registry))
            return (1, "pull access denied") if "fail" in registry else (0, "")

        monkeypatch.setattr(assemble, "_ensure_helm", lambda: True)
        monkeypatch.setattr(assemble, "_login", lambda registry: None)
        monkeypatch.setattr(
            assemble, "_pull_library", lambda registry, library, scratch: LIBRARY_DIR
        )
        monkeypatch.setattr(assemble, "_helm", _helm)
        return calls

    def _run(self, repo: Path, registry: str) -> tuple[int, Path | None]:
        _configure(repo, "deploy/contract.json")
        return assemble.assemble_chart(
            config_module.load_config(project_dir=repo, reload=True),
            repo,
            image=IMAGE,
            output_dir=repo.parent / "out",
            registry=registry,
            version="1.4.2",
        )

    def test_the_dependency_is_built_into_the_chart(
        self, repo: Path, helm_calls: list[tuple]
    ) -> None:
        rc, chart = self._run(repo, REGISTRY)
        assert rc == 0
        assert helm_calls == [("dependency", "build", str(chart), REGISTRY)]

    def test_a_failed_dependency_build_fails_and_leaves_no_chart(
        self, repo: Path, helm_calls: list[tuple]
    ) -> None:
        rc, chart = self._run(repo, "oci://fail.example.com/charts")
        assert (rc, chart) == (1, None)
        assert list((repo.parent / "out").iterdir()) == []


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
