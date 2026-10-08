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
skeleton into a chart that depends on scalo-service. ``files/contract.json`` is
the contract's own bytes. The files derived from it have sorted keys and no
timestamp, so the same inputs give the same bytes.

The library pulls ``<image_registry>/<app_name>`` at the chart's appVersion and
the digest in values, so the image given must be that repository.

``values.schema.json`` is the skeleton's own schema for the library values,
plus the ``config_schema`` nodes marked ``x-scalo-dial: big|small``, each
copied with its ``$ref``s inlined, under ``config.<path>``. Other config keys
reach the app through ``configOverrides`` and are not validated, so a stored
overlay never pins a key the app later drops. ``values.yaml`` sets ``config: {}`` and lists the same
dials commented out, so the app's own defaults stand until one is set.

The library's ``lint-skip.yaml`` names the scanner findings a thin chart
accepts by design. Its Checkov ids become ``quality.checkov.skip`` in the
chart's own ``.hyperi-ci.yaml``, which ``hyperi-ci lint-iac <chart>`` loads
and nothing run over the repo ever sees. ``.helmignore`` keeps that file out
of the packaged chart.
"""

import json
import math
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
# Rides the Build job's dist/ artefact to Tag & Release. A subdirectory, as the
# GitHub Release and R2 uploads take only dist/'s top-level files.
EMITTED_DIR = "dist/chart-contract"
SKELETON_DIR = "skeleton"
VALUES_SCHEMA = "values.schema.json"
SCHEMA_PATH = "schema/deployment-contract.v{version}.schema.json"
# A dial path segment is written into values.yaml and split on dots, so it is
# held to a plain property name.
DIAL_SEGMENT_RE = re.compile(r"[A-Za-z0-9_-]+")
DIAL_KEY = "x-scalo-dial"
DIAL_TIERS = ("big", "small")
LINT_SKIP = "lint-skip.yaml"
CHART_CONFIG = ".hyperi-ci.yaml"
HELMIGNORE = ".helmignore"
# checkov splits --skip-check on commas, so an id is held to one plain word.
CHECK_ID_RE = re.compile(r"[A-Za-z0-9_]+")

# The tag becomes the chart's appVersion, so a digest-only ref is refused.
IMAGE_RE = re.compile(
    r"(?P<repo>[^@\s]+):(?P<tag>[A-Za-z0-9_][A-Za-z0-9._-]{0,127})"
    r"@(?P<digest>sha256:[0-9a-f]{64})"
)
# The chart directory and every object the library renders, a Service among
# them, take this name, so it must be an RFC 1035 label whatever the schema says.
NAME_RE = re.compile(r"[a-z]([-a-z0-9]{0,61}[a-z0-9])?")


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


def lint_skips(library_dir: Path, library: str) -> dict[str, str]:
    """Return the Checkov ids the library's ``lint-skip.yaml`` accepts, with reasons.

    A library without the file accepts nothing. Scanners other than Checkov
    are ignored.

    Raises:
        ChartError: The file is not a mapping, or a Checkov entry is not an
            id with a reason.

    """
    path = library_dir / LINT_SKIP
    if not path.is_file():
        return {}
    where = f"{LIBRARY} {library} {LINT_SKIP}"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ChartError(f"{where}: {exc}") from exc
    if not isinstance(data, dict):
        raise ChartError(f"{where} is not a mapping")
    checks = data.get("checkov") or {}
    if not isinstance(checks, dict):
        raise ChartError(f"{where}: checkov is not a mapping of id to reason")
    for check, reason in checks.items():
        if not isinstance(check, str) or not CHECK_ID_RE.fullmatch(check):
            raise ChartError(f"{where}: checkov id {check!r} is not a check id")
        if not isinstance(reason, str) or not reason.strip():
            raise ChartError(f"{where}: checkov {check} has no reason")
    return dict(sorted(checks.items()))


def chart_config(skips: dict[str, str], library: str) -> str:
    """Build the chart's ``.hyperi-ci.yaml``: the Checkov skips, reasons as comments."""
    lines = [
        f"# Written by hyperi-ci chart assemble from {LIBRARY} {library} {LINT_SKIP}.",
        "# Checkov findings this chart accepts by design:",
    ]
    lines += [
        f"#   {check}: {' '.join(reason.split())}" for check, reason in skips.items()
    ]
    body = {"quality": {"checkov": {"skip": list(skips)}}}
    return "\n".join(lines) + "\n" + yaml.safe_dump(body, sort_keys=True)


def _ignore_chart_config(chart: Path) -> None:
    """Add the chart's ``.hyperi-ci.yaml`` to its ``.helmignore``."""
    path = chart / HELMIGNORE
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if CHART_CONFIG in text.splitlines():
        return
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text + CHART_CONFIG + "\n", encoding="utf-8", newline="\n")


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


def _read_bytes(path: Path, what: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ChartError(f"cannot read {what} {path}: {exc}") from exc


def _refuse_constant(name: str) -> object:
    raise ValueError(f"{name} is not a JSON number")


def _finite(text: str) -> float:
    number = float(text)
    if not math.isfinite(number):
        raise ValueError(f"{text} is out of range for a JSON number")
    return number


def _parse_json(raw: bytes, what: str) -> dict:
    """Parse ``raw`` as the library reads it: UTF-8, finite numbers only.

    Raises:
        ChartError: ``raw`` is not UTF-8, not JSON, carries NaN or Infinity,
            or is not an object.

    """
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ChartError(f"{what} is not UTF-8: {exc}") from exc
    try:
        data = json.loads(text, parse_constant=_refuse_constant, parse_float=_finite)
    except ValueError as exc:
        raise ChartError(f"{what} is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ChartError(f"{what} is not a JSON object")
    return data


def _read_json(path: Path, what: str) -> dict:
    return _parse_json(_read_bytes(path, what), f"{what} {path}")


def assemble(
    raw_contract: bytes,
    library_dir: Path,
    out_dir: Path,
    *,
    version: str,
    image: str,
    library: str,
    registry: str,
) -> Path:
    """Write the thin chart for a contract to ``out_dir/<app_name>``.

    Args:
        raw_contract: The deployment contract as the app emitted or the repo
            commits it. ``files/contract.json`` is these bytes unchanged.
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
            directory already exists or cannot be written. Nothing is left
            at the chart directory in any of these cases.

    """
    ref = IMAGE_RE.fullmatch(image)
    if ref is None:
        raise ChartError(f"image {image!r} is not <repo>:<tag>@sha256:<digest>")
    contract = _parse_json(raw_contract, "the contract")
    name = contract.get("app_name")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ChartError(
            f"app_name {name!r} is not a Kubernetes Service name: 1 to 63 "
            "lowercase letters, digits and inner hyphens, starting with a letter"
        )
    schema_file = library_dir / SCHEMA_PATH.format(
        version=contract.get("schema_version")
    )
    if not schema_file.is_file():
        raise ChartError(
            f"{LIBRARY} {library} ships no {schema_file.relative_to(library_dir)} "
            f"for contract schema_version {contract.get('schema_version')!r}"
        )
    validate_contract(contract, _read_json(schema_file, "contract schema"))
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

    config_schema = contract.get("config_schema")
    # An absent, null or boolean config_schema has no nodes, so no dials.
    if not isinstance(config_schema, dict):
        config_schema = {}
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
    skips = lint_skips(library_dir, library)
    files = {
        "Chart.yaml": yaml.safe_dump(meta, sort_keys=True),
        "values.yaml": values_yaml(ref["digest"], dials),
        VALUES_SCHEMA: _json(schema),
    }
    if skips:
        files[CHART_CONFIG] = chart_config(skips, library)

    # The chart is built beside its target and renamed in, so a failure
    # leaves nothing at the target.
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{name}-", dir=out_dir))
    except OSError as exc:
        raise ChartError(f"cannot create {chart}: {exc}") from exc
    try:
        built = staging / name
        shutil.copytree(skeleton, built)
        if skips:
            _ignore_chart_config(built)
        for rel, text in files.items():
            (built / rel).write_text(text, encoding="utf-8", newline="\n")
        (built / "files").mkdir(exist_ok=True)
        (built / "files" / "contract.json").write_bytes(raw_contract)
        built.rename(chart)
    except (OSError, UnicodeError) as exc:
        raise ChartError(f"cannot write {chart}: {exc}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)
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


def _committed_contract(setting: str, root: Path) -> bytes:
    try:
        path = confine(setting, root, key="release.helm.contract")
    except RepoPathError as exc:
        raise ChartError(str(exc)) from exc
    return _read_bytes(path, "contract")


def load_contract(setting: str, root: Path, scratch: Path, binary: str | None) -> bytes:
    """Return the bytes of the contract ``release.helm.contract`` names.

    The setting is ``emit`` or the path of a committed contract.

    Raises:
        ChartError: The producer failed, or the contract cannot be read.

    """
    if setting != "emit":
        return _committed_contract(setting, root)
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
    return _read_bytes(emitted / CONTRACT_FILE, "emitted contract")


def emit_contract(config: CIConfig, root: Path) -> int:
    """Leave the emitted contract in ``dist/`` for the release tail, when one is emitted.

    Tag & Release holds the publish credentials and runs no repo code, so the
    app's ``generate-artefacts`` runs here, in the Build job, beside the binary
    it just built. Nothing happens unless ``release.helm.contract`` is ``emit``.

    Returns:
        0 when nothing is emitted or the contract was written, 1 on failure.

    """
    if not config.get("release.helm.enabled", False):
        return 0
    if config.get("release.helm.contract") != "emit":
        return 0
    target = root / EMITTED_DIR / CONTRACT_FILE
    try:
        with tempfile.TemporaryDirectory(prefix="hyperi-ci-emit-") as tmp:
            raw = load_contract("emit", root, Path(tmp), None)
        _parse_json(raw, "the emitted contract")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    except (ChartError, OSError) as exc:
        error(f"release.helm.contract is emit: {exc}")
        return 1
    success(f"Emitted the deployment contract to {target.relative_to(root)}")
    return 0


def release_contract(setting: str, root: Path) -> bytes:
    """Return the contract the release tail assembles from, running no repo code.

    ``emit`` reads what :func:`emit_contract` left in ``dist/``. A path reads
    the committed file.

    Raises:
        ChartError: The contract is missing or cannot be read.

    """
    if setting != "emit":
        return _committed_contract(setting, root)
    path = root / EMITTED_DIR / CONTRACT_FILE
    if path.is_symlink() or not path.is_file():
        raise ChartError(
            f"release.helm.contract is emit, but the Build job left no "
            f"{EMITTED_DIR}/{CONTRACT_FILE} in its dist/ artefact"
        )
    return _read_bytes(path, "emitted contract")


def _pull_library(registry: str, library: str, scratch: Path) -> Path:
    # Anonymous first: a token without read:packages is refused even for a
    # public chart, so the job token is only tried after an anonymous miss.
    pull = (
        "pull",
        f"{registry}/{LIBRARY}",
        "--version",
        library,
        "--untar",
        "--untardir",
        str(scratch),
    )
    rc, out = _helm(*pull, registry=registry)
    if rc != 0 and os.environ.get("GITHUB_TOKEN"):
        _login(registry)
        rc, out = _helm(*pull, registry=registry)
    if rc != 0:
        raise ChartError(f"helm pull {LIBRARY} {library} failed:\n{out}")
    return scratch / LIBRARY


def build_dependency(chart: Path, registry: str) -> None:
    """Fetch scalo-service into ``chart/charts``, which a render needs first.

    Raises:
        ChartError: helm could not fetch it. The chart directory is removed.

    """
    rc, out = _helm("dependency", "build", str(chart), registry=registry)
    if rc != 0:
        shutil.rmtree(chart)
        raise ChartError(f"helm dependency build {chart.name} failed:\n{out}")


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
    contract: bytes | None = None,
) -> tuple[int, Path | None]:
    """Assemble the thin chart ``release.helm`` describes, if it describes one.

    A library pulled from the registry is also built into the chart's
    ``charts/``. A ``library_dir`` is read offline, so the chart is left
    for ``helm dependency build``. ``contract`` replaces the one
    ``release.helm.contract`` names, so the release tail runs no producer.

    Returns:
        ``(exit code, chart directory)``. The directory is None when the
        project assembles no chart or the assembly failed.

    """
    setting = config.get("release.helm.contract")
    library = config.get("release.helm.library")
    if not config.get("release.helm.enabled", False):
        info("release.helm.enabled is off -- no chart to assemble")
        return 0, None
    if not setting:
        info("release.helm.contract is not set -- no chart to assemble")
        return 0, None
    registry = (registry or config.get("release.helm.registry") or "").rstrip("/")
    try:
        version = version or resolve_release_version()
    except ReleaseVersionError as exc:
        error(str(exc))
        return 1, None
    runner_temp = os.environ.get("RUNNER_TEMP") or None
    if output_dir is not None:
        parent, where = output_dir.resolve(), "--output-dir"
    else:
        parent = Path(runner_temp or tempfile.gettempdir()).resolve()
        where = "the chart temp dir under"
    checks = (
        (not library, "release.helm.library is not set"),
        (
            not registry.startswith("oci://"),
            f"Helm registry must be oci://: {registry!r}",
        ),
        (not version, "No release version -- pass --version or set HYPERCI_VERSION"),
        (
            parent.is_relative_to(root.resolve()),
            f"{where} {parent} is inside the repo",
        ),
    )
    for failed, message in checks:
        if failed:
            error(message)
            return 1, None
    out = (
        parent
        if output_dir is not None
        else Path(tempfile.mkdtemp(prefix="hyperi-ci-chart-", dir=parent))
    )
    try:
        chart = _assemble_from_config(
            config,
            root,
            out,
            image=image,
            library_dir=library_dir,
            binary=binary,
            registry=registry,
            version=str(version),
            contract=contract,
        )
    except ChartError as exc:
        error(str(exc))
        if output_dir is None:
            shutil.rmtree(out, ignore_errors=True)
        return 1, None
    success(f"Assembled {chart.name} {version} in {chart}")
    return 0, chart


def _assemble_from_config(
    config: CIConfig,
    root: Path,
    out: Path,
    *,
    image: str,
    library_dir: Path | None,
    binary: str | None,
    registry: str,
    version: str,
    contract: bytes | None = None,
) -> Path:
    setting = str(config.get("release.helm.contract"))
    library = str(config.get("release.helm.library"))
    with tempfile.TemporaryDirectory(prefix="hyperi-ci-assemble-") as tmp:
        scratch = Path(tmp)
        raw_contract = (
            contract
            if contract is not None
            else load_contract(setting, root, scratch, binary)
        )
        pulled = library_dir is None
        if library_dir is None:
            if not _ensure_helm():
                raise ChartError("helm is not on PATH and could not be installed")
            library_dir = _pull_library(registry, library, scratch)
        chart = assemble(
            raw_contract,
            library_dir,
            out,
            version=version,
            image=image,
            library=library,
            registry=registry,
        )
        if pulled:
            build_dependency(chart, registry)
        else:
            info(
                f"{LIBRARY} was read from {library_dir}: run helm dependency "
                f"build on {chart} before rendering it"
            )
    return chart
