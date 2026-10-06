<!--
Project:   HyperI CI
File:      docs/languages/rust-llvm.md
Purpose:   How the LLVM version for Rust builds is chosen, installed and pinned

License:   BUSL-1.1 -- HYPERI PTY LIMITED
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Rust and LLVM versions

Every Rust build links with `ld.lld` and, on a Tier 2 release, rewrites the binary with BOLT. Both come from ONE LLVM major per run, the designated major. It is config, not code. Moving the fleet to a new LLVM is a one-line change.

## The model

1. CI accepts any LLVM major a project asks for.
2. A project that asks for nothing gets the hyperi-ci default, `llvm` in `src/hyperi_ci/config/versions.yaml`. We keep it at upstream's latest stable.
3. The Rust tooling for that major runs together: `ld.lld`, `llvm-bolt` and `merge-fdata`. If they aren't on the runner, they are installed at job time from apt.llvm.org (`bolt-NN`, `lld-NN`).
4. The ARC runner images pre-bake the default, and their own unversioned `clang` and `ld.lld` point at it. A non-default major costs a job-time install, nothing more.

`llvm-profdata` is the exception: it comes from the rustc sysroot (`llvm-tools-preview`), so the profile is merged by the LLVM that wrote it.

## Choosing the major

Highest wins:

| Source | Example | Use it for |
|---|---|---|
| `HYPERCI_LLVM_VERSION` env | `HYPERCI_LLVM_VERSION=24` | trying a major on one run |
| `.hyperi-ci.yaml` | `build.rust.llvm_version: 23` | a project that must hold still |
| `versions.yaml` `llvm` | `llvm: "23"` | the fleet default |

```yaml
build:
  rust:
    llvm_version: 23
```

A value that isn't a whole-number major fails the stage before any cargo step.

Pin only when a build must not move under a default bump, such as a maintenance release that has to reproduce its toolchain after the default has moved on. Everything else inherits the default, and a default bump is a deliberate hyperi-ci release.

## What the log shows

One line per shim step names the major, where it came from, and every tool's path:

```text
LLVM 23 (.hyperi-ci.yaml): ld.lld -> /usr/bin/ld.lld-23
LLVM 23 (.hyperi-ci.yaml): llvm-bolt -> /usr/bin/llvm-bolt-23, merge-fdata -> /usr/bin/merge-fdata-23, ld.lld -> /usr/bin/ld.lld-23
```

The designated major's tools are symlinked into `~/.local/bin`, first on PATH, ahead of whatever unversioned `ld.lld` the runner carries. If the designated major is incomplete, the build warns, names both majors, and still keeps the link and BOLT on one major.

That governs links driven by gcc, which finds `ld.lld` on PATH. A project with `linker = "clang"` links with the `ld.lld` beside whichever clang it runs, so on ARC it follows the image's default major, NOT a non-default designated one. Clang-driven links follow any designated major once #519 installs `clang-NN` and shims `clang`.

## Moving to a new LLVM

1. Bump `llvm` in `versions.yaml` and release hyperi-ci.
2. Rebuild the ARC runner images (hyperi-infra). The image points its `clang` and `ld.lld` at the `versions.yaml` default. Until #519 lands, the majors it bakes still come from the `versions:` list in `config/toolchains/llvm.yaml`, so add the new major there too.

Projects that pin keep their major until they change the pin. There is nothing to edit in the workflows or the Rust handlers.
