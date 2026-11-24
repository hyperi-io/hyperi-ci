# Project:   HyperI CI
# File:      src/hyperi_ci/linux_repo.py
# Purpose:   Signed apt, rpm and apk repositories on R2 from a release's packages
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

"""Publish signed apt, rpm and apk repositories to R2 from a release's packages.

The caller packs unsigned .deb, .rpm and .apk files. This signs each rpm and
every index with keys read from OpenBao, keeps the newest ``keep`` versions of
each package and arch, and syncs the result to ``<project>/apt``,
``<project>/rpm/<channel>/<arch>`` and ``<project>/apk/<channel>/<arch>``.
A .deb stays unsigned, because apt verifies InRelease, and an .apk too,
because apk trusts a package whose checksum the signed APKINDEX holds.

A ``SHA256SUMS`` at the top of the packages directory is checked against the
files it names and signed to ``SHA256SUMS.asc`` beside it. The caller publishes
both with its tarballs.
"""

import functools
import gzip
import os
import platform
import re
import shutil
import tarfile
import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from hyperi_ci.common import download_to_file, error, info, run_cmd, success
from hyperi_ci.config import load_org_config
from hyperi_ci.native_deps import apt_install, ensure_aws_cli
from hyperi_ci.native_tools import sha256_file
from hyperi_ci.openbao import OpenBaoError, read_to_file
from hyperi_ci.release.binaries import classic_s3_config
from hyperi_ci.versions import tool_field, tool_sha256, tool_version

_REPO_TOOLS = ["apt-utils", "createrepo-c", "rpm", "gnupg"]
_ALPINE_CDN = "https://dl-cdn.alpinelinux.org/alpine"

# Index files per format, uploaded only after the packages they name.
_METADATA = {
    "apt": ["dists/*"],
    "rpm": ["*/repodata/*"],
    "apk": ["*/APKINDEX.tar.gz"],
}

SUMS = "SHA256SUMS"
_SUMS_LINE = re.compile(r"([0-9a-f]{64}) [ *](.+)")

type Compare = Callable[[str, str], int]


class RepoError(Exception):
    """A step of the repository build or upload failed."""


@dataclass(frozen=True, slots=True)
class Package:
    """One package file and the identity its own metadata gives it.

    Attributes:
        path: Where the file is.
        name: Package name.
        version: Full version, release included for rpm.
        arch: Architecture as the format spells it.

    """

    path: Path
    name: str
    version: str
    arch: str


def _out(
    cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None
) -> str:
    result = run_cmd(cmd, check=False, capture=True, cwd=cwd, env=env)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-400:]
        raise RepoError(f"{cmd[0]} exited {result.returncode}: {detail}")
    return result.stdout or ""


def apt_suite(channel: str) -> str:
    """Return the apt suite ``channel`` publishes to: ``stable`` for ``release``."""
    return "stable" if channel == "release" else channel


def parse_apkindex(text: str) -> list[dict[str, str]]:
    """Return one ``{field: value}`` per blank-line-separated APKINDEX entry."""
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in [*text.splitlines(), ""]:
        if not line:
            if current:
                entries.append(current)
            current = {}
            continue
        key, _, value = line.partition(":")
        current[key] = value
    return entries


def prune(packages: list[Package], keep: int, compare: Compare) -> list[Package]:
    """Return all but the newest ``keep`` packages of each name and arch.

    Args:
        packages: Everything the repository holds.
        keep: Versions kept per name and arch. Below 1 keeps everything.
        compare: The format's version order, negative when the first is older.

    Returns:
        The packages to delete.

    """
    if keep < 1:
        return []
    groups: dict[tuple[str, str], list[Package]] = {}
    for package in packages:
        groups.setdefault((package.name, package.arch), []).append(package)
    order = functools.cmp_to_key(lambda a, b: compare(a.version, b.version))
    stale: list[Package] = []
    for group in groups.values():
        stale.extend(sorted(group, key=order, reverse=True)[keep:])
    return stale


def check_sums(sums: Path) -> int:
    """Check each ``sha256sum`` line of ``sums`` against the file it names.

    Returns:
        The number of files checked.

    Raises:
        RepoError: The listing is empty or malformed, names a path outside its
            directory, or a file is missing or does not match.

    """
    root = sums.parent.resolve()
    checked = 0
    for number, line in enumerate(sums.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        match = _SUMS_LINE.fullmatch(line)
        if not match:
            raise RepoError(f"{SUMS} line {number} is not '<sha256>  <file>'")
        digest, name = match.groups()
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            raise RepoError(f"{SUMS} names {name}, outside its directory")
        if not path.is_file():
            raise RepoError(f"{SUMS} names {name}, which the artifact does not hold")
        if sha256_file(path) != digest:
            raise RepoError(f"{name} does not match its {SUMS} entry")
        checked += 1
    if not checked:
        raise RepoError(f"{SUMS} lists no files")
    return checked


def apk_signature_segment(signature: bytes, key_name: str) -> bytes:
    """Return the gzip'd tar segment apk reads an index signature from.

    It is one ustar member, ``.SIGN.RSA.<key_name>``, with no end-of-archive
    blocks, so apk reads it and the index after it as one archive.
    """
    member = tarfile.TarInfo(f".SIGN.RSA.{key_name}")
    member.size = len(signature)
    member.mode = 0o644
    header = member.tobuf(format=tarfile.USTAR_FORMAT)
    padding = b"\0" * (-len(signature) % tarfile.BLOCKSIZE)
    return gzip.compress(header + signature + padding, mtime=0)


@dataclass(frozen=True, slots=True)
class Signer:
    """A GPG key imported into a private home, signing as ``fingerprint``.

    Attributes:
        home: The GNUPGHOME holding the key.
        fingerprint: The key's fingerprint.

    """

    home: Path
    fingerprint: str

    def _gpg(self, *args: str) -> None:
        _out(
            ["gpg", "--batch", "--yes", "--homedir", str(self.home)]
            + ["-u", self.fingerprint, *args]
        )

    def clearsign(self, source: Path, dest: Path) -> None:
        """Write ``source`` clear-signed to ``dest`` (apt's InRelease)."""
        self._gpg("--clearsign", "-o", str(dest), str(source))

    def detach(self, source: Path, dest: Path) -> None:
        """Write an armoured detached signature of ``source`` to ``dest``."""
        self._gpg("--armor", "--detach-sign", "-o", str(dest), str(source))

    def sign_rpm(self, path: Path) -> None:
        """Sign the rpm at ``path`` in place."""
        gpg = shutil.which("gpg") or "gpg"
        _out(
            ["rpmsign", "--addsign", "--define", f"_gpg_name {self.fingerprint}"]
            + ["--define", f"__gpg {gpg}", str(path)],
            env={"GNUPGHOME": str(self.home)},
        )


def signing_key(listing: str) -> str | None:
    """Return ``<fingerprint>!`` of the first signing key whose secret is present.

    ``listing`` is ``gpg --with-colons --list-secret-keys``. A key exported
    with its primary secret stripped signs with a subkey, and the ``!`` stops
    gpg choosing the primary.
    """
    candidate = False
    for line in listing.splitlines():
        fields = line.split(":")
        if fields[0] in {"sec", "ssb"}:
            capabilities, serial = fields[11], fields[14]
            candidate = "s" in capabilities and not serial.startswith("#")
        elif fields[0] == "fpr" and candidate:
            return f"{fields[9]}!"
    return None


@contextmanager
def gpg_signer(private_key: Path) -> Generator[Signer]:
    """Import ``private_key`` into a throwaway GNUPGHOME and yield its signer.

    Raises:
        RepoError: The import failed or yielded no key that can sign.

    """
    home = Path(tempfile.mkdtemp(prefix="gnupg-"))
    gpg = ["gpg", "--batch", "--homedir", str(home)]
    try:
        _out([*gpg, "--import", str(private_key)])
        listing = _out([*gpg, "--with-colons", "--list-secret-keys"])
        fingerprint = signing_key(listing)
        if not fingerprint:
            raise RepoError("the GPG key file holds no secret key that can sign")
        yield Signer(home, fingerprint)
    finally:
        run_cmd(["gpgconf", "--homedir", str(home), "--kill", "gpg-agent"], check=False)
        shutil.rmtree(home, ignore_errors=True)


def fetch_apk_tool(dest: Path) -> Path:
    """Fetch Alpine's static apk at the pinned version and digest.

    Raises:
        RepoError: Not an x86_64 runner, the download failed, or the digest
            does not match the pin.

    """
    if platform.machine() != "x86_64":
        raise RepoError(f"apk.static is pinned for x86_64, not {platform.machine()}")
    name = "apk-tools-static"
    branch = tool_field(name, "alpine")
    url = f"{_ALPINE_CDN}/{branch}/main/x86_64/{name}-{tool_version(name)}.apk"
    package = dest / f"{name}.apk"
    if not download_to_file(name, url, package):
        raise RepoError(f"could not download {url}")
    if sha256_file(package) != tool_sha256(name, "x86_64"):
        raise RepoError(f"{url} does not match the pinned sha256")
    tool = dest / "apk.static"
    with tarfile.open(package) as tar:
        source = tar.extractfile("sbin/apk.static")
        if source is None:
            raise RepoError(f"{url} holds no sbin/apk.static")
        tool.write_bytes(source.read())
    tool.chmod(0o755)
    return tool


def _deb(path: Path) -> Package:
    show = "--showformat=${Package}\\n${Version}\\n${Architecture}\\n"
    fields = _out(["dpkg-deb", "--show", show, str(path)]).splitlines()
    return Package(path, *fields[:3])


def _rpm(path: Path) -> Package:
    query = "%{NAME}\\n%{VERSION}-%{RELEASE}\\n%{ARCH}\\n"
    fields = _out(["rpm", "-qp", "--nosignature", "--qf", query, str(path)])
    return Package(path, *fields.splitlines()[:3])


def _apk(path: Path, apk: Path, scratch: Path) -> Package:
    index = scratch / "identify.tar.gz"
    _out([str(apk), "index", "--allow-untrusted", "-o", str(index), str(path)])
    with tarfile.open(index) as tar:
        member = tar.extractfile("APKINDEX")
        text = member.read().decode("utf-8") if member else ""
    entries = parse_apkindex(text)
    if not entries:
        raise RepoError(f"apk index read nothing from {path.name}")
    return Package(path, entries[0]["P"], entries[0]["V"], entries[0]["A"])


def _dpkg_compare(a: str, b: str) -> int:
    if a == b:
        return 0
    older = run_cmd(["dpkg", "--compare-versions", a, "lt", b], check=False)
    return -1 if older.returncode == 0 else 1


def _rpm_compare(a: str, b: str) -> int:
    script = f"%{{lua: print(rpm.vercmp('{a}', '{b}'))}}"
    return int(_out(["rpm", "--eval", script]).strip())


def _apk_compare(apk: Path) -> Compare:
    def compare(a: str, b: str) -> int:
        verdict = _out([str(apk), "version", "-t", a, b]).strip()
        return {"<": -1, "=": 0, ">": 1}[verdict]

    return compare


def _place(package: Package, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(package.path, dest)
    return dest


def _drop_stale(packages: list[Package], keep: int, compare: Compare) -> None:
    for package in prune(packages, keep, compare):
        info(f"  dropping {package.path.name}, past the newest {keep}")
        package.path.unlink()


def build_apt(
    root: Path, suite: str, new: list[Path], keep: int, signer: Signer
) -> None:
    """Add ``new`` .debs to ``root``'s pool for ``suite`` and re-sign its index."""
    pool = root / "pool" / suite
    for path in new:
        deb = _deb(path)
        version = deb.version.replace(":", "%3a")
        _place(deb, pool / f"{deb.name}_{version}_{deb.arch}.deb")
    _drop_stale([_deb(p) for p in sorted(pool.glob("*.deb"))], keep, _dpkg_compare)
    arches = sorted({_deb(p).arch for p in pool.glob("*.deb")} - {"all"})
    dists = root / "dists" / suite
    shutil.rmtree(dists, ignore_errors=True)
    for arch in arches:
        listing = dists / "main" / f"binary-{arch}" / "Packages"
        listing.parent.mkdir(parents=True)
        packages = ["apt-ftparchive", "--arch", arch, "packages", f"pool/{suite}"]
        text = _out(packages, cwd=root)
        listing.write_text(text, encoding="utf-8", newline="\n")
        gz = gzip.compress(text.encode("utf-8"), mtime=0)
        listing.with_suffix(".gz").write_bytes(gz)
    fields = {
        "Suite": suite,
        "Codename": suite,
        "Architectures": " ".join(arches),
        "Components": "main",
    }
    options = [f"-oAPT::FTPArchive::Release::{k}={v}" for k, v in fields.items()]
    release = dists / "Release"
    text = _out(["apt-ftparchive", *options, "release", f"dists/{suite}"], cwd=root)
    release.write_text(text, encoding="utf-8", newline="\n")
    signer.clearsign(release, dists / "InRelease")
    signer.detach(release, dists / "Release.gpg")


def build_rpm(
    root: Path, channel: str, new: list[Path], keep: int, signer: Signer
) -> None:
    """Sign and add ``new`` rpms under ``root/<channel>/<arch>``, then re-index."""
    for path in new:
        rpm = _rpm(path)
        name = f"{rpm.name}-{rpm.version}.{rpm.arch}.rpm"
        signer.sign_rpm(_place(rpm, root / channel / rpm.arch / name))
    for arch_dir in sorted(p for p in (root / channel).iterdir() if p.is_dir()):
        current = [_rpm(p) for p in sorted(arch_dir.glob("*.rpm"))]
        _drop_stale(current, keep, _rpm_compare)
        shutil.rmtree(arch_dir / "repodata", ignore_errors=True)
        _out(["createrepo_c", "-q", str(arch_dir)])
        repomd = arch_dir / "repodata" / "repomd.xml"
        signer.detach(repomd, repomd.with_name("repomd.xml.asc"))


@dataclass(frozen=True, slots=True)
class ApkSigning:
    """What signs an APKINDEX.

    Attributes:
        apk: Alpine's static apk.
        key: The RSA private key file.
        key_name: The public key's file name under ``/etc/apk/keys``.
        scratch: A directory for intermediate files.

    """

    apk: Path
    key: Path
    key_name: str
    scratch: Path


def build_apk(
    root: Path, channel: str, new: list[Path], keep: int, signing: ApkSigning
) -> None:
    """Add ``new`` .apks under ``root/<channel>/<arch>``, then re-sign each index."""
    apk, scratch = signing.apk, signing.scratch
    for path in new:
        package = _apk(path, apk, scratch)
        name = f"{package.name}-{package.version}.apk"
        _place(package, root / channel / package.arch / name)
    for arch_dir in sorted(p for p in (root / channel).iterdir() if p.is_dir()):
        current = [_apk(p, apk, scratch) for p in sorted(arch_dir.glob("*.apk"))]
        _drop_stale(current, keep, _apk_compare(apk))
        unsigned = scratch / "APKINDEX.unsigned.tar.gz"
        files = sorted(p.name for p in arch_dir.glob("*.apk"))
        index = [str(apk), "index", "--allow-untrusted", "-o", str(unsigned)]
        _out([*index, *files], cwd=arch_dir)
        signature = scratch / "APKINDEX.sig"
        _out(
            ["openssl", "dgst", "-sha1", "-sign", str(signing.key)]
            + ["-out", str(signature), str(unsigned)]
        )
        segment = apk_signature_segment(signature.read_bytes(), signing.key_name)
        (arch_dir / "APKINDEX.tar.gz").write_bytes(segment + unsigned.read_bytes())


@dataclass(frozen=True, slots=True)
class _R2:
    env: dict[str, str]
    endpoint: str
    prefix: str

    def sync(self, source: str, dest: str, *extra: str) -> None:
        _out(
            ["aws", "s3", "sync", source, dest, "--endpoint-url", self.endpoint]
            + ["--only-show-errors", *extra],
            env=self.env,
        )

    def download(self, fmt: str, local: Path) -> None:
        self.sync(f"{self.prefix}/{fmt}/", str(local))

    def upload(self, fmt: str, local: Path) -> None:
        remote = f"{self.prefix}/{fmt}/"
        skip = [arg for pattern in _METADATA[fmt] for arg in ("--exclude", pattern)]
        self.sync(str(local), remote, *skip)
        self.sync(str(local), remote)
        self.sync(str(local), remote, "--delete")


@dataclass(frozen=True, slots=True)
class Target:
    """Where and how a release's packages publish.

    Attributes:
        project: R2 prefix, and the name in the public URLs.
        channel: Release channel. ``release`` is apt's ``stable`` suite.
        keep: Versions kept per package name and arch, below 1 for all.
        role: OpenBao jwt role this job logs in as.
        key_path: kv-v2 path holding the ``gpg`` and ``apk`` key secrets.

    """

    project: str
    channel: str
    keep: int
    role: str
    key_path: str


def publish(packages_dir: Path, target: Target) -> int:
    """Build, sign and upload every repository ``packages_dir`` has packages for.

    Args:
        packages_dir: The caller's unsigned packages, searched recursively.
        target: Where and how they publish.

    Returns:
        Exit code, 0 on success.

    """
    found = {
        ext: sorted(packages_dir.rglob(f"*.{ext}")) for ext in ("deb", "rpm", "apk")
    }
    if not any(found.values()):
        error(f"no .deb, .rpm or .apk under {packages_dir}")
        return 1
    sums: Path | None = packages_dir / SUMS
    if sums.is_file():
        try:
            info(f"{SUMS}: {check_sums(sums)} file(s) match")
        except RepoError as exc:
            error(str(exc))
            return 1
    else:
        sums = None
    access_key = os.environ.get("R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")
    if not access_key or not secret_key:
        error("R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY are needed to publish")
        return 1
    if apt_install(_REPO_TOOLS) != 0 or not ensure_aws_cli():
        error(f"could not install {', '.join(_REPO_TOOLS)} and the aws CLI")
        return 1
    org = load_org_config()
    try:
        with (
            tempfile.TemporaryDirectory(prefix="linux-repo-") as tmp,
            classic_s3_config() as config,
        ):
            r2 = _R2(
                env={
                    "AWS_ACCESS_KEY_ID": access_key,
                    "AWS_SECRET_ACCESS_KEY": secret_key,
                    "AWS_DEFAULT_REGION": "auto",
                    "AWS_CONFIG_FILE": config,
                },
                endpoint=org.r2_endpoint,
                prefix=f"s3://{org.r2_bucket}/{target.project}",
            )
            _publish(Path(tmp), r2, found, target, sums)
    except (RepoError, OpenBaoError) as exc:
        error(str(exc))
        return 1
    for fmt, ext in (("apt", "deb"), ("rpm", "rpm"), ("apk", "apk")):
        if found[ext]:
            success(f"Published {fmt}: {org.r2_public_url}/{target.project}/{fmt}")
    return 0


def _read_key(target: Target, secret: str, field: str, out: Path) -> Path:
    path = f"{target.key_path}/{secret}"
    org = load_org_config()
    return read_to_file(org, role=target.role, path=path, field=field, out=out)


def _publish(
    work: Path,
    r2: _R2,
    found: dict[str, list[Path]],
    target: Target,
    sums: Path | None,
) -> None:
    if found["deb"] or found["rpm"] or sums:
        gpg_key = _read_key(target, "gpg", "private", work / "gpg.key")
        with gpg_signer(gpg_key) as signer:
            if sums:
                signer.detach(sums, sums.with_name(f"{SUMS}.asc"))
                info(f"{SUMS}: signed to {SUMS}.asc")
            if found["deb"]:
                info(f"apt: {len(found['deb'])} new package(s)")
                r2.download("apt", work / "apt")
                suite = apt_suite(target.channel)
                build_apt(work / "apt", suite, found["deb"], target.keep, signer)
                r2.upload("apt", work / "apt")
            if found["rpm"]:
                info(f"rpm: {len(found['rpm'])} new package(s)")
                r2.download("rpm", work / "rpm")
                build_rpm(
                    work / "rpm", target.channel, found["rpm"], target.keep, signer
                )
                r2.upload("rpm", work / "rpm")
    if found["apk"]:
        info(f"apk: {len(found['apk'])} new package(s)")
        scratch = work / "scratch"
        scratch.mkdir()
        signing = ApkSigning(
            apk=fetch_apk_tool(scratch),
            key=_read_key(target, "apk", "private", work / "apk.key"),
            key_name=_read_key(target, "apk", "key_name", work / "apk.name")
            .read_text(encoding="utf-8")
            .strip(),
            scratch=scratch,
        )
        r2.download("apk", work / "apk")
        build_apk(work / "apk", target.channel, found["apk"], target.keep, signing)
        r2.upload("apk", work / "apk")
