# Rust Release-Track Build Optimisation

Channel-gated build optimisation for Rust binaries shipped through
hyperi-ci. This is consumer-facing — it's what you need to know to
turn Tier 1 and Tier 2 on for your project.

For the design rationale and internal contract, see
`hyperi-ai/standards/rules/rust.md` → *hyperi-ci Release-Track Optimisation*.

For PGO workload writing, see `docs/PGO-WORKLOAD-GUIDE.md`.

## Matrix

Defaults applied by channel (your `.hyperi-ci.yaml` can override
individual keys):

| Channel | Allocator | LTO | PGO | BOLT |
|---------|-----------|------|------|------|
| `spike` | jemalloc | thin | — | — |
| `alpha` | jemalloc | thin | — | — |
| `beta` | jemalloc | fat | — | — |
| `release` | jemalloc | fat | opt-in | opt-in (Linux only) |

**Allocator is jemalloc at every channel, no exceptions.** Rationale:
consistent allocator across spike/alpha/beta/release means fragmentation
patterns, `jeprof` profiles, and crash dumps all look the same regardless
of where a binary came from. ~10s extra compile per build, cached after
first run.

**LTO ramp**: thin at spike/alpha (fast feedback), fat at beta+. Fat LTO
adds 5-10 min per CI run — meaningful friction for rapid spike iteration,
worth the cost for beta/release.

## Tier 1 — preconditions

Before Tier 1 can do anything, your project must declare the jemalloc
feature and wire the global allocator. hyperi-ci performs a
feature-existence check and will skip allocator injection (with a
warning) if the feature isn't declared — so projects that aren't ready
yet continue to build with the system allocator.

**Cargo.toml**:

```toml
[dependencies]
tikv-jemallocator = { version = "0.6", optional = true }

[features]
# DO NOT put jemalloc in default — hyperi-ci opts in per channel.
default = []
jemalloc = ["dep:tikv-jemallocator"]
```

**src/main.rs** (top of file):

```rust
#[cfg(feature = "jemalloc")]
#[global_allocator]
static GLOBAL: tikv_jemallocator::Jemalloc = tikv_jemallocator::Jemalloc;
```

**[profile.release] in Cargo.toml** — keep `lto = "thin"` as the
source-level default. hyperi-ci overrides to `fat` on beta+ via
`CARGO_PROFILE_RELEASE_LTO=fat`, so local `cargo build --release`
remains fast while CI builds get the fat-LTO benefit.

## Tier 1 — verification

After a build on beta/release channel:

```bash
# Look for allocator marker in the CI build log
grep "Rust build optimisation:" <ci-log>
# Expected: "channel=release, allocator=jemalloc, lto=fat"

# On the published binary:
strings target/<target>/release/<binary> | grep -ciE 'jemalloc|je_mallctl'
# Expected: > 0 (binary is stripped so `nm` won't work; use `strings`)
```

Binary-size overhead: jemalloc adds approximately 400-500 KB to a
stripped release binary (measured on dfe-receiver: +491 KB on a 14 MB
baseline, +3.5%).

## Tier 2 — opt-in

PGO and BOLT are opt-in because:
1. They add 30-60 min to release builds (3-4× baseline).
2. A bad PGO workload produces *negative* gains — the compiler
   optimises the wrong hot paths.
3. They only apply on `release` channel, so they only trigger on the
   rare manual-dispatch release build.

**.hyperi-ci.yaml**:

```yaml
build:
  rust:
    optimize:
      pgo:
        enabled: true
        workload_cmd: "bash scripts/pgo-workload.sh"
        duration_secs: 300   # Minimum 60 — shorter = bad profile
      bolt:
        enabled: true         # Linux only; skipped on macOS/Windows
```

**The `workload_cmd` must be a self-contained script** that:
1. Accepts the instrumented binary path as `$1`.
2. Runs it with a realistic configuration.
3. Drives production-like traffic for `duration_secs`.
4. Cleans up on exit (kill receiver, stop containers, etc.).
5. Exits 0 on success.

See `docs/PGO-WORKLOAD-GUIDE.md` for the full four-rules. Templates for
common app shapes (HTTP server, gRPC server, Kafka producer, Kafka
consumer, multi-protocol) live in `templates/pgo-workload/`.

## Tier 2 — verification

```bash
# CI build log should contain all of these markers (order not guaranteed):
grep -E "cargo pgo build|cargo pgo optimize|cargo pgo bolt" <ci-log>

# Profile data should be non-trivial (min-size threshold enforced):
# Look for: "PGO profile data: NNNN bytes"

# On Linux release binary, BOLT-rewritten sections exist:
llvm-readelf --sections ./<binary> | grep -E '\.bolt|\.text.hot'
# Expected: at least one .text.hot or .bolt.* section
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| Build log says "allocator 'jemalloc' requested but feature not declared" | Add `jemalloc = ["dep:tikv-jemallocator"]` to your Cargo.toml `[features]` |
| PGO disabled with "no workload_cmd configured" warning | Set `build.rust.optimize.pgo.workload_cmd` in `.hyperi-ci.yaml` |
| PGO build fails with "profile data too small" | Your workload didn't run long enough or didn't exercise hot paths. See PGO-WORKLOAD-GUIDE.md |
| BOLT skipped with "not a Linux target" warning | Expected on macOS/Windows/cross-compile to non-Linux targets |
| Release build is 3× slower than before | Expected with PGO+BOLT enabled. Either accept the cost or set `bolt.enabled: false` |
| jemalloc symbols absent from published binary | Check `cargo tree --features jemalloc` resolves correctly. Check your `#[cfg(feature = "jemalloc")]` allocator wiring actually compiled in |
| Cross-compile (arm64) release build with PGO succeeds but published binary is slow | PGO profiles are arch-specific — the amd64 CI worker produces profiles that don't generalise to arm64. PGO on cross-compiled targets is currently skipped (logged) |

## Opt-out

To disable optimisations for a project (uncommon, primarily debug):

```yaml
build:
  rust:
    optimize:
      allocator: system     # Skip jemalloc everywhere
      lto: thin             # Skip fat LTO on beta+
      pgo:
        enabled: false
      bolt:
        enabled: false
```

## Library crates

Library crates (no `[[bin]]`) skip this whole path — consumers choose
their own build profile when compiling from crates.io source. hyperi-ci
detects library-only crates and doesn't try to apply allocator/LTO
overrides or PGO.

## Changelog

Added in hyperi-ci v1.8.0 — see `CHANGELOG.md` for the full feature
commit.
