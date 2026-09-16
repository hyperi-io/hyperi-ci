# Rust CI Guide

Consumer-facing reference for hyperi-ci's Rust build pipeline: channel-gated release optimisation (Tier 1 allocator + LTO, Tier 2 PGO + BOLT) and the `.hyperi-ci.yaml` keys that drive it.

What a release dispatch prints and how to confirm a tier applied is [rust-release-verification.md](rust-release-verification.md). Symptoms and fixes are [rust-troubleshooting.md](rust-troubleshooting.md). Concurrent Rust work on your own machine is [rust-local-dev.md](rust-local-dev.md).

For PGO workload script specifics, see [`pgo-bolt.md`](../runtime/pgo-bolt.md).

---

## What you get

Measured on dfe-receiver v1.15.7 release canary (production workload mix -
HTTP/gRPC/OTLP/Kafka):

| Build | Binary size | vs baseline | Channel that applies |
|---|---|---|---|
| System allocator, thin LTO | ~14 MB | baseline | `spike`, `alpha` |
| jemalloc + fat LTO (Tier 1) | ~12 MB | -14% size, +10-20% throughput | `beta` |
| + PGO (Tier 2 partial) | ~9 MB | -36% size, +25-40% throughput | `release` (opt-in) |
| + BOLT (Tier 2 full) | ~9 MB | -36% size, +30-50% throughput | `release` (opt-in) |

**Build time cost**: Tier 2 adds roughly +14 min per arch per release
(PGO instrument ~5 min, workload ~5 min, PGO optimise ~4 min, BOLT ~2 min).
Not applied on `spike/alpha/beta` - release channel only.

Both amd64 AND arm64 runners support full Tier 2. BOLT has supported
aarch64 since LLVM 16 and the runner image provides everything for both
architectures.

---

## Channel x tier matrix

Defaults applied by channel (your `.hyperi-ci.yaml` can override
individual keys):

| Channel | Allocator | LTO | PGO | BOLT |
|---------|-----------|------|------|------|
| `spike` | jemalloc | thin | - | - |
| `alpha` | jemalloc | thin | - | - |
| `beta` | jemalloc | fat | - | - |
| `release` | jemalloc | fat | opt-in | opt-in (Linux only) |

**Allocator is jemalloc at every channel, no exceptions.** Rationale:
consistent allocator across spike/alpha/beta/release means fragmentation
patterns, `jeprof` profiles, and crash dumps all look the same regardless
of where a binary came from. ~10s extra compile per build, cached after
first run.

**LTO ramp**: thin at spike/alpha (fast feedback), fat at beta+. Fat LTO
adds 5-10 min per CI run - meaningful friction for rapid spike iteration,
worth the cost for beta/release.

**Tier 2 is release-only, opt-in**: PGO/BOLT add ~20 min per arch. Also, a
bad workload produces *negative* gains. They fire on manual `hyperi-ci
release <tag>` dispatches only - never on push.

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
- [ ] `.hyperi-ci.yaml`: `publish.channel: release`
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
lto = "thin"        # hyperi-ci overrides to "fat" on beta/release
codegen-units = 1
strip = true
panic = "abort"
opt-level = 3
```

**Critical**: `jemalloc` must NOT be in `default` features. hyperi-ci
injects `--features jemalloc` per channel; if it's already on by default
you lose the ability to opt out for debugging or canary comparisons.

**LTO source-level default stays `thin`** - hyperi-ci overrides to `fat`
on beta+ via `CARGO_PROFILE_RELEASE_LTO=fat`, so local `cargo build
--release` remains fast while CI builds get the fat-LTO benefit.

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
      bolt:
        enabled: true        # Linux only; skipped on macOS/Windows
```

Nothing to configure in hyperi-ci itself - these keys control per-project
Tier 2 behaviour.

### Workload script contract

Full spec: [`pgo-bolt.md`](../runtime/pgo-bolt.md). One-line
summary:

```bash
#!/usr/bin/env bash
# $1 = path to the instrumented binary (canonical contract)
# Also exported as HYPERCI_PGO_INSTRUMENTED_BINARY for convenience.
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

### LLVM version

`HYPERCI_LLVM_VERSION` (default `23`) controls which `bolt-NN` +
`llvm-bolt-NN` + `merge-fdata-NN` + `ld.lld-NN` get used. Bump it in your project only
if you need a specific LLVM major - otherwise trust the default.

---

## Skipping optimisation for one run

Tier 2 is four sequential cargo passes plus two workload runs, and a failed
BOLT attempt retries all three BOLT steps -- 35-45 minutes with both
architectures in parallel, as observed on the dfe-loader v1.17.5 and
v1.18.0 publishes. `skip-optimize` drops the optimisation stage for one run
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

A skipped run publishes like any other, release channel included. The build
emits a `::warning::` annotation on the run and `optimize=skipped` in the
profile line:

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

- [`rust-release-verification.md`](rust-release-verification.md) - the dispatch timeline and the grep markers that prove a tier applied
- [`rust-troubleshooting.md`](rust-troubleshooting.md) - symptom-to-fix tables, the canary lessons, and what a release costs
- [`rust-local-dev.md`](rust-local-dev.md) - per-project target dirs, sccache, mold, parallelism on your own machine
- [`pgo-bolt.md`](../runtime/pgo-bolt.md) - how to write a good PGO workload script
- [`templates/pgo-workload/`](../../templates/pgo-workload/) - reusable workload skeletons
- [`onboarding.md`](../migration/onboarding.md) - general onboarding to hyperi-ci
