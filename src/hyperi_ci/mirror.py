# Project:   HyperI CI
# File:      src/hyperi_ci/mirror.py
# Purpose:   Keep the Docker Hub images a repo pins copied to the GHCR mirror
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""``hyperi-ci mirror``: the Docker Hub images a repo pins, served from GHCR.

``ensure`` runs in a consumer's CI before its tests. It reads the checkout's
pins, asks GHCR anonymously which are missing, and dispatches the mirror
workflow for those. ``copy`` is that workflow's half: it copies each pin with
skopeo, so the digest a consumer pins is the digest the mirror serves. GHCR
rejects App tokens and only the publishing repo's job token can write a
package, which is why the copy runs in one repo and consumers only dispatch.
"""

import fnmatch
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from hyperi_ci.common import error, info, run_cmd, success, warn
from hyperi_ci.config import OrgConfig

_DOCKER_HUB_HOSTS = ("docker.io/", "index.docker.io/", "registry-1.docker.io/")
_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_NAME = re.compile(rf"{_COMPONENT}(?:/{_COMPONENT})*")
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_LITERAL = re.compile(
    r"(?<![\w./:@-])(?P<ref>(?:[\w.-]+(?::\d+)?/)?[a-z0-9][\w./-]*:[\w][\w.-]{0,127}"
    r"@sha256:[0-9a-f]{64})"
)
_FROM = re.compile(
    r"^\s*FROM\s+(?:--\S+\s+)*(?P<ref>\S+)", re.IGNORECASE | re.MULTILINE
)
_IMAGE = re.compile(r"^\s*-?\s*image:\s*[\"']?(?P<ref>[^\s\"'#]+)", re.MULTILINE)
_SCANNED_SUFFIXES = {".rs", ".py", ".sh", ".yml", ".yaml", ".toml"}
_MAX_FILE_BYTES = 2_000_000
_MAX_PINS = 200
_MANIFEST_TYPES = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


class MirrorError(RuntimeError):
    """The mirror could not be read or written."""


@dataclass(frozen=True, slots=True, order=True)
class Pin:
    """One Docker Hub image a repo pulls.

    Attributes:
        name: Docker Hub repository, ``library/`` form for official images.
        tag: The tag the repo names.
        digest: ``sha256:...`` the repo pins, empty for a tag-only reference.
    """

    name: str
    tag: str
    digest: str = ""

    @property
    def source(self) -> str:
        """Skopeo source, by digest where there is one."""
        if self.digest:
            return f"docker://docker.io/{self.name}@{self.digest}"
        return f"docker://docker.io/{self.name}:{self.tag}"

    def mirrored(self, prefix: str) -> str:
        """The mirror reference that serves this pin."""
        return f"{prefix}/{self.name}:{self.tag}"


def dockerhub_name(name: str) -> str | None:
    """Docker Hub repository for an image name, None for any other registry."""
    for host in _DOCKER_HUB_HOSTS:
        name = name.removeprefix(host)
    first, sep, _ = name.partition("/")
    if sep and ("." in first or ":" in first or first == "localhost"):
        return None
    full = name if sep else f"library/{name}"
    return full if _NAME.fullmatch(full) else None


def parse_ref(ref: str) -> Pin | None:
    """Pin for a tagged Docker Hub reference, None for anything else."""
    name, _, digest = ref.partition("@")
    if digest and not _DIGEST.fullmatch(digest):
        return None
    head, sep, tag = name.rpartition(":")
    hub = dockerhub_name(head) if sep else None
    if hub is None or not _TAG.fullmatch(tag):
        return None
    return Pin(hub, tag, digest)


@dataclass(frozen=True, slots=True)
class AnnotationPattern:
    """One Renovate regex manager, compiled for Python."""

    files: tuple[re.Pattern[str], ...]
    match: re.Pattern[str]
    datasource: str | None


def _file_pattern(pattern: str) -> re.Pattern[str]:
    """Renovate file pattern: ``/regex/`` or a glob."""
    body = pattern.rstrip("i")
    if len(body) > 1 and body[0] == "/" and body[-1] == "/":
        return re.compile(body[1:-1])
    return re.compile(fnmatch.translate(pattern))


def renovate_patterns(config: dict) -> list[AnnotationPattern]:
    """The regex managers of a Renovate config, with JS named groups rewritten."""
    patterns = []
    for manager in config.get("customManagers") or []:
        if manager.get("customType") != "regex":
            continue
        files = manager.get("managerFilePatterns") or manager.get("fileMatch") or []
        compiled = tuple(_file_pattern(f) for f in files)
        for raw in manager.get("matchStrings") or []:
            python = re.sub(r"\(\?<(?=[A-Za-z])", "(?P<", raw)
            patterns.append(
                AnnotationPattern(
                    compiled, re.compile(python), manager.get("datasourceTemplate")
                )
            )
    return patterns


def _digest_after(text: str, start: int, *, lines: int = 6) -> str:
    """The first quoted digest in the lines after an annotated tag, or "".

    The shared Renovate regex allows one comment line between a tag and its
    digest, and a blank line or a second comment line puts it out of reach.
    The mirror still has to serve the digest the code pins.
    """
    window = "\n".join(text[start:].split("\n", lines)[:lines])
    window = window.split("renovate:", 1)[0]
    found = re.search(r"[\"'](sha256:[0-9a-f]{64})[\"']", window)
    return found[1] if found else ""


def _built_images(text: str) -> set[str]:
    """The ``image:`` of each compose service with ``build:``, which tags a local build."""
    try:
        docs = list(yaml.safe_load_all(text))
    except yaml.YAMLError:
        return set()
    built = set()
    for doc in docs:
        services = doc.get("services") if isinstance(doc, dict) else None
        if not isinstance(services, dict):
            continue
        for service in services.values():
            if isinstance(service, dict) and "build" in service:
                if isinstance(image := service.get("image"), str):
                    built.add(image)
    return built


def extract(path: str, text: str, patterns: Sequence[AnnotationPattern]) -> set[Pin]:
    """Docker Hub pins in one file.

    Args:
        path: Repo-relative path, matched against each manager's file patterns.
        text: The file's content.
        patterns: From :func:`renovate_patterns`.

    Returns:
        Every pin found. A tag-only pin is dropped where the same tag is also
        pinned by digest.
    """
    pins: set[Pin] = set()
    for pattern in patterns:
        if not any(f.search(path) for f in pattern.files):
            continue
        for match in pattern.match.finditer(text):
            groups = match.groupdict()
            if (groups.get("datasource") or pattern.datasource) != "docker":
                continue
            hub = dockerhub_name(groups.get("depName") or "")
            tag = groups.get("currentValue") or ""
            if hub and _TAG.fullmatch(tag):
                digest = groups.get("currentDigest") or _digest_after(text, match.end())
                pins.add(Pin(hub, tag, digest))
    refs = [m["ref"] for m in _LITERAL.finditer(text)]
    base = Path(path).name
    if base.startswith(("Dockerfile", "Containerfile")) or base.endswith(".Dockerfile"):
        refs += [m["ref"] for m in _FROM.finditer(text)]
    if Path(path).suffix in {".yml", ".yaml"}:
        built = _built_images(text)
        refs += [m["ref"] for m in _IMAGE.finditer(text) if m["ref"] not in built]
    pins.update(pin for ref in refs if (pin := parse_ref(ref)))
    pinned = {(pin.name, pin.tag) for pin in pins if pin.digest}
    return {pin for pin in pins if pin.digest or (pin.name, pin.tag) not in pinned}


def _scanned(path: str) -> bool:
    base = Path(path).name
    return (
        Path(path).suffix in _SCANNED_SUFFIXES
        or base.startswith(("Dockerfile", "Containerfile"))
        or base.endswith(".Dockerfile")
    )


def scan_tree(root: Path, patterns: Sequence[AnnotationPattern]) -> set[Pin]:
    """Docker Hub pins across the git-tracked files under ``root``."""
    listed = run_cmd(["git", "ls-files", "-z"], capture=True, cwd=root).stdout
    pins: set[Pin] = set()
    for rel in filter(_scanned, listed.split("\0")):
        file = root / rel
        if not file.is_file() or file.stat().st_size > _MAX_FILE_BYTES:
            continue
        pins |= extract(
            rel, file.read_text(encoding="utf-8", errors="replace"), patterns
        )
    return pins


def load_patterns(org: OrgConfig, *, attempts: int = 3) -> list[AnnotationPattern]:
    """The shared Renovate regex managers, or none when the config is unreachable."""
    for attempt in range(1, attempts + 1):
        try:
            url = org.mirror_renovate_config
            # nosemgrep: dynamic-urllib-use-detected -- the https URL comes from the packaged org.yaml
            with urllib.request.urlopen(url, timeout=20) as resp:  # noqa: S310
                return renovate_patterns(json.load(resp))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            if attempt == attempts:
                warn(
                    f"::warning::Renovate config unreadable ({exc}): annotated "
                    "pins go unchecked, literal and FROM references still count"
                )
            else:
                time.sleep(2 * attempt)
    return []


def is_mirrored(pin: Pin, prefix: str) -> bool:
    """Whether the mirror serves ``pin``, asked anonymously.

    http.client follows no redirect, so the bearer token never leaves the
    registry.

    Raises:
        MirrorError: GHCR answered with anything but present or absent, or
            could not be reached.
    """
    registry, _, path = prefix.partition("/")
    repository = f"{path}/{pin.name}"
    # nosemgrep: httpsconnection-detected -- 3.14 verifies the certificate by default
    conn = http.client.HTTPSConnection(registry, timeout=20)
    try:
        conn.request("GET", f"/token?scope=repository:{repository}:pull")
        resp = conn.getresponse()
        body = resp.read()
        # GHCR refuses an anonymous token for a package that does not exist.
        if resp.status in (401, 403, 404):
            return False
        token = json.loads(body).get("token") if resp.status == 200 else None
        if not token:
            raise MirrorError(f"{registry} token for {repository}: HTTP {resp.status}")
        headers = {"Authorization": f"Bearer {token}", "Accept": _MANIFEST_TYPES}
        ref = pin.digest or pin.tag
        conn.request("HEAD", f"/v2/{repository}/manifests/{ref}", headers=headers)
        resp = conn.getresponse()
        resp.read()
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise MirrorError(f"{pin.mirrored(prefix)}: {exc}") from exc
    finally:
        conn.close()
    if resp.status in (200, 404):
        return resp.status == 200
    raise MirrorError(f"{pin.mirrored(prefix)}: HTTP {resp.status}")


def parse_pins(payload: str) -> list[Pin]:
    """Pins from the mirror workflow's input, each one checked.

    Raises:
        ValueError: Not a JSON list of pins, too many, or one fails its check.
    """
    raw = json.loads(payload)
    if not isinstance(raw, list) or len(raw) > _MAX_PINS:
        raise ValueError(f"expected a JSON list of at most {_MAX_PINS} pins")
    pins = []
    for entry in raw:
        try:
            pin = Pin(**entry) if isinstance(entry, dict) else None
        except TypeError:
            pin = None
        if (
            pin is None
            or dockerhub_name(pin.name) != pin.name
            or not _TAG.fullmatch(pin.tag)
            or (pin.digest and not _DIGEST.fullmatch(pin.digest))
        ):
            raise ValueError(f"not a Docker Hub pin: {entry!r}")
        pins.append(pin)
    return pins


def _login(registry: str, user: str, secret: str) -> None:
    run_cmd(
        ["skopeo", "login", registry, "-u", user, "--password-stdin"],
        capture=True,
        stdin_text=secret,
    )


def copy(pins: Iterable[Pin], prefix: str) -> int:
    """Copy each pin the mirror lacks, keeping its digest.

    Logs in to the mirror with ``GITHUB_TOKEN``, and to Docker Hub with
    ``DOCKERHUB_READ_USERNAME`` / ``DOCKERHUB_READ_TOKEN`` when both are set.

    Returns:
        0 when every pin is mirrored, 1 when a copy failed.
    """
    registry = prefix.partition("/")[0]
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        error(f"no GITHUB_TOKEN to push to {registry}")
        return 1
    _login(registry, os.environ.get("GITHUB_ACTOR", "hyperi-ci"), token)
    hub_user = os.environ.get("DOCKERHUB_READ_USERNAME", "")
    hub_token = os.environ.get("DOCKERHUB_READ_TOKEN", "")
    if hub_user and hub_token:
        _login("docker.io", hub_user, hub_token)
    failed = 0
    for pin in sorted(set(pins)):
        target = pin.mirrored(prefix)
        if is_mirrored(pin, prefix):
            info(f"present  {target}")
            continue
        cmd = ["skopeo", "copy", "--all", "--preserve-digests", "--retry-times", "3"]
        result = run_cmd([*cmd, pin.source, f"docker://{target}"], check=False)
        if result.returncode == 0:
            success(f"copied   {target} {pin.digest}")
        else:
            error(f"failed   {target} from {pin.source}")
            failed += 1
    return 1 if failed else 0


def _find_run(org: OrgConfig, request: str, *, wait: float = 90) -> str | None:
    """Id of the dispatched mirror run whose title carries ``request``."""
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        repo = f"{org.github_org}/{org.mirror_repo}"
        fields = "databaseId,displayTitle"
        cmd = ["gh", "run", "list", "-R", repo, "-w", org.mirror_workflow]
        cmd += ["-e", "workflow_dispatch", "-L", "20", "--json", fields]
        listed = run_cmd(cmd, capture=True, check=False)
        if listed.returncode == 0:
            for run in json.loads(listed.stdout or "[]"):
                if request in run.get("displayTitle", ""):
                    return str(run["databaseId"])
        time.sleep(5)
    return None


def dispatch(pins: Sequence[Pin], org: OrgConfig, *, timeout: int) -> int:
    """Run the mirror workflow for ``pins`` and wait for it.

    Returns:
        The mirror run's exit code, 1 when it never started.
    """
    from hyperi_ci.watch import watch_run

    request = uuid.uuid4().hex[:12]
    repo = f"{org.github_org}/{org.mirror_repo}"
    payload = json.dumps([asdict(pin) for pin in pins])
    cmd = ["gh", "workflow", "run", org.mirror_workflow, "-R", repo]
    cmd += ["-f", f"pins={payload}", "-f", f"request={request}"]
    started = run_cmd(cmd, capture=True, check=False)
    if started.returncode != 0:
        error(f"could not dispatch {org.mirror_workflow} in {repo}: {started.stderr}")
        return 1
    run_id = _find_run(org, request)
    if run_id is None:
        error(f"mirror run for request {request} never appeared in {repo}")
        return 1
    info(f"mirror run https://github.com/{repo}/actions/runs/{run_id}")
    return watch_run(run_id=run_id, repo=repo, timeout=timeout)


def _missing(pins: Iterable[Pin], prefix: str) -> list[Pin]:
    """Pins the mirror lacks. One GHCR could not answer for is warned and left out."""
    missing = []
    for pin in pins:
        try:
            if not is_mirrored(pin, prefix):
                missing.append(pin)
        except MirrorError as exc:
            warn(f"::warning::could not check {pin.mirrored(prefix)}: {exc}")
    return missing


def ensure(root: Path, org: OrgConfig, *, timeout: int, strict: bool = False) -> int:
    """Have the mirror serve every Docker Hub pin under ``root``.

    A pin still missing afterwards is a warning, not a failure: a fixture can
    hold a well-formed reference nothing pulls, and a test that does pull a
    missing image fails on its own, naming it. Dispatching needs ``GH_TOKEN``
    with actions access to the mirror repo, which a fork PR lacks.

    Returns:
        0, or 1 under ``strict`` when a pin is still missing.
    """
    prefix = org.mirror_prefix
    pins = sorted(scan_tree(root, load_patterns(org)))
    missing = _missing(pins, prefix)
    info(f"{len(pins)} Docker Hub pin(s), {len(missing)} missing from {prefix}")
    if missing and os.environ.get("GH_TOKEN"):
        dispatch(missing, org, timeout=timeout)
        missing = _missing(missing, prefix)
    elif missing:
        warn("::warning::no GH_TOKEN to dispatch the mirror")
    for pin in missing:
        warn(f"::warning::not mirrored: {pin.mirrored(prefix)} {pin.digest}")
    return 1 if strict and missing else 0
