<!--
Project:   HyperI CI
File:      docs/languages/rust-tier2.md
Purpose:   Setting up, skipping and opting out of Tier 2 (PGO + BOLT)

License:   BUSL-1.1 -- HYPERI PTY LIMITED
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Rust Tier 2: PGO + BOLT

Split out of [rust.md](rust.md), which keeps what Tier 2 buys and the
build-channel matrix. This page is for a project opting into Tier 2, or
needing to skip or disable it for one run.

## `.hyperi-ci.yaml` opt-in

```yaml
build:
  rust:
    optimize:
      allocator: jemalloc   # explicit (defaulted per channel anyway)
      lto: fat              # explicit (defaulted per channel anyway)
      pgo:
        enabled: true
        workload_cmd: "bash scripts/pgo-workload.sh"
        duration_secs: 300   # minimum 60 -- shorter produces bad profiles
        # Optional: runs once before the workload, off the workload's clock
        # (1h timeout of its own). For building a load driver or pulling images.
        workload_setup_cmd: "cargo build --release -p pgo-driver"
      bolt:
        enabled: true        # Linux only; skipped on macOS/Windows
      # strict: false        # default true: a release whose PGO/BOLT is skipped fails
```

The workload gets `duration_secs` as `PGO_WORKLOAD_DURATION_SECS` and must stop
by then. The wrapper allows `duration_secs + 600` before killing it, and a
timeout fails the release, so anything slow that happens before profiling
belongs in `workload_setup_cmd`.

Nothing to configure in hyperi-ci itself - these keys control per-project
Tier 2 behaviour.

## Workload script contract

Full spec: [`pgo-bolt.md`](../runtime/pgo-bolt.md). One-line
summary:

```bash
#!/usr/bin/env bash
# $1 = path to the instrumented binary (canonical contract)
# Also exported as HYPERCI_PGO_INSTRUMENTED_BINARY for convenience.
# PGO_WORKLOAD_DURATION_SECS carries duration_secs.
# Must exercise real data-processing hot paths for >= 60s (300s recommended).
# Must self-terminate at duration_secs -- the wrapper timeout is safety, not runtime.
# Must exit 0 on success; non-zero aborts the build (bad profile > no profile).
```

See dfe-receiver's [`scripts/pgo-workload.sh`](https://github.com/hyperi-io/dfe-receiver/blob/main/scripts/pgo-workload.sh) <!-- doc-paths: ignore -->
and [`tools/pgo-driver/`](https://github.com/hyperi-io/dfe-receiver/tree/main/tools/pgo-driver) for a
working reference that drives all 9 protocols against a testcontainer
Kafka. Templates for common app shapes (HTTP server, gRPC server, Kafka
producer/consumer, multi-protocol) live in
[`templates/pgo-workload/`](../../templates/pgo-workload/).

## Runner requirements

- **apt.llvm.org egress** - `bolt-NN` (LLVM's post-link optimiser) isn't
  in Ubuntu's default universe repo. hyperi-ci adds the repo
  scheme-agnostically if it's not already present (i.e. works on vanilla
  GH runners and on self-hosted runners that pre-provision the repo
  under any filename).
- **crates.io egress** - for `cargo install cargo-pgo --locked`.
- **Linux runners** - BOLT doesn't apply on macOS/Windows targets.
- **Native arm64 runner for arm64 builds** - `ubuntu-24.04-arm`. Cross-
  compiled arm64 from amd64 cannot collect arm64-native PGO profiles
  (no way to execute the instrumented binary on the wrong host).

## Reading the result in the log

Every Tier 2 skip is warn-only, so a green run does not prove the pass ran.
Each arch's build group ends with one line stating what that arch got:

```
optimised: pgo=yes bolt=no allocator=jemalloc
```

`bolt=no` means the toolchain was incomplete or its workload failed;
`allocator=system` means the feature is declared nowhere in the workspace.
A warn line earlier in the group names the reason.

A virtual workspace root (only `[workspace]`) counts as a Rust project for
the `bolt-NN` / `lld-NN` install, and the allocator check reads the root
manifest's `[features]` unioned with every member's.

When BOLT will run, every compile of the pipeline builds with `CARGO_PROFILE_RELEASE_STRIP=none` whatever your `[profile.release]` declares, because cargo hashes profile settings into symbol names and the PGO profile matches only names compiled under the same settings. Packaging strips the shipped binary. When it cannot, a CI build fails naming the binary, and a local build warns. Thousands of `no profile data available` warnings in the BOLT optimise step mean the profile did not match.

## LLVM version and running without a release

`HYPERCI_LLVM_VERSION` (default `23`) controls which `bolt-NN` +
`llvm-bolt-NN` + `merge-fdata-NN` + `ld.lld-NN` get used. Bump it in your project only
if you need a specific LLVM major - otherwise trust the default.

A run that publishes nothing builds Tier 1. The `optimize-tier: release` dispatch input builds Tier 2 on one validate-only run, so a PGO or BOLT fix no longer needs a release to test. How to run it, and what it costs: [`pgo-bolt.md`](../runtime/pgo-bolt.md) -> *Validating your workload locally* -> *Testing it in CI without a release*.

On the same kind of run, `bolt-optimize-args` replaces the BOLT optimise flags so a BOLT fault can be bisected one dispatch at a time: [`pgo-bolt.md`](../runtime/pgo-bolt.md) -> *Bisecting BOLT*.

## Skipping optimisation for one run

Tier 2 is four sequential cargo passes plus two workload runs, and a failed
BOLT attempt retries all three BOLT steps -- 35-45 minutes with both
architectures in parallel, as observed on the dfe-loader v1.17.5 and
v1.18.0 releases. `skip-optimize` drops the optimisation stage for one run
without editing `.hyperi-ci.yaml`. For Rust that means no PGO and no BOLT.
Tier 1 (allocator + LTO) still applies. A language with no optimisation
stage ignores it.

Three ways in, highest wins:

| Where | Key | Scope |
|---|---|---|
| Dispatch input / workflow env | `skip-optimize` | this run |
| Repo or org variable | `HYPERCI_SKIP_OPTIMIZE` | every run in the repo |
| `.hyperi-ci.yaml` | `build.skip_optimize` | the project |

```bash
gh workflow run ci.yml -f from-head=true -f bump=patch -f skip-optimize=true
```

`hyperi-ci init` writes the dispatch input into the consumer `ci.yml`. A
repo scaffolded earlier adds it by hand or uses the repo variable.
Optimisation is on unless something asks otherwise.

Skipping optimisation and shipping a release are two separate consents. On
`alpha` and `beta` a skipped run builds and publishes like any other. On
`release`, where the project would otherwise run PGO or BOLT, the build
refuses before compiling anything:

```
Refusing to build a release with the optimisation stage skipped: ...
Re-run with the 'release-unoptimized: true' dispatch input ...
```

To ship a fast unoptimised release anyway, set BOTH inputs on the one run:

```bash
gh workflow run ci.yml -f from-head=true -f bump=patch \
  -f skip-optimize=true -f release-unoptimized=true
```

`release-unoptimized` is a per-run input only, with no repo variable and no
`.hyperi-ci.yaml` key, so the consent never outlives the run it was given
for. A release with no Tier 2 configured loses nothing by skipping and is
never refused.

Either way the build emits a `::warning::` annotation on the run and
`optimize=skipped` in the profile line:

```
Rust build optimisation: channel=release, allocator=jemalloc, lto=fat, optimize=skipped
```

## Opt-out and library crates

To disable optimisations for a project (uncommon, primarily debug):

```yaml
build:
  rust:
    optimize:
      allocator: system
      lto: thin
      pgo:
        enabled: false
      bolt:
        enabled: false
```

Library crates (no `[[bin]]`) skip this whole path - consumers choose
their own build profile when compiling from crates.io source. hyperi-ci
detects library-only crates and doesn't try to apply allocator/LTO
overrides or PGO.

## See also

- [rust.md](rust.md) -- what Tier 2 buys, the build-channel matrix, the quickstart checklist
- [rust-release-verification.md](rust-release-verification.md) -- the dispatch timeline and grep markers that prove a tier applied
- [rust-troubleshooting.md](rust-troubleshooting.md) -- symptom-to-fix tables
- [`pgo-bolt.md`](../runtime/pgo-bolt.md) -- how to write a good PGO workload script
