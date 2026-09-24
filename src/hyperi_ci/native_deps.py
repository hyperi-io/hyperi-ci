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
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from scalo import logger

from hyperi_ci.common import (
    URL_ERRORS,
    curl_read,
    download_artefact,
    info,
    url_read,
    warn,
)
from hyperi_ci.versions import runtime_version

# Codenames to try as fallbacks when a given APT repo doesn't ship
# packages for the current OS codename. Ordered by preference (newest
# Ubuntu LTS first, then older LTS, then Debian stable). Resolute
# (Ubuntu 26.04 LTS, April 2026) is the current latest; trixie (Debian
# 13) is supported alongside for ESH projects.
_FALLBACK_CODENAMES = ["resolute", "noble", "jammy", "focal", "trixie"]

_CONFIG_ROOT = Path(__file__).resolve().parent / "config"
_NATIVE_DEPS_DIR = _CONFIG_ROOT / "native-deps"
_TOOLCHAINS_DIR = _CONFIG_ROOT / "toolchains"


def _sudo_prefix() -> list[str]:
    """Return ``["sudo"]`` when non-root, ``[]`` when already root.

    Runner image bake (Dockerfile ``RUN``) executes as root where sudo
    isn't configured, producing 'root is not in the sudoers file'. CI-time
    invocations on vanilla runners still prepend sudo as before.
    """
    if platform.system() != "Linux":
        return []
    return [] if os.geteuid() == 0 else ["sudo"]


# Supported categories map to config subdirectories. Both share the same
# YAML schema (patterns, manifest_files, dpkg_check, apt_repos, apt_packages)
# plus the optional `versions:` list for multi-version expansion.
#
# Semantic difference:
#   native-deps: conditional by default (install only if manifest matches)
#   toolchains:  conditional in --auto mode; --all bypasses pattern check.
#                Covers multi-version apt families (LLVM 19-22, GCC 13/14).
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

    `bake` controls behaviour in `--all` mode (runner image bake):
      True  (default): install unconditionally -- entry ends up pre-baked
                       into the runner image for every matching category.
      False:           SKIP in --all mode. The entry still installs
                       conditionally at CI job time when manifest patterns
                       match. Use for non-coinstallable toolsets (e.g.
                       `libc++-N-dev` and friends declare Conflicts:x.y,
                       so only one version may be present at a time --
                       baking a default would lock out jobs needing a
                       different version).
    """

    name: str
    patterns: list[str]
    manifest_files: list[str]
    dpkg_check: str
    apt_packages: list[str] = field(default_factory=list)
    apt_repos: list[AptRepo] = field(default_factory=list)
    dpkg_min_version: str = ""
    bake: bool = True


def _expand_template_vars(text: str) -> str:
    """Expand ${VAR} placeholders in YAML configs.

    Supports a small set of hyperi-ci-controlled variables -- keeps the
    YAML readable while letting ops override per environment without
    editing code.

    Recognised variables:
      HYPERCI_LLVM_VERSION  -- LLVM/BOLT major (default: versions.yaml `llvm`)
      OS_CODENAME           -- current OS codename from lsb_release -cs
                              (e.g. noble, trixie, resolute). Lets a single
                              YAML reference distro-specific apt.llvm.org
                              subpaths (https://apt.llvm.org/${OS_CODENAME}/).

    Unknown ${VAR} placeholders pass through unchanged so apt-cache
    surfaces a clear "package not found" error instead of a silent
    mis-resolve.
    """
    llvm_version = os.environ.get("HYPERCI_LLVM_VERSION") or runtime_version("llvm")
    os_codename = os.environ.get("OS_CODENAME") or _get_os_codename() or "noble"
    return text.replace("${HYPERCI_LLVM_VERSION}", llvm_version).replace(
        "${OS_CODENAME}", os_codename
    )


def _substitute_version(text: str, version: str) -> str:
    """Substitute the {V} placeholder with a concrete version."""
    return text.replace("{V}", version)


def _dep_group_from_entry(entry: dict, version: str | None = None) -> DepGroup:
    """Materialise one DepGroup from a YAML entry, optionally substituting {V}.

    When `version` is None (the common native-deps path) the entry is used
    verbatim. When `version` is a concrete string (the toolchains path)
    `{V}` is substituted everywhere it appears: `dpkg_check`, every
    `apt_repos[*].codename`, every `apt_packages[*]`, and the `name`
    (so log lines distinguish versions).
    """

    def sub(text: str) -> str:
        return _substitute_version(text, version) if version is not None else text

    name = sub(entry["name"])
    if version is not None:
        # Disambiguate the group name so log lines are readable
        name = f"{entry['name']} v{version}"

    return DepGroup(
        name=name,
        # patterns and manifest_files stay shared across version expansions
        patterns=entry.get("patterns", []),
        manifest_files=entry.get("manifest_files", []),
        dpkg_check=sub(entry["dpkg_check"]),
        apt_packages=[sub(p) for p in entry.get("apt_packages", [])],
        bake=entry.get("bake", True),
        apt_repos=[
            AptRepo(
                key_url=r["key_url"],
                keyring=r["keyring"],
                url=r["url"],
                codename=sub(r.get("codename", "auto")),
                components=r.get("components", "main"),
                key_fingerprint=r.get("key_fingerprint", ""),
            )
            for r in entry.get("apt_repos", [])
        ],
        dpkg_min_version=entry.get("dpkg_min_version", ""),
    )


def _load_dep_groups(language: str, category: str = "native-deps") -> list[DepGroup]:
    """Load dep group definitions from bundled config.

    Entries with a `versions:` list expand into one DepGroup per version,
    with `{V}` substituted in `dpkg_check`, `apt_repos[*].codename`, and
    every `apt_packages[*]`. Entries without `versions:` are loaded as-is
    (backward-compatible with existing native-deps YAMLs).
    """
    config_dir = _CATEGORY_DIRS.get(category)
    if config_dir is None:
        logger.warning(f"Unknown config category: {category}")
        return []

    config_file = config_dir / f"{language}.yaml"
    if not config_file.exists():
        logger.warning(f"No {category} config for: {language}")
        return []

    raw = yaml.safe_load(_expand_template_vars(config_file.read_text(encoding="utf-8")))
    if not raw:
        return []

    groups: list[DepGroup] = []
    for entry in raw:
        versions = entry.get("versions")
        if versions is None:
            # No versions key -- load as-is (backward-compatible native-deps path)
            groups.append(_dep_group_from_entry(entry))
        elif not versions:
            # Empty list is almost certainly a config bug -- {V} would leak
            # into the final install command and fail at apt-cache time
            # with a confusing "package not found" message. Warn loudly.
            logger.warning(
                f"entry {entry.get('name', '<unnamed>')!r} in "
                f"{config_file} has empty `versions:` — skipping"
            )
        else:
            # Multi-version expansion: one DepGroup per version
            for v in versions:
                groups.append(_dep_group_from_entry(entry, version=str(v)))
    return groups


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


def _get_os_codename() -> str:
    """Get the current OS codename via lsb_release, or "" if unavailable.

    macOS has no `lsb_release` binary -- `FileNotFoundError` propagates up
    from Popen. Swallow it so callers get an empty string (same contract
    as a non-zero exit on Linux); the `_expand_template_vars` fallback
    then defaults `${OS_CODENAME}` to "noble".
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
        url_read(urllib.request.Request(url, method="HEAD"), timeout=10)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise
    return True


def _resolve_codename(repo: AptRepo) -> str:
    """Resolve the codename to use for an APT repo.

    If codename is explicit, uses it directly. If "auto", tries the current
    OS codename first, then falls back through LTS codenames in order.

    Only a 404 moves on to the next candidate. A probe that gets no answer
    keeps the candidate it was checking and warns, because skipping it would
    install from an older codename's repo on the strength of a network fault.
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

    Self-hosted runners may pre-configure apt.llvm.org (or other upstream
    repos) under a different filename than ours. To avoid duplicate
    entries, scan all files in /etc/apt/sources.list.d/ and the main
    /etc/apt/sources.list for a line that matches our url + codename.

    Returns the path of the first matching file, or None if not found.
    Match is substring-based on `url + " " + codename` so variations in
    `[signed-by=...]` options, arch flags, or components don't cause
    false negatives.
    """
    # Normalise scheme -- pre-provisioned runners often use `http://` for
    # apt.llvm.org (their Dockerfile does) while we write `https://`.
    # Match on the scheme-less path so either form is detected.
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
        # Match on scheme-less URL path + codename. Covers both the
        # one-line `deb` format and deb822-style `.sources` files.
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

    HTTPS proves who served the key, not which key it is -- without this a
    swapped or hijacked upstream key installs silently and then signs every
    package apt pulls from that repo.
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

    A declared ``key_fingerprint`` is checked before the key reaches the
    keyring, so a swapped upstream key fails the install rather than signing
    everything apt pulls afterwards.

    Idempotent on three levels:
      1. GPG keyring: skips download if keyring file already present.
      2. Exact sources.list match: skips write if our file already has
         the exact same line. Other unrelated lines in the file are
         preserved (append, don't overwrite).
      3. Cross-file duplicate detection: skips write if ANY other apt
         source file already references the same url + codename -- this
         handles self-hosted runners where admins have pre-configured
         upstream repos under a different filename.

    Multi-version note: many entries can share a keyring (e.g. LLVM 19/20/
    21/22 all use `/usr/share/keyrings/llvm.gpg`). The sources filename is
    derived from the keyring stem, so multiple AptRepo writes collide on
    one file. We APPEND -- multiple `deb` lines in the same .list pointing
    at the same keyring is valid apt syntax.
    """
    keyring_path = Path(repo.keyring)
    if keyring_path.exists():
        logger.info(f"APT keyring already exists: {repo.keyring}")
    else:
        logger.info(f"Adding APT key from {repo.key_url}")
        # Download key and dearmor into keyring
        rc, key = curl_read(repo.key_url)
        if rc != 0:
            logger.error(f"Failed to download APT key from {repo.key_url}")
            return rc

        if repo.key_fingerprint and not _verify_apt_key(key, repo.key_fingerprint):
            logger.error(f"Refusing to install the APT key from {repo.key_url}")
            return 1

        dearmor = subprocess.run(
            [
                *_sudo_prefix(),
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

    # Cross-file check: if another sources file already references this
    # repo (pre-configured by runner admin), skip to avoid duplicates.
    existing = _repo_already_configured(repo.url, codename)
    if existing is not None:
        logger.info(
            f"APT source for {repo.url} {codename} already present in {existing}"
        )
        return 0

    # Derive a stable filename from the keyring name
    sources_name = keyring_path.stem + ".list"
    sources_path = Path("/etc/apt/sources.list.d") / sources_name

    # Skip if the exact `deb` line is already present in the file. Substring
    # check (not equality) -- the file may contain other entries for different
    # versions of the same toolchain (e.g. llvm.list holds v19/v20/v21/v22).
    if sources_path.exists() and sources_line in sources_path.read_text(
        encoding="utf-8"
    ):
        logger.info(f"APT source already configured: {sources_path}")
        return 0

    logger.info(f"Adding APT source: {sources_line}")
    # Append (don't overwrite) -- `tee -a` creates the file if missing.
    # Prepend a newline so subsequent lines don't get concatenated.
    prefix = "" if not sources_path.exists() else "\n"
    result = subprocess.run(
        [*_sudo_prefix(), "tee", "-a", str(sources_path)],
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
    """Run apt-get update then install packages. Returns exit code."""
    sudo = _sudo_prefix()
    update = subprocess.run([*sudo, "apt-get", "update"])
    if update.returncode != 0:
        logger.warning("apt-get update failed — continuing anyway")

    install = subprocess.run(
        [
            *sudo,
            "apt-get",
            "install",
            "-y",
            "--no-install-recommends",
            *packages,
        ]
    )
    return install.returncode


# ---------------------------------------------------------------------------
# Signed-archive install: AWS CLI v2
# ---------------------------------------------------------------------------
#
# AWS runs no apt repository and Ubuntu noble ships no awscli package, so the
# signed zip bundle is the only route to v2 on a runner.
#
# The URL carries no version, so there is no digest to pin: the detached
# signature is the integrity anchor, checked against a key shipped in this
# package that expires 2027-07-01 (versions.yaml `watch:`).

_AWS_CLI_ZIP_URL = "https://awscli.amazonaws.com/awscli-exe-linux-{arch}.zip"
_AWS_CLI_KEY_FILE = _CONFIG_ROOT / "aws-cli-team.asc"
_AWS_CLI_KEY_FPR = "FB5DB77FD5C118B80511ADA8A6310ACC4672475C"
_AWS_CLI_INSTALL_DIR = "/usr/local/aws-cli"
_AWS_CLI_BIN_DIR = "/usr/local/bin"

# platform.machine() spelling -> the spelling AWS's asset names use.
_AWS_CLI_ARCHES = {"x86_64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}


def _verify_detached_signature(payload: bytes, signature: bytes, key: bytes) -> bool:
    """Check ``signature`` covers ``payload`` under the shipped AWS CLI key.

    The key is imported into a throwaway GNUPGHOME so verification never reads
    or writes the runner's own keyring, and its fingerprint is checked before
    it is trusted - importing a key and then verifying against whatever was
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

    ``ZipFile.extract`` drops permissions, which would leave AWS's own
    ``install`` script and the ``aws`` binary non-executable.
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

    Called at the point of use rather than declared as a dep group: the trigger
    is a runtime fact (this run is uploading to R2), not a manifest pattern, so
    no YAML entry could express it. Same shape as
    ``pgo._ensure_llvm_bolt_available``.

    Returns None when it cannot install - off Linux (dev machines get the
    ``brew install awscli`` notice instead) or on any download, signature or
    install failure. The caller decides whether that is fatal.
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

        # --update so a re-run over an existing install succeeds instead of
        # failing on the directory it wrote last time.
        result = subprocess.run(
            [
                *_sudo_prefix(),
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

    dep_groups = _load_dep_groups(language, category=category)
    if not dep_groups:
        logger.info(f"No {category} groups defined for {language}")
        return 0

    needed: list[DepGroup] = []
    for group in dep_groups:
        if all_mode:
            # --all mode bypasses manifest match, but entries marked
            # `bake: false` are ALWAYS install-on-demand -- runner image
            # bake skips them. Non-coinstallable toolsets (one version
            # at a time) use this; baking a default would lock out jobs
            # that need a different version.
            if not group.bake:
                logger.info(
                    f"[{group.name}] skipped in --all (bake: false, "
                    "install-on-demand only)"
                )
                continue
        else:
            # Conditional mode: only install if a manifest pattern matches
            content = _read_manifests(cwd, group.manifest_files)
            if not content:
                continue
            if not _patterns_match(content, group.patterns):
                continue

        if _is_dpkg_installed(group.dpkg_check, group.dpkg_min_version):
            logger.info(f"[{group.name}] already installed ({group.dpkg_check})")
        else:
            logger.info(f"[{group.name}] needs install: {group.apt_packages}")
            needed.append(group)

    if not needed:
        logger.info(f"All {category} satisfied for {language}")
        return 0

    # Add any custom APT repos before installing
    for group in needed:
        for repo in group.apt_repos:
            rc = _add_apt_repo(repo)
            if rc != 0:
                logger.error(f"Failed to add APT repo for [{group.name}]")
                return rc

    # Collect all packages across needed groups and install in one apt call.
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

    # Universal per-language tooling (cargo / npm / pip / go-install) only
    # applies to the native-deps category -- toolchains have no language tools.
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
# Install-once-per-runner tooling keyed by language. Runs unconditionally
# after apt deps. Philosophy: if a CI stage (quality, test, build, release)
# might need a tool, install it at setup time instead of just-in-time. The
# "which stage uses it?" gating would be brittle + slow on opt-in features
# like Rust Tier 2 PGO.
#
# Each entry:
#   name:         Human label for logs
#   binary:       Executable to probe for "already installed?"
#   bin_dir:      Directory where the installer drops binaries (ensured on PATH)
#   installer:    Shell tokens that install the tool (prepended to cargo/npm/etc)
#   args:         Arguments passed after the installer tokens
#
# Non-fatal: failures log a warning and the function continues. Downstream
# stages that depend on the tool handle the missing-tool case themselves
# (Rust Tier 2 falls back to plain release, etc.).


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
    # Populate as concrete needs appear. The abstraction is in place;
    # adding a Python tool (e.g. 'pip-audit' if a stage requires it) is
    # one entry below. Don't over-populate preemptively -- only list tools
    # the CI pipeline actually runs.
    "python": [],
    "typescript": [],
    "golang": [],
}


def _install_language_tools(language: str) -> int:
    """Install per-language tooling (cargo / npm / pip / go-install tools).

    Runs universally (not pattern-gated) -- tools listed here may be
    required by any CI stage (quality, test, build, release). Gating by
    stage would be fragile on opt-in features like Rust Tier 2 PGO.

    Non-fatal: install failure logs a warning and continues. Downstream
    stages that depend on the tool handle missing-tool fallback.

    No-op on non-Linux -- CI stages that need these tools only run on
    Linux runners in our current pipelines.
    """
    tools = _LANGUAGE_TOOLS.get(language, [])
    if not tools:
        return 0
    if platform.system() != "Linux":
        return 0

    for tool in tools:
        # Ensure the tool's install dir is on PATH before probe + install
        bin_dir = Path.home() / tool.bin_dir
        current_path = os.environ.get("PATH", "")
        if str(bin_dir) not in current_path.split(os.pathsep):
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{current_path}"

        if shutil.which(tool.binary) or (bin_dir / tool.binary).exists():
            logger.info(f"[{tool.name}] already installed")
            continue

        cmd = [*tool.installer, *tool.args]

        # A missing installer raises FileNotFoundError before the process
        # starts, which `check=False` does not cover (issue #91). Rust reaches
        # here before `Install Rust toolchain` runs, so cargo may not exist.
        if shutil.which(cmd[0]) is None:
            logger.warning(
                f"[{tool.name}] {cmd[0]} not on PATH — skipping; dependent CI "
                "stages will handle the missing tool"
            )
            continue

        logger.info(f"Installing {tool.name}: {' '.join(cmd)}")
        result = subprocess.run(cmd, check=False)
        if result.returncode != 0:
            logger.warning(
                f"[{tool.name}] install failed (exit {result.returncode}) — "
                "dependent CI stages will handle the missing tool"
            )
            # Non-fatal: downstream has graceful fallback
    return 0


def print_needed(
    language: str,
    project_dir: Path | None = None,
    category: str = "native-deps",
    all_mode: bool = False,
) -> None:
    """Print which dep groups would be triggered (dry-run helper)."""
    cwd = project_dir or Path.cwd()
    dep_groups = _load_dep_groups(language, category=category)

    for group in dep_groups:
        if all_mode:
            # --all skips bake: false entries (install-on-demand only)
            matched = group.bake
        else:
            content = _read_manifests(cwd, group.manifest_files)
            matched = bool(content) and _patterns_match(content, group.patterns)
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
