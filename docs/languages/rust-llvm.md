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
3. The Rust tooling for that major runs together: `ld.lld`, `clang`, `llvm-bolt` and `merge-fdata`. If they aren't on the runner, they are installed at job time from apt.llvm.org: `bolt-NN` and `lld-NN` for every Rust repo, `clang-NN` only for a repo whose `.cargo/config.toml` links through clang (`linker = "clang"` or `-C linker=clang`).
4. The ARC runner images pre-bake the default and no other major, and their own unversioned `clang` and `ld.lld` point at it. A non-default major costs a job-time install, nothing more.

`llvm-profdata` is the exception: it comes from the rustc sysroot (`llvm-tools-preview`), so the profile is merged by the LLVM that wrote it.

## One major on the runner image

`src/hyperi_ci/config/toolchains/llvm.yaml` bakes exactly `versions.yaml` `llvm`, read through `${HYPERCI_LLVM_DEFAULT}`. It carries no list of its own, and neither `HYPERCI_LLVM_VERSION` nor `build.rust.llvm_version` reaches it, so the image and its `clang` / `ld.lld` alternatives always agree.

An older runner image may carry LLVM 19 to 22 as well, with its unversioned `clang` and `ld.lld` on 19. Those served the ClickHouse server fork's C++ build, which no longer exists. They are not a requirement.

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
LLVM 23 (.hyperi-ci.yaml): clang -> /usr/bin/clang-23, clang++ -> /usr/bin/clang++-23
LLVM 23 (.hyperi-ci.yaml): llvm-bolt -> /usr/bin/llvm-bolt-23, merge-fdata -> /usr/bin/merge-fdata-23, ld.lld -> /usr/bin/ld.lld-23
```

The designated major's tools are symlinked into `~/.local/bin`, first on PATH, ahead of whatever unversioned `ld.lld` and `clang` the runner carries. If the designated major is incomplete, the build warns, names both majors, and still keeps the link and BOLT on one major.

A gcc-driven link finds `ld.lld` on PATH. A project with `linker = "clang"` runs the shimmed `clang`, which takes `ld.lld` from its own install before PATH, so both kinds of link follow the designated major. `clang` is shimmed apart from `ld.lld`, so a runner without `clang-NN` leaves the `ld.lld` on PATH at the designated major. The clang it falls back to still links with its own major's lld, and the warning names that major.

ARC bakes `clang-NN` at the default major. Elsewhere, a repo that does not link through clang never installs it. The install would cost every job an apt.llvm.org fetch, and the `libclang1-NN` that comes with it can change which libclang bindgen picks. So on a GitHub-hosted runner such a repo has no `clang-NN`, and the PGO build leaves `clang` as the runner has it, with no warning and no fallback to another major.

The shims run in a PGO build only. Quality, test and a plain release build link with the runner's unversioned `clang` and `ld.lld`. On ARC those are the default major. hyperi-ci never installs the distro's unversioned `clang`, which is 18 on noble and would add a second LLVM.

## Moving to a new LLVM

1. Bump `llvm` in `versions.yaml` and release hyperi-ci.
2. Rebuild the ARC runner images (hyperi-infra). The image bakes the new default and points its `clang` and `ld.lld` at it. There is no second list to edit.

Projects that pin keep their major until they change the pin. There is nothing to edit in the workflows or the Rust handlers.
