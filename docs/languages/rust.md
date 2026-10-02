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

The `.hyperi-ci.yaml` opt-in, the workload script contract, runner
requirements, reading the result in the log, the LLVM version knob, running
it without a release, skipping it for one run, and opting out entirely are
all in [rust-tier2.md](rust-tier2.md).

---

## The Build job's time limit

The Build job stops at 135 minutes, per matrix leg. That is at least twice the longest successful build across the fleet (issue #262), but a Tier 2 release can outrun it: dfe-transform-elastic's PGO-only Build took 93 minutes on 2026-09-16, and BOLT adds an instrument build, a second workload run and an optimise build on top.

A caller raises it in `with:`, the same on every `<lang>-ci.yml`:

```yaml
jobs:
  ci:
    uses: hyperi-io/hyperi-ci/.github/workflows/rust-ci.yml@main
    with:
      build-timeout-minutes: 240
```

Leaving it out, or passing 0, keeps 135. A GitHub-hosted runner stops a job at 360 whatever is set, and a self-hosted one at 7200 (5 days). Pass it as a number, unquoted: a quoted `"240"` is a string, and GitHub refuses the whole run before any job starts. `hyperi-ci init` does not scaffold it.

---

## References

- [`rust-tier2.md`](rust-tier2.md) - the Tier 2 opt-in, workload script contract, skipping it for one run, and opting out
- [`rust-release-verification.md`](rust-release-verification.md) - the dispatch timeline, the grep markers that prove a tier applied, and what a release costs
- [`rust-troubleshooting.md`](rust-troubleshooting.md) - symptom-to-fix tables and the canary lessons
- [`rust-local-dev.md`](rust-local-dev.md) - per-project target dirs, sccache, mold, parallelism on your own machine
- [`pgo-bolt.md`](../runtime/pgo-bolt.md) - how to write a good PGO workload script
- [`templates/pgo-workload/`](../../templates/pgo-workload/) - reusable workload skeletons
- [`onboarding.md`](../migration/onboarding.md) - general onboarding to hyperi-ci
