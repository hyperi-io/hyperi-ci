# Project:   HyperI CI
# File:      src/hyperi_ci/static_libs.py
# Purpose:   Build librdkafka, OpenSSL, zstd and zlib into one static-only prefix
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Build librdkafka, OpenSSL, zstd and zlib into one static-only prefix.

A Rust app links the prefix through rdkafka-sys's ``dynamic-linking`` feature.
With ``PKG_CONFIG_PATH`` on ``<prefix>/lib/pkgconfig`` and ``RDKAFKA_STATIC=1``
the pkg-config crate emits a static link for every library here, so the binary
needs no librdkafka, libssl, libsasl2 or libcurl at runtime. rdkafka-sys's own
build bundles an older librdkafka and its ``static-linking`` feature reaches no
code, so neither is used.

The sources and their digests are the ``static_libs:`` pins in versions.yaml.
librdkafka is built with SSL, zstd and zlib and without GSSAPI or curl. OpenSSL
reads its CA bundle from ``/etc/ssl``, where Debian and Ubuntu runtime images
keep it. Every ``.pc`` file names its prefix relative to itself, so the archive
works wherever it is unpacked.
"""

import hashlib
import json
import os
import platform
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path

from hyperi_ci.common import curl_fetch, error, info, run_cmd
from hyperi_ci.gh import gh_run
from hyperi_ci.upgrade import CACHE_DIR
from hyperi_ci.versions import static_libs, static_libs_release

_TRIPLES = {
    "x86_64": "x86_64-unknown-linux-gnu",
    "aarch64": "aarch64-unknown-linux-gnu",
}
_ARCHIVES = ("libssl.a", "libcrypto.a", "libz.a", "libzstd.a", "librdkafka.a")
# A link the static binary must not carry at runtime.
_REFUSED_LINKS = ("sasl2", "curl")
# Shared objects a binary linked against the prefix must not still need.
STATIC_ONLY = ("librdkafka", "libssl", "libcrypto", "libsasl2", "libcurl")
MANIFEST = "hyperi-static-libs.json"
RELEASE_URL = "https://github.com/hyperi-io/hyperi-ci/releases/download/{tag}/{asset}"


@dataclass(frozen=True, slots=True)
class _Step:
    """One build command, run in ``subdir`` of the unpacked source."""

    argv: tuple[str, ...]
    subdir: str = ""


def _recipe(name: str, prefix: Path, jobs: int) -> list[_Step]:
    """Return the configure, make and install steps for ``name`` into ``prefix``."""
    make = ("make", f"-j{jobs}")
    pkgconfig = prefix / "lib" / "pkgconfig"
    recipes = {
        "openssl": [
            _Step(
                (
                    "./Configure",
                    f"--prefix={prefix}",
                    "--openssldir=/etc/ssl",
                    "--libdir=lib",
                    "no-shared",
                    "no-tests",
                    "no-docs",
                )
            ),
            _Step(make),
            _Step(("make", "install_sw")),
        ],
        "zlib": [
            _Step(("./configure", f"--prefix={prefix}", "--static")),
            _Step(make),
            _Step(("make", "install")),
        ],
        "zstd": [
            _Step((*make, "libzstd.a", "libzstd.pc", f"PREFIX={prefix}"), "lib"),
            _Step(
                (
                    "make",
                    "install-static",
                    "install-pc",
                    "install-includes",
                    f"PREFIX={prefix}",
                ),
                "lib",
            ),
        ],
        "librdkafka": [
            _Step(
                (
                    "./configure",
                    f"--prefix={prefix}",
                    f"--pkg-config-path={pkgconfig}",
                    "--no-download",
                    "--enable-static",
                    "--enable-ssl",
                    "--enable-zstd",
                    "--enable-zlib",
                    "--disable-gssapi",
                    "--disable-curl",
                    "--disable-lz4-ext",
                )
            ),
            _Step((*make, "libs")),
            _Step(("make", "install-subdirs")),
        ],
    }
    if name not in recipes:
        raise KeyError(f"no build recipe for static_libs.{name}")
    return recipes[name]


def target_triple() -> str | None:
    """Return this host's Rust target triple, or None off Linux x86_64 and aarch64."""
    if platform.system() != "Linux":
        return None
    return _TRIPLES.get(platform.machine().lower())


def _fetch_source(name: str, pin: dict[str, str], work: Path) -> Path | None:
    """Download and unpack ``name``'s pinned source, returning its top directory."""
    url = pin["url"].format(version=pin["version"])
    archive = work / f"{name}.tar.gz"
    result = curl_fetch(url, archive)
    if result.returncode != 0:
        error(f"  {name}: could not download {url}")
        return None
    with archive.open("rb") as fh:
        got = hashlib.file_digest(fh, "sha256").hexdigest()
    if got != pin["sha256"].lower():
        error(
            f"  {name}: SHA256 mismatch for {url} (expected {pin['sha256']}, got {got})"
        )
        return None
    dest = work / name
    try:
        # The "data" filter refuses absolute paths, ".." and links out.
        # nosemgrep: trailofbits.python.tarfile-extractall-traversal.tarfile-extractall-traversal
        with tarfile.open(archive) as tar:
            tops = {Path(m.name).parts[0] for m in tar.getmembers() if m.name}
            tar.extractall(dest, filter="data")
    except (OSError, tarfile.TarError) as exc:
        error(f"  {name}: cannot unpack {archive}: {exc}")
        return None
    if len(tops) != 1:
        error(f"  {name}: expected one top directory in {url}, found {sorted(tops)}")
        return None
    return dest / tops.pop()


def _build_env(prefix: Path) -> dict[str, str]:
    """Return the environment each step runs in, finding earlier libraries first."""
    env = dict(os.environ)
    env["PKG_CONFIG_PATH"] = str(prefix / "lib" / "pkgconfig")
    env["CFLAGS"] = f"{env.get('CFLAGS', '')} -I{prefix / 'include'} -fPIC".strip()
    env["LDFLAGS"] = f"{env.get('LDFLAGS', '')} -L{prefix / 'lib'}".strip()
    return env


def _make_relocatable(prefix: Path) -> None:
    """Point every ``.pc`` file at its own location, so the prefix can move."""
    for pc in (prefix / "lib" / "pkgconfig").glob("*.pc"):
        lines = pc.read_text(encoding="utf-8").replace(str(prefix), "${prefix}")
        fixed = [
            "prefix=${pcfiledir}/../.." if line.startswith("prefix=") else line
            for line in lines.splitlines()
        ]
        pc.write_text("\n".join(fixed) + "\n", encoding="utf-8", newline="\n")


def _prune(prefix: Path) -> None:
    """Drop everything but the static archives, headers and ``.pc`` files."""
    for shared in (prefix / "lib").rglob("*.so*"):
        shared.unlink()
    for unwanted in ("bin", "share", "ssl"):
        shutil.rmtree(prefix / unwanted, ignore_errors=True)


def _check(prefix: Path) -> list[str]:
    """Return what is wrong with a built prefix, empty when it is complete."""
    problems = [
        f"missing lib/{name}"
        for name in _ARCHIVES
        if not (prefix / "lib" / name).is_file()
    ]
    rdkafka_pc = prefix / "lib" / "pkgconfig" / "rdkafka.pc"
    if not rdkafka_pc.is_file():
        return [*problems, "missing lib/pkgconfig/rdkafka.pc"]
    text = rdkafka_pc.read_text(encoding="utf-8")
    problems += [f"rdkafka.pc links {lib}" for lib in _REFUSED_LINKS if lib in text]
    return problems


def build(prefix: Path, work: Path, *, jobs: int | None = None) -> int:
    """Build every pinned library into ``prefix``, in versions.yaml order.

    Args:
        prefix: Install directory. It must not exist yet.
        work: Scratch directory for the downloads and source trees.
        jobs: Parallel make jobs, the CPU count when None.

    Returns:
        0 when ``prefix`` holds every archive and its manifest, else 1.

    """
    if prefix.exists():
        error(f"{prefix} already exists, so the build would mix with it")
        return 1
    triple = target_triple()
    if triple is None:
        error(f"no static-libs build for {platform.system()} {platform.machine()}")
        return 1
    pins = static_libs()
    parallel = jobs or os.cpu_count() or 1
    work.mkdir(parents=True, exist_ok=True)
    env = _build_env(prefix)
    for name, pin in pins.items():
        info(f"static-libs: {name} {pin['version']}")
        source = _fetch_source(name, pin, work)
        if source is None:
            return 1
        for step in _recipe(name, prefix, parallel):
            result = run_cmd(
                list(step.argv), check=False, cwd=source / step.subdir, env=env
            )
            if result.returncode != 0:
                error(f"  {name}: `{' '.join(step.argv)}` exited {result.returncode}")
                return 1
    _prune(prefix)
    _make_relocatable(prefix)
    if problems := _check(prefix):
        error(f"static-libs: incomplete prefix {prefix}: {', '.join(problems)}")
        return 1
    manifest = {"target": triple, "libs": pins}
    (prefix / MANIFEST).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return 0


def fetch(triple: str, cache_dir: Path = CACHE_DIR) -> Path | None:
    """Download and unpack the pinned release's prefix for ``triple``.

    The archive is checked against its versions.yaml digest before anything is
    unpacked, and is unpacked afresh each time, so nothing cached is trusted.

    Returns:
        The prefix, or None after logging why there is none.

    """
    tag, digests = static_libs_release()
    if triple not in digests:
        error(f"static-libs: {tag} pins no archive for {triple}")
        return None
    asset = f"hyperi-static-libs-{triple}.tar.gz"
    pin = {
        "version": tag,
        "url": RELEASE_URL.format(tag=tag, asset=asset),
        "sha256": digests[triple],
    }
    work = cache_dir / "static-libs" / tag / triple
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    prefix = _fetch_source("download", pin, work)
    if prefix is None:
        return None
    if problems := _check(prefix):
        error(f"static-libs: {asset} is incomplete: {', '.join(problems)}")
        return None
    info(f"static-libs: {tag} for {triple} at {prefix}")
    return prefix


def link_env(prefix: Path) -> dict[str, str]:
    """Return the variables that make a Rust build link ``prefix`` statically.

    The prefix goes first on any ``PKG_CONFIG_PATH`` already set, so it wins
    over a system librdkafka and the rest still resolve.
    """
    paths = [str(prefix / "lib" / "pkgconfig"), os.environ.get("PKG_CONFIG_PATH", "")]
    return {
        "PKG_CONFIG_PATH": os.pathsep.join(p for p in paths if p),
        "RDKAFKA_STATIC": "1",
        "OPENSSL_STATIC": "1",
        "OPENSSL_DIR": str(prefix),
    }


def package(prefix: Path, out_dir: Path) -> Path:
    """Write ``prefix`` as ``hyperi-static-libs-<triple>.tar.gz``, its ``.sha256`` and ``.json``.

    The archive holds one top directory, ``hyperi-static-libs``. The ``.json``
    is the manifest, so a release shows what it was built from without a download.

    Returns:
        The archive's path.

    """
    manifest = json.loads((prefix / MANIFEST).read_text(encoding="utf-8"))
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f"hyperi-static-libs-{manifest['target']}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(prefix, arcname="hyperi-static-libs")
    with archive.open("rb") as fh:
        digest = hashlib.file_digest(fh, "sha256").hexdigest()
    sha_file = archive.with_name(archive.name + ".sha256")
    sha_file.write_text(f"{digest}  {archive.name}\n", encoding="utf-8", newline="\n")
    shutil.copyfile(
        prefix / MANIFEST, out_dir / f"hyperi-static-libs-{manifest['target']}.json"
    )
    info(f"static-libs: {archive.name} sha256 {digest}")
    return archive


def release_tag() -> str:
    """Return the GitHub release tag for the current pins and build recipes.

    It names librdkafka and OpenSSL and ends in a digest of every pin and every
    recipe, so a changed flag gets a new release rather than a re-upload.
    """
    pins = static_libs()
    placeholder = Path("/prefix")
    recipes = {name: [s.argv for s in _recipe(name, placeholder, 1)] for name in pins}
    material = json.dumps({"pins": pins, "recipes": recipes}, sort_keys=True)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]
    rdkafka = pins["librdkafka"]["version"]
    openssl = pins["openssl"]["version"]
    return f"static-libs-rdkafka{rdkafka}-openssl{openssl}-{digest}"


def publish(out_dir: Path, *, target: str) -> int:
    """Create the release for :func:`release_tag` from the archives in ``out_dir``.

    An existing release is left alone, because a rebuild is not byte-identical
    and the digests consumers pin would stop matching.

    Args:
        out_dir: Holds each arch's archive, ``.sha256`` and manifest.
        target: The commit the tag goes on.

    Returns:
        0 when the release exists or was created, else 1.

    """
    tag = release_tag()
    if gh_run(["release", "view", tag], check=False).returncode == 0:
        info(f"static-libs: release {tag} already exists, nothing published")
        return 0
    assets = sorted(out_dir.glob("hyperi-static-libs-*"))
    archives = [a for a in assets if a.name.endswith(".tar.gz")]
    if len(archives) != len(_TRIPLES):
        error(
            f"static-libs: expected {len(_TRIPLES)} archives in {out_dir}, found {archives}"
        )
        return 1
    notes = out_dir / "notes.md"
    pins = json.dumps(static_libs(), indent=2, sort_keys=True)
    notes.write_text(
        f"Static librdkafka, OpenSSL, zstd and zlib.\n\n```json\n{pins}\n```\n",
        encoding="utf-8",
        newline="\n",
    )
    cmd = [
        "release", "create", tag,
        "--target", target,
        "--title", tag,
        "--notes-file", str(notes),
        "--latest=false",
        *map(str, assets),
    ]  # fmt: skip
    result = gh_run(cmd, check=False, capture=False)
    if result.returncode != 0:
        error(f"static-libs: gh release create {tag} exited {result.returncode}")
        return 1
    info(f"static-libs: published {tag}")
    return 0
