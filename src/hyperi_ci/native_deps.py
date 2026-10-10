# Project:   HyperI CI
# File:      src/hyperi_ci/native_deps.py
# Purpose:   Detect and install native system dependencies from per-language config
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Detect and install native system dependencies for CI builds.

Reads per-language YAML config from config/native-deps/{language}.yaml,
scans project manifest files for known patterns, and installs missing
apt packages on Linux. No-ops on non-Linux platforms.
"""

import io
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from scalo import logger

from hyperi_ci.apt_retry import (
    APT_RETRY_OPTION,
    APT_UPDATE_ATTEMPTS,
    APT_UPDATE_BACKOFF_SECONDS,
)
from hyperi_ci.common import (
    URL_ERRORS,
    curl_read,
    download_artefact,
    info,
    run_cmd,
    sudo_prefix,
    url_read,
    warn,
)
from hyperi_ci.llvm_version import (
    LLVMVersionError,
    default_llvm_major,
    designated_llvm_version,
)

_LLVM_PLACEHOLDER = "${HYPERCI_LLVM_VERSION}"
_LLVM_DEFAULT_PLACEHOLDER = "${HYPERCI_LLVM_DEFAULT}"

# Fallback codenames for an APT repo that does not ship the current OS codename,
# newest Ubuntu LTS first, then older LTS, then Debian stable (trixie, for ESH).
_FALLBACK_CODENAMES = ["resolute", "noble", "jammy", "focal", "trixie"]

_CONFIG_ROOT = Path(__file__).resolve().parent / "config"
_NATIVE_DEPS_DIR = _CONFIG_ROOT / "native-deps"
_TOOLCHAINS_DIR = _CONFIG_ROOT / "toolchains"


# Categories map to config subdirectories sharing one YAML schema (patterns,
# manifest_files, dpkg_check, apt_repos, apt_packages). native-deps installs
# only if a manifest matches. toolchains (the apt families the image bakes: the
# default LLVM major) match in --auto mode and --all bypasses the pattern check.
_CATEGORY_DIRS: dict[str, Path] = {
    "native-deps": _NATIVE_DEPS_DIR,
    "toolchains": _TOOLCHAINS_DIR,
}


@dataclass
class AptRepo:
    """An APT repository to add before installing packages.

    If codename is "auto" (default), the current OS codename is tried first.
    If the repo doesn't support it, LTS codenames are tried in reverse order.

    `key_fingerprint` is the primary-key fingerprint the download must carry
    (spaces optional, case-insensitive); empty means no check.
    """

    key_url: str
    keyring: str
    url: str
    codename: str = "auto"
    components: str = "main"
    key_fingerprint: str = ""


@dataclass
class DepGroup:
    """A group of related native packages triggered by manifest patterns.

    `bake` controls `--all` mode (runner image bake). True (default) installs
    unconditionally. False skips it in --all and installs at job time when
    manifest patterns match, for a toolset whose packages `Conflicts:` across
    versions, where a baked default would lock out jobs needing another.
    """

    name: str
    patterns: list[str]
    manifest_files: list[str]
    dpkg_check: str
    apt_packages: list[str] = field(default_factory=list)
    apt_repos: list[AptRepo] = field(default_factory=list)
    dpkg_min_version: str = ""
    bake: bool = True


def _expand_template_vars(text: str, project_dir: Path | None = None) -> str:
    """Expand ${VAR} placeholders in YAML configs.

    Recognised variables:
      HYPERCI_LLVM_VERSION  -- the designated LLVM/BOLT major, from
                              ``llvm_version.designated_llvm_version`` (env var,
                              then `build.rust.llvm_version`, then versions.yaml
                              `llvm`).
      HYPERCI_LLVM_DEFAULT  -- versions.yaml `llvm` alone, from
                              ``llvm_version.default_llvm_major``. The image
                              bakes this one so nothing else can move the bake.
      OS_CODENAME           -- the codename from lsb_release -cs (noble, trixie,
                              resolute), for distro-specific apt.llvm.org
                              subpaths.

    Unknown ${VAR} placeholders pass through so apt-cache reports "package not
    found" instead of mis-resolving.

    Args:
        text: The raw YAML text.
        project_dir: Project root whose .hyperi-ci.yaml is read. Defaults to cwd.

    Raises:
        LLVMVersionError: The text names an LLVM placeholder and the version
            it resolves to is not a whole-number major.

    """
    # Resolved only when named, so a bad value cannot fail a YAML that never uses it.
    if _LLVM_PLACEHOLDER in text:
        llvm_version = str(designated_llvm_version(project_dir).major)
        text = text.replace(_LLVM_PLACEHOLDER, llvm_version)
    if _LLVM_DEFAULT_PLACEHOLDER in text:
        text = text.replace(_LLVM_DEFAULT_PLACEHOLDER, str(default_llvm_major()))
    os_codename = os.environ.get("OS_CODENAME") or _get_os_codename() or "noble"
    return text.replace("${OS_CODENAME}", os_codename)


def _dep_group_from_entry(entry: dict) -> DepGroup:
    """Materialise one DepGroup from a YAML entry."""
    return DepGroup(
        name=entry["name"],
        patterns=entry.get("patterns", []),
        manifest_files=entry.get("manifest_files", []),
        dpkg_check=entry["dpkg_check"],
        apt_packages=list(entry.get("apt_packages", [])),
        bake=entry.get("bake", True),
        apt_repos=[
            AptRepo(
                key_url=r["key_url"],
                keyring=r["keyring"],
                url=r["url"],
                codename=r.get("codename", "auto"),
                components=r.get("components", "main"),
                key_fingerprint=r.get("key_fingerprint", ""),
            )
            for r in entry.get("apt_repos", [])
        ],
        dpkg_min_version=entry.get("dpkg_min_version", ""),
    )


def _load_dep_groups(
    language: str,
    category: str = "native-deps",
    project_dir: Path | None = None,
) -> list[DepGroup]:
    """Load dep group definitions from bundled config.

    ``project_dir`` is where the designated LLVM version's project config is
    read from.
    """
    config_dir = _CATEGORY_DIRS.get(category)
    if config_dir is None:
        logger.warning(f"Unknown config category: {category}")
        return []

    config_file = config_dir / f"{language}.yaml"
    if not config_file.exists():
        logger.warning(f"No {category} config for: {language}")
        return []

    text = config_file.read_text(encoding="utf-8")
    raw = yaml.safe_load(_expand_template_vars(text, project_dir))
    if not raw:
        return []

    return [_dep_group_from_entry(entry) for entry in raw]


def _read_manifests(project_dir: Path, manifest_files: list[str]) -> str:
    """Read all manifest files, concatenating their content for pattern matching."""
    content_parts: list[str] = []
    for filename in manifest_files:
        manifest_path = project_dir / filename
        if manifest_path.exists():
            content_parts.append(manifest_path.read_text(encoding="utf-8"))
    return "\n".join(content_parts)


def _patterns_match(content: str, patterns: list[str]) -> bool:
    """Return True if any pattern appears as a substring in content."""
    return any(pattern in content for pattern in patterns)


def _group_matches(group: DepGroup, project_dir: Path) -> bool:
    """Report whether ``group``'s manifest patterns match ``project_dir``."""
    content = _read_manifests(project_dir, group.manifest_files)
    return bool(content) and _patterns_match(content, group.patterns)


def group_matches(language: str, name: str, project_dir: Path) -> bool:
    """Report whether native-deps installs the group ``name`` for ``project_dir``.

    Raises:
        ValueError: ``language`` defines no native-deps group called ``name``.
        LLVMVersionError: The designated LLVM version is not a whole-number major.

    """
    for group in _load_dep_groups(language, project_dir=project_dir):
        if group.name == name:
            return _group_matches(group, project_dir)
    raise ValueError(f"no native-deps group {name!r} for {language}")


def _get_os_codename() -> str:
    """Get the current OS codename via lsb_release, or "" if unavailable.

    macOS has no `lsb_release`, so the `FileNotFoundError` is swallowed into an
    empty string, and `_expand_template_vars` then defaults to "noble".
    """
    try:
        result = subprocess.run(
            ["lsb_release", "-cs"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _repo_has_codename(repo_url: str, codename: str) -> bool:
    """Check whether the repo publishes a Release file for ``codename``.

    Only a 404 reads as "not published". Anything else that outlasts
    ``url_read``'s retries raises, so the caller can tell an unreachable repo
    from a missing codename.

    Raises:
        OSError: The repo could not be reached, or answered with an error
            other than 404.
        http.client.HTTPException: The reply was malformed or cut short.

    """
    url = f"{repo_url.rstrip('/')}/dists/{codename}/Release"
    try:
        # repo_url is a fixed https URL from the shipped native-deps/toolchains
        # YAML, never a consumer's config.
        url_read(urllib.request.Request(url, method="HEAD"), timeout=10)  # noqa: S310
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise
    return True


def _resolve_codename(repo: AptRepo) -> str:
    """Resolve the codename to use for an APT repo.

    An explicit codename is used directly. "auto" tries the current OS codename,
    then the fallbacks in order. Only a 404 moves to the next candidate: a probe
    with no answer keeps its candidate and warns, because skipping it would
    install from an older codename's repo on a network fault.
    """
    if repo.codename != "auto":
        return repo.codename

    os_codename = _get_os_codename()
    candidates = [os_codename] if os_codename else []
    candidates += [c for c in _FALLBACK_CODENAMES if c != os_codename]

    for codename in candidates:
        try:
            published = _repo_has_codename(repo.url, codename)
        except URL_ERRORS as exc:
            warn(
                f"Could not check {repo.url} for {codename!r} ({exc}). Using "
                f"{codename} unchecked rather than falling back on a network "
                "failure."
            )
            return codename
        if not published:
            continue
        if codename == os_codename:
            info(f"Repo {repo.url} supports current codename: {codename}")
        else:
            info(
                f"Repo {repo.url} does not support {os_codename!r}, "
                f"using fallback: {codename}"
            )
        return codename

    warn(f"No supported codename found for {repo.url}")
    return os_codename or _FALLBACK_CODENAMES[0]


def _get_dpkg_arch() -> str:
    """Get the current dpkg architecture (amd64, arm64, etc.)."""
    result = subprocess.run(
        ["dpkg", "--print-architecture"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout.strip() if result.returncode == 0 else "amd64"


def _repo_already_configured(repo_url: str, codename: str) -> Path | None:
    """Check if any existing APT sources file references this repo.

    Self-hosted runners may pre-configure apt.llvm.org under another filename,
    so this scans /etc/apt/sources.list.d/ and /etc/apt/sources.list for a line
    matching our url + codename, to avoid duplicates. Returns the first matching
    file, or None. The match is a substring, so `[signed-by=...]`, arch flags
    and components cannot cause false negatives.
    """
    # Match the scheme-less path: pre-provisioned runners use `http://` for
    # apt.llvm.org while we write `https://`.
    url_stripped = repo_url.rstrip("/")
    path_only = url_stripped.split("://", 1)[-1]  # e.g. "apt.llvm.org/noble"

    candidates = [Path("/etc/apt/sources.list")]
    sources_dir = Path("/etc/apt/sources.list.d")
    if sources_dir.is_dir():
        candidates.extend(sources_dir.glob("*.list"))
        candidates.extend(sources_dir.glob("*.sources"))

    for path in candidates:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # Covers both one-line `deb` and deb822 `.sources` files.
        if path_only in content and codename in content:
            return path
    return None


def _key_fingerprints(key_bytes: bytes) -> list[str]:
    """Return the primary-key fingerprints gpg reads out of an armoured key."""
    result = subprocess.run(
        ["gpg", "--show-keys", "--with-fingerprint", "--with-colons"],
        input=key_bytes,
        capture_output=True,
    )
    if result.returncode != 0:
        return []

    found: list[str] = []
    expect_fpr = False
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        if line.startswith("pub:"):
            expect_fpr = True
        elif expect_fpr and line.startswith("fpr:"):
            found.append(line.split(":")[9].upper())
            expect_fpr = False
    return found


def _verify_apt_key(key_bytes: bytes, expected: str) -> bool:
    """Check a downloaded APT key is the one the YAML declares.

    HTTPS proves who served the key, not which key it is, so without this a
    swapped upstream key would sign every package apt pulls from that repo.
    """
    wanted = expected.replace(" ", "").upper()
    found = _key_fingerprints(key_bytes)
    if not found:
        logger.error("Could not read a key fingerprint from the downloaded APT key")
        return False
    if wanted not in found:
        logger.error(
            f"APT key fingerprint mismatch: expected {wanted}, got {', '.join(found)}"
        )
        return False
    logger.info(f"APT key fingerprint verified: {wanted}")
    return True


def _add_apt_repo(repo: AptRepo) -> int:
    """Add a GPG key and APT sources entry for a repo. Returns exit code.

    A declared ``key_fingerprint`` is checked before the key reaches the keyring.

    Idempotent on three levels:
      1. Skips the download if the keyring file exists.
      2. Skips the write if our sources file already has the exact line.
      3. Skips the write if ANY other apt source file references the same
         url + codename (self-hosted runners with pre-configured repos).

    Entries can share a keyring (every apt.llvm.org entry uses
    `/usr/share/keyrings/llvm.gpg`) and the sources filename comes from the
    keyring stem, so writes APPEND: several `deb` lines against one keyring is
    valid apt syntax.
    """
    keyring_path = Path(repo.keyring)
    if keyring_path.exists():
        logger.info(f"APT keyring already exists: {repo.keyring}")
    else:
        logger.info(f"Adding APT key from {repo.key_url}")
        rc, key = curl_read(repo.key_url)
        if rc != 0:
            logger.error(f"Failed to download APT key from {repo.key_url}")
            return rc

        if repo.key_fingerprint and not _verify_apt_key(key, repo.key_fingerprint):
            logger.error(f"Refusing to install the APT key from {repo.key_url}")
            return 1

        dearmor = subprocess.run(
            [
                *sudo_prefix(),
                "gpg",
                "--batch",
                "--yes",
                "--dearmor",
                "-o",
                repo.keyring,
            ],
            input=key,
        )
        if dearmor.returncode != 0:
            logger.error(f"Failed to dearmor APT key to {repo.keyring}")
            return dearmor.returncode

    codename = _resolve_codename(repo)
    arch = _get_dpkg_arch()
    sources_line = (
        f"deb [signed-by={repo.keyring} arch={arch}] "
        f"{repo.url} {codename} {repo.components}"
    )

    existing = _repo_already_configured(repo.url, codename)
    if existing is not None:
        logger.info(
            f"APT source for {repo.url} {codename} already present in {existing}"
        )
        return 0

    sources_name = keyring_path.stem + ".list"
    sources_path = Path("/etc/apt/sources.list.d") / sources_name

    # Substring, not equality: llvm.list holds one line per LLVM major.
    if sources_path.exists() and sources_line in sources_path.read_text(
        encoding="utf-8"
    ):
        logger.info(f"APT source already configured: {sources_path}")
        return 0

    logger.info(f"Adding APT source: {sources_line}")
    # The leading newline keeps an appended line off the last one.
    prefix = "" if not sources_path.exists() else "\n"
    result = subprocess.run(
        [*sudo_prefix(), "tee", "-a", str(sources_path)],
        input=f"{prefix}{sources_line}\n".encode(),
        capture_output=True,
    )
    return result.returncode


def _is_dpkg_installed(package: str, min_version: str = "") -> bool:
    """Return True if a dpkg package is installed (and meets min version)."""
    result = subprocess.run(
        ["dpkg", "-s", package],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return False
    if not min_version:
        return True

    for line in result.stdout.splitlines():
        if line.startswith("Version:"):
            installed = line.split(":", 1)[1].strip()
            cmp = subprocess.run(
                ["dpkg", "--compare-versions", installed, "ge", min_version],
                capture_output=True,
            )
            if cmp.returncode != 0:
                logger.info(f"{package} {installed} installed but < {min_version}")
                return False
            return True
    return False


def _apt_install(packages: list[str]) -> int:
    """Run apt-get update then install packages. Returns exit code.

    The update is re-run on the ``apt_retry`` schedule because a mirror
    mid-sync fails the fetch outright (see that module).
    """
    apt_get = [*sudo_prefix(), "apt-get", *APT_RETRY_OPTION.split()]
    for attempt in range(1, APT_UPDATE_ATTEMPTS + 1):
        if run_cmd([*apt_get, "update"], check=False).returncode == 0:
            break
        if attempt == APT_UPDATE_ATTEMPTS:
            logger.warning("apt-get update failed -- continuing anyway")
            break
        time.sleep(attempt * APT_UPDATE_BACKOFF_SECONDS)

    install = run_cmd(
        [*apt_get, "install", "-y", "--no-install-recommends", *packages],
        check=False,
    )
    return install.returncode


# ---------------------------------------------------------------------------
# Signed-archive install: AWS CLI v2
# ---------------------------------------------------------------------------
#
# AWS runs no apt repository and noble ships no awscli package, so the signed
# zip is the only route to v2. The URL carries no version to pin a digest to, so
# the detached signature is the integrity anchor, checked against a key shipped
# here that expires 2027-07-01 (versions.yaml `watch:`).

_AWS_CLI_ZIP_URL = "https://awscli.amazonaws.com/awscli-exe-linux-{arch}.zip"
_AWS_CLI_KEY_FILE = _CONFIG_ROOT / "aws-cli-team.asc"
_AWS_CLI_KEY_FPR = "FB5DB77FD5C118B80511ADA8A6310ACC4672475C"
_AWS_CLI_INSTALL_DIR = "/usr/local/aws-cli"
_AWS_CLI_BIN_DIR = "/usr/local/bin"

# platform.machine() spelling -> the spelling AWS's asset names use.
_AWS_CLI_ARCHES = {"x86_64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}


def _verify_detached_signature(payload: bytes, signature: bytes, key: bytes) -> bool:
    """Check ``signature`` covers ``payload`` under the shipped AWS CLI key.

    The key goes into a throwaway GNUPGHOME so the runner's keyring is untouched,
    and its fingerprint is checked first: verifying against whatever was
    imported proves only that the archive is self-consistent.
    """
    found = _key_fingerprints(key)
    if _AWS_CLI_KEY_FPR not in found:
        logger.error(
            f"Shipped AWS CLI key is not {_AWS_CLI_KEY_FPR} "
            f"(got {', '.join(found) or 'nothing readable'})"
        )
        return False

    with tempfile.TemporaryDirectory() as home:
        base = ["gpg", "--homedir", home, "--batch", "--no-tty"]
        if subprocess.run(
            [*base, "--import"], input=key, capture_output=True
        ).returncode:
            logger.error("Could not import the AWS CLI signing key")
            return False

        tmp = Path(home)
        zip_path, sig_path = tmp / "awscliv2.zip", tmp / "awscliv2.sig"
        zip_path.write_bytes(payload)
        sig_path.write_bytes(signature)

        result = subprocess.run(
            [*base, "--verify", str(sig_path), str(zip_path)], capture_output=True
        )

    if result.returncode != 0:
        logger.error("AWS CLI archive failed signature verification - refusing it")
        return False
    logger.info(f"AWS CLI archive signature verified against {_AWS_CLI_KEY_FPR}")
    return True


def _extract_zip(payload: bytes, dest: Path) -> bool:
    """Unpack a zip, keeping the Unix mode each entry recorded.

    ``ZipFile.extract`` drops permissions, leaving AWS's ``install`` script
    non-executable.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            for member in zf.infolist():
                target = zf.extract(member, dest)
                mode = member.external_attr >> 16
                if mode:
                    Path(target).chmod(mode & 0o777)
    except (zipfile.BadZipFile, OSError) as exc:
        logger.error(f"Could not unpack the AWS CLI archive: {exc}")
        return False
    return True


def ensure_aws_cli() -> str | None:
    """Return a path to ``aws``, installing AWS CLI v2 on Linux if it is absent.

    Called at the point of use, not declared as a dep group, because the trigger
    (this run uploads to R2) is a runtime fact no manifest pattern can express.

    Returns None off Linux (dev machines get the ``brew install awscli``
    notice) or on any download, signature or install failure. The caller decides
    whether that is fatal.
    """
    exe = shutil.which("aws")
    if exe:
        return exe

    if platform.system() != "Linux":
        return None

    arch = _AWS_CLI_ARCHES.get(platform.machine())
    if not arch:
        logger.error(f"No AWS CLI v2 build for {platform.machine()}")
        return None

    url = _AWS_CLI_ZIP_URL.format(arch=arch)
    logger.info(f"aws not found - installing AWS CLI v2 from {url}")

    payload = download_artefact("AWS CLI", url)
    signature = download_artefact("AWS CLI signature", f"{url}.sig")
    if payload is None or signature is None:
        return None

    try:
        key = _AWS_CLI_KEY_FILE.read_bytes()
    except OSError as exc:
        logger.error(f"Cannot read the shipped AWS CLI signing key: {exc}")
        return None

    if not _verify_detached_signature(payload, signature, key):
        return None

    with tempfile.TemporaryDirectory() as workdir:
        if not _extract_zip(payload, Path(workdir)):
            return None

        installer = Path(workdir) / "aws" / "install"
        if not installer.is_file():
            logger.error("AWS CLI archive carried no `aws/install`")
            return None

        # --update lets a re-run succeed over an existing install.
        result = subprocess.run(
            [
                *sudo_prefix(),
                str(installer),
                "--update",
                "-i",
                _AWS_CLI_INSTALL_DIR,
                "-b",
                _AWS_CLI_BIN_DIR,
            ],
            capture_output=True,
        )

    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        logger.error(f"AWS CLI install failed (exit {result.returncode}): {detail}")
        return None

    installed = shutil.which("aws")
    if not installed:
        logger.error(f"AWS CLI installed but `aws` is not on PATH ({_AWS_CLI_BIN_DIR})")
        return None

    logger.info(f"AWS CLI v2 installed at {installed}")
    return installed


def install_native_deps(
    language: str,
    project_dir: Path | None = None,
    category: str = "native-deps",
    all_mode: bool = False,
) -> int:
    """Detect and install deps for the given language.

    Args:
        language: Language identifier (rust, typescript, golang, python) for
            `native-deps`, or toolchain family (llvm, gcc) for `toolchains`.
        project_dir: Project root. Defaults to cwd.
        category: Config subdirectory -- `native-deps` or `toolchains`.
        all_mode: If True, bypass the manifest-pattern check and install every
            group unconditionally. Used by runner-image bake (`--all`); CI-time
            invocations on vanilla runners stay conditional (default).

    Returns:
        0 on success, non-zero on failure.

    """
    cwd = project_dir or Path.cwd()

    if platform.system() != "Linux":
        logger.info(f"Skipping {category} on {platform.system()}")
        return 0

    try:
        dep_groups = _load_dep_groups(language, category=category, project_dir=cwd)
    except LLVMVersionError as exc:
        logger.error(str(exc))
        return 1
    if not dep_groups:
        logger.info(f"No {category} groups defined for {language}")
        return 0

    needed: list[DepGroup] = []
    for group in dep_groups:
        if all_mode:
            # --all bypasses the manifest match, but `bake: false` entries stay
            # install-on-demand.
            if not group.bake:
                logger.info(
                    f"[{group.name}] skipped in --all (bake: false, "
                    "install-on-demand only)"
                )
                continue
        elif not _group_matches(group, cwd):
            continue

        if _is_dpkg_installed(group.dpkg_check, group.dpkg_min_version):
            logger.info(f"[{group.name}] already installed ({group.dpkg_check})")
        else:
            logger.info(f"[{group.name}] needs install: {group.apt_packages}")
            needed.append(group)

    if not needed:
        logger.info(f"All {category} satisfied for {language}")
        return 0

    for group in needed:
        for repo in group.apt_repos:
            rc = _add_apt_repo(repo)
            if rc != 0:
                logger.error(f"Failed to add APT repo for [{group.name}]")
                return rc

    all_packages: list[str] = []
    seen: set[str] = set()
    for group in needed:
        for pkg in group.apt_packages:
            if pkg not in seen:
                all_packages.append(pkg)
                seen.add(pkg)

    logger.info(f"Installing {category} packages: {all_packages}")
    rc = _apt_install(all_packages)
    if rc != 0:
        logger.error(f"apt-get install failed (exit {rc})")
        return rc

    # Toolchains have no language tools.
    if category == "native-deps":
        rc = _install_language_tools(language)
        if rc != 0:
            return rc

    logger.info(f"{category} installed for {language}")
    return 0


# ---------------------------------------------------------------------------
# Universal language tooling
# ---------------------------------------------------------------------------
#
# Per-runner tooling keyed by language, installed unconditionally after the apt
# deps, because gating on which stage uses a tool is brittle on opt-in features
# like Rust Tier 2 PGO. Failures are non-fatal: dependent stages handle the
# missing tool themselves (Rust Tier 2 falls back to plain release). List only
# tools the CI pipeline actually runs.


@dataclass(frozen=True)
class LanguageTool:
    """A language toolchain installable into a per-user dir (e.g. cargo-pgo)."""

    name: str
    binary: str
    bin_dir: str  # relative to $HOME
    installer: list[str]
    args: list[str]


_LANGUAGE_TOOLS: dict[str, list[LanguageTool]] = {
    "rust": [
        LanguageTool(
            name="cargo-pgo",
            binary="cargo-pgo",
            bin_dir=".cargo/bin",
            installer=["cargo", "install"],
            args=["cargo-pgo", "--locked"],
        ),
    ],
    "python": [],
    "typescript": [],
    "golang": [],
}


def _install_language_tools(language: str) -> int:
    """Install per-language tooling (cargo / npm / pip / go-install tools).

    Not pattern-gated, because any CI stage may need the tools. A failed install
    logs a warning and continues. No-op off Linux.
    """
    tools = _LANGUAGE_TOOLS.get(language, [])
    if not tools:
        return 0
    if platform.system() != "Linux":
        return 0

    for tool in tools:
        bin_dir = Path.home() / tool.bin_dir
        current_path = os.environ.get("PATH", "")
        if str(bin_dir) not in current_path.split(os.pathsep):
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{current_path}"

        if shutil.which(tool.binary) or (bin_dir / tool.binary).exists():
            logger.info(f"[{tool.name}] already installed")
            continue

        cmd = [*tool.installer, *tool.args]

        # A missing installer raises FileNotFoundError, which `check=False` does
        # not cover (issue #91). Rust gets here before cargo is installed.
        if shutil.which(cmd[0]) is None:
            logger.warning(
                f"[{tool.name}] {cmd[0]} not on PATH -- skipping; dependent CI "
                "stages will handle the missing tool"
            )
            continue

        logger.info(f"Installing {tool.name}: {' '.join(cmd)}")
        result = subprocess.run(cmd, check=False)
        if result.returncode != 0:
            logger.warning(
                f"[{tool.name}] install failed (exit {result.returncode}) -- "
                "dependent CI stages will handle the missing tool"
            )
    return 0


def print_needed(
    language: str,
    project_dir: Path | None = None,
    category: str = "native-deps",
    all_mode: bool = False,
) -> None:
    """Print which dep groups would be triggered (dry-run helper)."""
    cwd = project_dir or Path.cwd()
    try:
        dep_groups = _load_dep_groups(language, category=category, project_dir=cwd)
    except LLVMVersionError as exc:
        logger.error(str(exc))
        return

    for group in dep_groups:
        if all_mode:
            matched = group.bake
        else:
            matched = _group_matches(group, cwd)
        installed = (
            _is_dpkg_installed(group.dpkg_check)
            if platform.system() == "Linux"
            else None
        )
        status = (
            "would-install (already present)"
            if matched and installed
            else "would-install"
            if matched and not installed
            else "skip (no match)"
        )
        print(f"  {group.name}: {status}", file=sys.stderr)
