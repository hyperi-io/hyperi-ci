---
title: Static librdkafka and OpenSSL
type: reference
---

# Static librdkafka and OpenSSL

A Rust app that links librdkafka can carry it, OpenSSL, zstd and zlib inside its own binary instead of needing them in the runtime image. `hyperi-ci static-libs build` compiles the four into one static-only prefix, and the Static libs workflow publishes that prefix per arch as a `static-libs-*` GitHub release on this repo.

## How a Rust build links it

rdkafka-sys's own static build bundles an older librdkafka (4.10.0 bundles 2.12.1), and its `static-linking` feature reaches no code: the build script checks a feature of a different name. So the app keeps rdkafka's `dynamic-linking` feature, and pkg-config finds the static prefix instead of a system library:

| Variable | Value |
|---|---|
| `PKG_CONFIG_PATH` | `<prefix>/lib/pkgconfig` |
| `RDKAFKA_STATIC` | `1`, so the pkg-config crate emits a static link for every library in the prefix |
| `OPENSSL_STATIC` | `1` |
| `OPENSSL_DIR` | `<prefix>` |

The resulting binary needs only libc, libm, libgcc_s and the loader. `readelf -d` shows no librdkafka, libssl, libsasl2 or libcurl.

An app turns it on in `.hyperi-ci.yaml`:

```yaml
build:
  rust:
    static_libs: true
```

The Build job then downloads the `static_libs_release` archive pinned in versions.yaml for its own target triple, checks its sha256, unpacks it, and sets the variables above for cargo. The prefix goes ahead of any `PKG_CONFIG_PATH` the runner already has. Packaging fails a binary that still needs librdkafka, libssl, libcrypto, libsasl2 or libcurl. Quality and Test keep linking the system librdkafka the runner image installs.

## What is built

librdkafka is configured with SSL, zstd and zlib, and without GSSAPI (libsasl2) or curl. SCRAM and the callback form of OAUTHBEARER are built in. The OIDC token fetch, which needs curl, is not. Its mklove `configure` runs with `--no-download`, so it fetches nothing the pins do not cover.

OpenSSL is built `no-shared` with `--openssldir=/etc/ssl`, so it reads the CA bundle from `/etc/ssl/cert.pem` and `/etc/ssl/certs`, where Debian and Ubuntu images keep `ca-certificates`.

Every `.pc` file names its prefix as `${pcfiledir}/../..`, so the archive works wherever it is unpacked. The build fails if a static archive is missing or `rdkafka.pc` links sasl2 or curl.

## Pins, release and assets

The source versions and digests are `static_libs:` in `src/hyperi_ci/config/versions.yaml`, built in the order listed. Each digest is the source archive's, from the project's checksum file or GitHub's asset digest. librdkafka publishes neither, so its digest is of the tag archive, and `commit` records the tag's commit.

These pins take no release-age cooldown: each moves to the newest upstream release, OpenSSL on its current LTS line. They are mature, widely used libraries, so staying current outweighs the delay. Checksum verification, the LTS line and the recorded digests still apply.

The release tag names librdkafka and OpenSSL and ends in a digest of every pin and every build recipe, for example `static-libs-rdkafka2.16.0-openssl3.5.9-<digest>`. A changed pin or flag publishes a new release. An unchanged one publishes nothing, and an existing release is never re-uploaded, because a rebuild is not byte-identical. Releases are never marked latest.

Each release carries three assets per Rust target triple (`x86_64-unknown-linux-gnu`, `aarch64-unknown-linux-gnu`), each built natively on its own GitHub-hosted Ubuntu 24.04 runner:

- `hyperi-static-libs-<triple>.tar.gz`, one top directory `hyperi-static-libs`
- `hyperi-static-libs-<triple>.tar.gz.sha256`
- `hyperi-static-libs-<triple>.json`, the manifest of pins it was built from, also inside the archive
