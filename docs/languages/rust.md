# Rust CI Guide

Consumer-facing reference for hyperi-ci's Rust build pipeline: the build tiers (Tier 1 allocator + LTO, Tier 2 PGO + BOLT), what turns each one on, and the `.hyperi-ci.yaml` keys that drive them.

What a release dispatch prints and how to confirm a tier applied is [rust-release-verification.md](rust-release-verification.md). Symptoms and fixes are [rust-troubleshooting.md](rust-troubleshooting.md). Concurrent Rust work on your own machine is [rust-local-dev.md](rust-local-dev.md).

For PGO workload script specifics, see [`pgo-bolt.md`](../runtime/pgo-bolt.md).

---

## What you get

Measured on dfe-receiver v1.15.7 release canary (production workload mix -
HTTP/gRPC/OTLP/Kafka):

| Build | Binary size | vs baseline | When it applies |
|---|---|---|---|
| System allocator, thin LTO | ~14 MB | baseline | none -- measurement baseline |
| jemalloc + fat LTO (Tier 1) | ~12 MB | -14% size, +10-20% throughput | a release run |
| + PGO (Tier 2 partial) | ~9 MB | -36% size, +25-40% throughput | a release run, opt-in |
| + BOLT (Tier 2 full) | ~9 MB | -36% size, +30-50% throughput | a release run, opt-in |

A non-release run sits between the first two rows, jemalloc with thin LTO, so it gets the allocator gain without the LTO one -- not separately measured.

**Build time cost**: Tier 2 adds roughly +14 min per arch per release
(PGO instrument ~5 min, workload ~5 min, PGO optimise ~4 min, BOLT ~2 min).
Release runs only.

Both amd64 AND arm64 runners support full Tier 2. BOLT has supported
aarch64 since LLVM 16 and the runner image provides everything for both
architectures.

---

## Build channel x tier matrix

The **build** channel is resolved per run, not from `release.channel`
(`_resolve_build_channel` in `languages/rust/build.py` never reads it): a run
that releases builds at `release`, every other run builds at `alpha`.
`HYPERCI_CHANNEL` in the workflow env forces one. Defaults per build channel
(your `.hyperi-ci.yaml` can override individual keys):

| Build channel | When | Allocator | LTO | PGO | BOLT |
|---|---|---|---|---|---|
| `alpha` | any non-release run | jemalloc | thin | - | - |
| `beta` | `HYPERCI_CHANNEL=beta` only | jemalloc | fat | - | - |
| `release` | a release run | jemalloc | fat | opt-in | opt-in (Linux only) |

**Allocator is jemalloc at every channel, no exceptions.** Rationale:
consistent allocator across alpha/beta/release means fragmentation
patterns, `jeprof` profiles, and crash dumps all look the same regardless
of where a binary came from. ~10s extra compile per build, cached after
first run.

**LTO ramp**: thin at alpha (fast feedback), fat at beta+. Fat LTO
adds 5-10 min per CI run - meaningful friction on an ordinary push,
worth the cost on a release.

**Tier 2 is release-only, opt-in**: PGO/BOLT add ~20 min per arch, and a bad
workload produces *negative* gains. They fire only on a run that releases -
`hyperi-ci push --release`, `hyperi-ci release`, or a tag / from-head
dispatch - never on an ordinary push to main.

---

## Quickstart

Want Tier 2 running by tomorrow. Follow the checklist; detail sections
below cover each step.

- [ ] `Cargo.toml`: `tikv-jemallocator` optional dep, `jemalloc` feature declared, **not** in default features
- [ ] `src/main.rs`: `#[global_allocator]` wired behind `#[cfg(feature = "jemalloc")]`
- [ ] `[profile.release]`: `lto = "thin"` (hyperi-ci overrides to fat), `codegen-units = 1`, `panic = "abort"`, `strip = true`
- [ ] `scripts/pgo-workload.sh`: exercises real hot paths, takes `$1` as binary path, self-terminates at `duration_secs`
- [ ] Workload driver binary (if Rust): declared as `[[bin]]` with `required-features`
- [ ] `.hyperi-ci.yaml`: `build.rust.optimize` stanza with pgo + bolt enabled
- [ ] Release through a release run - `hyperi-ci push --release` or `hyperi-ci release`; Tier 2 never fires on an ordinary push
- [ ] Runner has network egress to `apt.llvm.org` and `crates.io`
- [ ] Local validation: `cargo build --release --features jemalloc && strings target/release/<bin> | grep -i jemalloc` shows symbols
- [ ] Local PGO smoke: `cargo install cargo-pgo && cargo pgo build && ./scripts/pgo-workload.sh <path> && cargo pgo optimize` round-trips cleanly
- [ ] First canary release dispatch: watch for the grep markers in [Verification](rust-release-verification.md#verification)

If any box can't be ticked, stop - ask in #hyperi-ci before dispatching a
release.

---

## Tier 1 - allocator + LTO

### Cargo.toml preconditions

hyperi-ci performs a feature-existence check and skips allocator injection
(with a warning) if the feature isn't declared. Projects that aren't
ready yet keep building with the system allocator, no hard failure.

```toml
[dependencies]
tikv-jemallocator = { version = "0.6", optional = true }

[features]
default = []  # MUST NOT include jemalloc — hyperi-ci opts in per channel
jemalloc = ["dep:tikv-jemallocator"]

[profile.release]
lto = "thin"        # hyperi-ci overrides to "fat" on a release run
codegen-units = 1
strip = true
panic = "abort"
opt-level = 3
```

**Critical**: `jemalloc` must NOT be in `default` features. hyperi-ci adds
the allocator to whatever `build.rust.features` declares and passes that one
set on every cargo line for the target - plain release, PGO instrument, PGO
optimise and BOLT alike; if it's already on by default you lose the ability
to opt out for debugging or canary comparisons.

**LTO source-level default stays `thin`** - hyperi-ci overrides to `fat`
on a release run via `CARGO_PROFILE_RELEASE_LTO=fat`, so local `cargo build
--release` remains fast while release builds get the fat-LTO benefit.

### main.rs wiring

```rust
#[cfg(feature = "jemalloc")]
#[global_allocator]
static GLOBAL: tikv_jemallocator::Jemalloc = tikv_jemallocator::Jemalloc;
```

Nothing else - no runtime switching, no environment detection. Let cargo
features drive it.

### Binary size overhead

jemalloc adds approximately 400-500 KB to a stripped release binary
(measured on dfe-receiver: +491 KB on a 14 MB baseline, +3.5%).

---

## Tier 2 - PGO + BOLT

### .hyperi-ci.yaml opt-in

```yaml
build:
  rust:
    optimize:
      allocator: jemalloc   # explicit (defaulted per channel anyway)
      lto: fat              # explicit (defaulted per channel anyway)
      pgo:
        enabled: true
        workload_cmd: "bash scripts/pgo-workload.sh"
        duration_secs: 300   # minimum 60 — shorter produces bad profiles
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

### Workload script contract

Full spec: [`pgo-bolt.md`](../runtime/pgo-bolt.md). One-line
summary:

```bash
#!/usr/bin/env bash
# $1 = path to the instrumented binary (canonical contract)
# Also exported as HYPERCI_PGO_INSTRUMENTED_BINARY for convenience.
# PGO_WORKLOAD_DURATION_SECS carries duration_secs.
# Must exercise real data-processing hot paths for >= 60s (300s recommended).
# Must self-terminate at duration_secs — the wrapper timeout is safety, not runtime.
# Must exit 0 on success; non-zero aborts the build (bad profile > no profile).
```

See dfe-receiver's [`scripts/pgo-workload.sh`](https://github.com/hyperi-io/dfe-receiver/blob/main/scripts/pgo-workload.sh)
and [`tools/pgo-driver/`](https://github.com/hyperi-io/dfe-receiver/tree/main/tools/pgo-driver) for a
working reference that drives all 9 protocols against a testcontainer
Kafka. Templates for common app shapes (HTTP server, gRPC server, Kafka
producer/consumer, multi-protocol) live in
[`templates/pgo-workload/`](../../templates/pgo-workload/).

### Runner requirements

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

### Reading the result in the log

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

### LLVM version

`HYPERCI_LLVM_VERSION` (default `23`) controls which `bolt-NN` +
`llvm-bolt-NN` + `merge-fdata-NN` + `ld.lld-NN` get used. Bump it in your project only
if you need a specific LLVM major - otherwise trust the default.

### Running it without a release

A run that publishes nothing builds Tier 1. The `optimize-tier: release` dispatch input builds Tier 2 on one validate-only run, so a PGO or BOLT fix no longer needs a release to test. How to run it, and what it costs: [`pgo-bolt.md`](../runtime/pgo-bolt.md) -> *Validating your workload locally* -> *Testing it in CI without a release*.

---

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

---

## Opt-out

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

### Library crates

Library crates (no `[[bin]]`) skip this whole path - consumers choose
their own build profile when compiling from crates.io source. hyperi-ci
detects library-only crates and doesn't try to apply allocator/LTO
overrides or PGO.

---

## References

- [`rust-release-verification.md`](rust-release-verification.md) - the dispatch timeline, the grep markers that prove a tier applied, and what a release costs
- [`rust-troubleshooting.md`](rust-troubleshooting.md) - symptom-to-fix tables and the canary lessons
- [`rust-local-dev.md`](rust-local-dev.md) - per-project target dirs, sccache, mold, parallelism on your own machine
- [`pgo-bolt.md`](../runtime/pgo-bolt.md) - how to write a good PGO workload script
- [`templates/pgo-workload/`](../../templates/pgo-workload/) - reusable workload skeletons
- [`onboarding.md`](../migration/onboarding.md) - general onboarding to hyperi-ci
