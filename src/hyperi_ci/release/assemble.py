# Project:   HyperI CI
# File:      src/hyperi_ci/release/assemble.py
# Purpose:   Assemble a thin Helm chart from a deployment contract
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Assemble a thin Helm chart from an app's deployment contract.

The contract comes from the app (``<binary> generate-artefacts``) or from a
committed file. It is validated against the JSON Schema the scalo-service
library chart ships for its ``schema_version``, then written with the library's
skeleton into a chart that depends on scalo-service. Every generated file has
sorted keys and no timestamp, so the same inputs give the same bytes.

The library pulls ``<image_registry>/<app_name>`` at the chart's appVersion and
the digest in values, so the image given must be that repository.

``values.schema.json`` is the skeleton's own schema for the library values,
plus the ``config_schema`` nodes marked ``x-scalo-dial: big|small``, each
copied with its ``$ref``s inlined, under ``config.<path>``. Other config keys
reach the app through ``configOverrides`` and are not validated, so a stored
overlay never pins a key the app later drops. ``values.yaml`` sets ``config: {}`` and lists the same
dials commented out, so the app's own defaults stand until one is set.
"""

import json
import os
import re
import shutil
import tempfile
from pathlib import Path

import jsonschema
import yaml

from hyperi_ci.common import (
    ReleaseVersionError,
    error,
    info,
    resolve_release_version,
    run_cmd,
    success,
)
from hyperi_ci.config import CIConfig
from hyperi_ci.deployment.detect import Tier, resolve_tier
from hyperi_ci.deployment.manifest import python_entry_point, rust_binary_name
from hyperi_ci.native_tools import _linux_arch
from hyperi_ci.release.charts import ChartError, _ensure_helm, _helm, _login
from hyperi_ci.repo_path import RepoPathError, confine

LIBRARY = "scalo-service"
CONTRACT_FILE = "deployment-contract.json"
SKELETON_DIR = "skeleton"
VALUES_SCHEMA = "values.schema.json"
SCHEMA_PATH = "schema/deployment-contract.v{version}.schema.json"
# A dial path segment is written into values.yaml and split on dots, so it is
# held to a plain property name.
DIAL_SEGMENT_RE = re.compile(r"[A-Za-z0-9_-]+")
DIAL_KEY = "x-scalo-dial"
DIAL_TIERS = ("big", "small")

# The tag becomes the chart's appVersion, so a digest-only ref is refused.
IMAGE_RE = re.compile(
    r"(?P<repo>[^@\s]+):(?P<tag>[A-Za-z0-9_][A-Za-z0-9._-]{0,127})"
    r"@(?P<digest>sha256:[0-9a-f]{64})"
)
# The chart directory and every object the library renders take this name, and
# the contract schema does not constrain it.
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")


def _json(data: object) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def _pointer(root: dict, ref: str) -> object:
    if not ref.startswith("#"):
        raise ChartError(f"config_schema $ref {ref!r} is not local to the contract")
    node: object = root
    for part in filter(None, ref[1:].split("/")):
        key = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or key not in node:
            raise ChartError(f"config_schema $ref {ref!r} points at nothing")
        node = node[key]
    return node


def _inline(node: object, root: dict, seen: frozenset[str] = frozenset()) -> object:
    """Return ``node`` with every local ``$ref`` replaced by its target."""
    if isinstance(node, list):
        return [_inline(item, root, seen) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict = {}
    ref = node.get("$ref")
    if isinstance(ref, str):
        if ref in seen:
            raise ChartError(f"dial schema {ref!r} refers to itself")
        target = _inline(_pointer(root, ref), root, seen | {ref})
        out = dict(target) if isinstance(target, dict) else {}
    out.update({k: _inline(v, root, seen) for k, v in node.items() if k != "$ref"})
    return out


def find_dials(config_schema: dict) -> dict[str, dict]:
    """Return every ``x-scalo-dial`` node in ``config_schema`` by dotted path.

    A marked node is a dial whole, so nothing beneath it is searched.
    ``allOf``, ``anyOf`` and ``oneOf`` branches sit at their parent's path.

    A ``properties`` that is not a mapping, or a branch list that is not a
    list, has no nodes to walk.

    Raises:
        ChartError: A marker is not big or small, a ``$ref`` is broken, or a
            dial's path has a segment that is not a plain property name.

    """
    found: dict[str, dict] = {}

    def walk(node: object, path: tuple[str, ...], seen: frozenset[str]) -> None:
        if not isinstance(node, dict):
            return
        tier = node.get(DIAL_KEY)
        if tier is not None:
            if tier not in DIAL_TIERS:
                raise ChartError(f"config.{'.'.join(path)}: {DIAL_KEY} is {tier!r}")
            for segment in path:
                if not DIAL_SEGMENT_RE.fullmatch(segment):
                    raise ChartError(
                        f"dial property {segment!r} is not a plain property name: "
                        "letters, digits, underscores and hyphens only"
                    )
            inlined = _inline(node, config_schema)
            if isinstance(inlined, dict):
                found.setdefault(".".join(path), inlined)
            return
        ref = node.get("$ref")
        if isinstance(ref, str) and ref not in seen:
            walk(_pointer(config_schema, ref), path, seen | {ref})
        properties = node.get("properties")
        if isinstance(properties, dict):
            for name, child in sorted(properties.items()):
                walk(child, (*path, name), seen)
        for key in ("allOf", "anyOf", "oneOf"):
            branches = node.get(key)
            if isinstance(branches, list):
                for child in branches:
                    walk(child, path, seen)

    walk(config_schema, (), frozenset())
    return dict(sorted(found.items()))


def values_schema(
    config_schema: dict, dials: dict[str, dict], base: dict | None = None
) -> dict:
    """Build ``values.schema.json``: the library's base, plus the dials under ``config``.

    ``base`` is the skeleton's own ``values.schema.json``, which constrains the
    library values (image, replicas, resources and so on). It must not declare
    ``config``, because the dials own that key.

    Raises:
        ChartError: ``base`` declares ``properties.config``.

    """
    config: dict = {"type": "object"}
    for dotted, leaf in dials.items():
        *parents, name = dotted.split(".")
        node = config
        for part in parents:
            properties = node.setdefault("properties", {})
            node = properties.setdefault(part, {"type": "object"})
        node.setdefault("properties", {})[name] = leaf
    schema: dict = json.loads(json.dumps(base)) if base else {"type": "object"}
    properties = schema.setdefault("properties", {})
    if "config" in properties:
        raise ChartError(f"{LIBRARY} {SKELETON_DIR}/{VALUES_SCHEMA} declares config")
    properties["config"] = config
    if "$schema" not in schema and "$schema" in config_schema:
        schema["$schema"] = config_schema["$schema"]
    return schema


def values_yaml(digest: str, dials: dict[str, dict]) -> str:
    """Build ``values.yaml``: the image digest, empty config, dials commented."""
    body = yaml.safe_dump({"config": {}, "image": {"digest": digest}}, sort_keys=True)
    lines = ["# Generated by hyperi-ci chart assemble from files/contract.json."]
    lines.append(body.rstrip("\n"))
    if dials:
        lines.append("# Dials, unset so the app's own default applies:")
    for dotted, leaf in dials.items():
        # ASCII-escaped JSON has no line break, so a default stays on its comment.
        default = (
            f" {json.dumps(leaf['default'], ensure_ascii=True)}"
            if "default" in leaf
            else ""
        )
        lines.append(f"# config.{dotted}:{default}  # {leaf[DIAL_KEY]}")
    return "\n".join(lines) + "\n"


def validate_contract(contract: dict, schema: dict) -> None:
    """Fail on every place ``contract`` breaks ``schema``.

    Raises:
        ChartError: The schema is not a JSON Schema, or the contract fails it.

    """
    validator = jsonschema.validators.validator_for(schema)
    try:
        validator.check_schema(schema)
    except jsonschema.SchemaError as exc:
        raise ChartError(f"the contract schema is invalid: {exc.message}") from exc
    findings = sorted(
        validator(schema).iter_errors(contract), key=lambda e: e.json_path
    )
    if findings:
        lines = [f"  {e.json_path}: {e.message}" for e in findings]
        raise ChartError("the contract fails its schema:\n" + "\n".join(lines))


def _read_json(path: Path, what: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ChartError(f"cannot read {what} {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ChartError(f"{what} {path} is not a JSON object")
    return data


def assemble(
    contract: dict,
    library_dir: Path,
    out_dir: Path,
    *,
    version: str,
    image: str,
    library: str,
    registry: str,
) -> Path:
    """Write the thin chart for ``contract`` to ``out_dir/<app_name>``.

    Args:
        contract: The deployment contract.
        library_dir: An unpacked scalo-service chart.
        out_dir: Where the chart directory is created.
        version: The chart version, the release version.
        image: ``<repo>:<tag>@sha256:<digest>``. The library pulls
            ``<image_registry>/<app_name>`` from the contract, so ``<repo>``
            must be that; the tag becomes appVersion.
        library: The scalo-service version the chart depends on.
        registry: The ``oci://`` registry scalo-service is pulled from.

    Returns:
        The chart directory.

    Raises:
        ChartError: The image ref, contract or library chart cannot be used,
            the image is not the one the contract names, or the chart
            directory already exists.

    """
    ref = IMAGE_RE.fullmatch(image)
    if ref is None:
        raise ChartError(f"image {image!r} is not <repo>:<tag>@sha256:<digest>")
    schema_file = library_dir / SCHEMA_PATH.format(
        version=contract.get("schema_version")
    )
    if not schema_file.is_file():
        raise ChartError(
            f"{LIBRARY} {library} ships no {schema_file.relative_to(library_dir)} "
            f"for contract schema_version {contract.get('schema_version')!r}"
        )
    validate_contract(contract, _read_json(schema_file, "contract schema"))
    name = contract.get("app_name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ChartError(f"app_name {name!r} is not a lowercase DNS label")
    image_registry = str(contract.get("image_registry") or "").removesuffix("/")
    if not image_registry:
        raise ChartError("the contract has no image_registry to pull the image from")
    if ref["repo"] != f"{image_registry}/{name}":
        raise ChartError(
            f"image {ref['repo']} is not {image_registry}/{name}, "
            "the <image_registry>/<app_name> the chart pulls"
        )
    skeleton = library_dir / SKELETON_DIR
    if not skeleton.is_dir():
        raise ChartError(f"{LIBRARY} {library} has no {SKELETON_DIR}/ directory")
    chart = out_dir / name
    if chart.exists():
        raise ChartError(f"{chart} already exists")

    config_schema = contract.get("config_schema") or {}
    dials = find_dials(config_schema)
    try:
        meta = yaml.safe_load((skeleton / "Chart.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ChartError(f"{LIBRARY} {library} skeleton Chart.yaml: {exc}") from exc
    if not isinstance(meta, dict):
        raise ChartError(f"{LIBRARY} {library} skeleton Chart.yaml is not a mapping")
    meta.update(
        name=name,
        version=version,
        appVersion=ref["tag"],
        dependencies=[{"name": LIBRARY, "repository": registry, "version": library}],
    )
    if contract.get("description"):
        meta["description"] = contract["description"]

    base_file = skeleton / VALUES_SCHEMA
    base = (
        _read_json(base_file, "skeleton values schema") if base_file.is_file() else None
    )
    schema = values_schema(config_schema, dials, base)

    shutil.copytree(skeleton, chart)
    files = {
        "Chart.yaml": yaml.safe_dump(meta, sort_keys=True),
        "files/contract.json": _json(contract),
        "values.yaml": values_yaml(ref["digest"], dials),
        VALUES_SCHEMA: _json(schema),
    }
    (chart / "files").mkdir(exist_ok=True)
    for rel, text in files.items():
        (chart / rel).write_text(text, encoding="utf-8", newline="\n")
    return chart


def producer_command(root: Path, binary: str | None = None) -> list[str]:
    """Return the command whose ``generate-artefacts`` emits the contract.

    Raises:
        ChartError: No built binary or console script can be found.

    """
    if binary:
        return [binary]
    decision = resolve_tier(root)
    if decision.tier is Tier.RUST:
        name = rust_binary_name(root)
        arch = _linux_arch()
        candidates = [root / "target" / "release" / str(name)]
        if arch:
            candidates.insert(0, root / "dist" / f"{name}-linux-{arch}")
        for candidate in candidates:
            if candidate.is_file():
                return [str(candidate)]
        raise ChartError(f"no built {name} for this host in dist/ or target/release/")
    if decision.tier is Tier.PYTHON:
        script = str(python_entry_point(root))
        return ["uv", "run", "--frozen", "--project", str(root), script]
    raise ChartError(f"release.helm.contract is emit, but {decision.reason}")


def load_contract(setting: str, root: Path, scratch: Path, binary: str | None) -> dict:
    """Return the contract ``release.helm.contract`` names: ``emit`` or a path.

    Raises:
        ChartError: The producer failed, or the contract cannot be read.

    """
    if setting != "emit":
        try:
            path = confine(setting, root, key="release.helm.contract")
        except RepoPathError as exc:
            raise ChartError(str(exc)) from exc
        return _read_json(path, "contract")
    cmd = [*producer_command(root, binary), "generate-artefacts"]
    info(f"Emitting the contract: {' '.join(cmd)}")
    emitted = scratch / "emitted"
    try:
        result = run_cmd(
            [*cmd, "--output-dir", str(emitted)],
            check=False,
            capture=True,
            merge_stderr=True,
            cwd=root,
        )
    except OSError as exc:
        raise ChartError(f"cannot run {cmd[0]}: {exc}") from exc
    if result.returncode != 0:
        raise ChartError(f"{' '.join(cmd)} failed:\n{result.stdout}")
    return _read_json(emitted / CONTRACT_FILE, "emitted contract")


def _pull_library(registry: str, library: str, scratch: Path) -> Path:
    _login(registry)
    rc, out = _helm(
        "pull",
        f"{registry}/{LIBRARY}",
        "--version",
        library,
        "--untar",
        "--untardir",
        str(scratch),
        registry=registry,
    )
    if rc != 0:
        raise ChartError(f"helm pull {LIBRARY} {library} failed:\n{out}")
    return scratch / LIBRARY


def assemble_chart(
    config: CIConfig,
    root: Path,
    *,
    image: str,
    output_dir: Path | None = None,
    library_dir: Path | None = None,
    binary: str | None = None,
    registry: str | None = None,
    version: str | None = None,
) -> tuple[int, Path | None]:
    """Assemble the thin chart ``release.helm`` describes, if it describes one.

    Returns:
        ``(exit code, chart directory)``. The directory is None when the
        project assembles no chart or the assembly failed.

    """
    setting = config.get("release.helm.contract")
    library = config.get("release.helm.library")
    if not config.get("release.helm.enabled", False) or not setting:
        info("release.helm.contract is not set -- no chart to assemble")
        return 0, None
    registry = (registry or config.get("release.helm.registry") or "").rstrip("/")
    try:
        version = version or resolve_release_version()
    except ReleaseVersionError as exc:
        error(str(exc))
        return 1, None
    inside_repo = output_dir is not None and output_dir.resolve().is_relative_to(
        root.resolve()
    )
    checks = (
        (not library, "release.helm.library is not set"),
        (
            not registry.startswith("oci://"),
            f"Helm registry must be oci://: {registry!r}",
        ),
        (not version, "No release version -- pass --version or set HYPERCI_VERSION"),
        (inside_repo, f"--output-dir {output_dir} is inside the repo"),
    )
    for failed, message in checks:
        if failed:
            error(message)
            return 1, None
    runner_temp = os.environ.get("RUNNER_TEMP") or None
    out = output_dir or Path(
        tempfile.mkdtemp(prefix="hyperi-ci-chart-", dir=runner_temp)
    )
    out = out.resolve()
    try:
        with tempfile.TemporaryDirectory(prefix="hyperi-ci-assemble-") as tmp:
            scratch = Path(tmp)
            contract = load_contract(str(setting), root, scratch, binary)
            if library_dir is None:
                if not _ensure_helm():
                    return 1, None
                library_dir = _pull_library(registry, str(library), scratch)
            out.mkdir(parents=True, exist_ok=True)
            chart = assemble(
                contract,
                library_dir,
                out,
                version=str(version),
                image=image,
                library=str(library),
                registry=registry,
            )
    except ChartError as exc:
        error(str(exc))
        return 1, None
    success(f"Assembled {chart.name} {version} in {chart}")
    return 0, chart
