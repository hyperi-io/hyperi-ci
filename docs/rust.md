# Rust CI Guide

Single consumer-facing reference for hyperi-ci's Rust build pipeline. Covers
channel-gated release optimisation (Tier 1 allocator + LTO, Tier 2 PGO +
BOLT), local developer hygiene, and operational troubleshooting.

For PGO workload script specifics, see [`PGO-WORKLOAD-GUIDE.md`](PGO-WORKLOAD-GUIDE.md).
For the internal design rationale, see
`hyperi-ai/standards/rules/rust.md` → *hyperi-ci Release-Track Optimisation*.

---

## What you get

Measured on dfe-receiver v1.15.7 release canary (production workload mix —
HTTP/gRPC/OTLP/Kafka):

| Build | Binary size | vs baseline | Channel that applies |
|---|---|---|---|
| System allocator, thin LTO | ~14 MB | baseline | `spike`, `alpha` |
| jemalloc + fat LTO (Tier 1) | ~12 MB | −14% size, +10-20% throughput | `beta` |
| + PGO (Tier 2 partial) | ~9 MB | −36% size, +25-40% throughput | `release` (opt-in) |
| + BOLT (Tier 2 full) | ~9 MB | −36% size, +30-50% throughput | `release` (opt-in) |

**Build time cost**: Tier 2 adds roughly +14 min per arch per release
(PGO instrument ~5 min, workload ~5 min, PGO optimise ~4 min, BOLT ~2 min).
Not applied on `spike/alpha/beta` — release channel only.

Both amd64 AND arm64 runners support full Tier 2. BOLT has supported
aarch64 since LLVM 16 and the runner image provides everything for both
architectures.

---

## Channel × tier matrix

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

**Tier 2 is release-only, opt-in**: PGO/BOLT add ~20 min per arch. Also, a
bad workload produces *negative* gains. They fire on manual `hyperi-ci
release <tag>` dispatches only — never on push.

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
- [ ] First canary release dispatch: watch for the grep markers in [Verification](#verification)

If any box can't be ticked, stop — ask in #hyperi-ci before dispatching a
release.

---

## Tier 1 — allocator + LTO

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

**LTO source-level default stays `thin`** — hyperi-ci overrides to `fat`
on beta+ via `CARGO_PROFILE_RELEASE_LTO=fat`, so local `cargo build
--release` remains fast while CI builds get the fat-LTO benefit.

### main.rs wiring

```rust
#[cfg(feature = "jemalloc")]
#[global_allocator]
static GLOBAL: tikv_jemallocator::Jemalloc = tikv_jemallocator::Jemalloc;
```

Nothing else — no runtime switching, no environment detection. Let cargo
features drive it.

### Binary size overhead

jemalloc adds approximately 400-500 KB to a stripped release binary
(measured on dfe-receiver: +491 KB on a 14 MB baseline, +3.5%).

---

## Tier 2 — PGO + BOLT

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

Nothing to configure in hyperi-ci itself — these keys control per-project
Tier 2 behaviour.

### Workload script contract

Full spec: [`PGO-WORKLOAD-GUIDE.md`](PGO-WORKLOAD-GUIDE.md). One-line
summary:

```bash
#!/usr/bin/env bash
# $1 = path to the instrumented binary (canonical contract)
# Also exported as HYPERCI_PGO_INSTRUMENTED_BINARY for convenience.
# Must exercise real data-processing hot paths for >= 60s (300s recommended).
# Must self-terminate at duration_secs — the wrapper timeout is safety, not runtime.
# Must exit 0 on success; non-zero aborts the build (bad profile > no profile).
```

See dfe-receiver's [`scripts/pgo-workload.sh`](/projects/dfe-receiver/scripts/pgo-workload.sh)
and [`tools/pgo-driver/`](/projects/dfe-receiver/tools/pgo-driver) for a
working reference that drives all 9 protocols against a testcontainer
Kafka. Templates for common app shapes (HTTP server, gRPC server, Kafka
producer/consumer, multi-protocol) live in
[`templates/pgo-workload/`](../templates/pgo-workload/).

### Runner requirements

- **apt.llvm.org egress** — `bolt-NN` (LLVM's post-link optimiser) isn't
  in Ubuntu's default universe repo. hyperi-ci adds the repo
  scheme-agnostically if it's not already present (i.e. works on vanilla
  GH runners and on self-hosted runners that pre-provision the repo
  under any filename).
- **crates.io egress** — for `cargo install cargo-pgo --locked`.
- **Linux runners** — BOLT doesn't apply on macOS/Windows targets.
- **Native arm64 runner for arm64 builds** — `ubuntu-24.04-arm`. Cross-
  compiled arm64 from amd64 cannot collect arm64-native PGO profiles
  (no way to execute the instrumented binary on the wrong host).

### LLVM version

`HYPERCI_LLVM_VERSION` (default `22`) controls which `bolt-NN` +
`llvm-bolt-NN` + `merge-fdata-NN` + `ld.lld-NN` get used. Bump it in your project only
if you need a specific LLVM major — otherwise trust the default.

---

## Release dispatch flow

```
0:00  Setup: runners claimed (arc-runner-16cpu on amd64, ubuntu-24.04-arm on arm64)
0:30  Native deps install — bolt-22, binutils via apt.llvm.org
1:00  cargo install cargo-pgo --locked
2:00  Cargo build deps (cached after first run)
2:00  hyperi-ci: "Rust build optimisation: channel=release, allocator=jemalloc, lto=fat, pgo=on, bolt=on"
2:00  PGO: building instrumented binary
7:00  Instrumented build complete
7:00  Workload: Kafka container up, pgo-driver compiled, traffic driven for 300s
12:00 Workload complete, profile data: ~5 MiB collected
12:00 PGO: building optimised binary (fresh compile with profile data)
16:00 PGO-optimised build complete
16:00 llvm-bolt + merge-fdata + ld.lld shim: ~/.local/bin/* -> /usr/bin/*-22
16:00 BOLT: building instrumented binary
18:00 BOLT: applying profile, emitting final binary
18:00 Artifact upload
```

Both archs run concurrently. Release pipeline critical path ≈ 20 min
from dispatch to published artifacts.

---

## Verification

### On the shipped binary

Binary is stripped — `nm` won't show symbols. Use `strings`:

```bash
strings /path/to/binary | grep -iE 'jemalloc|je_mallctl' | head
# Expect: jemalloc_bg_thd, jemalloc, <jemalloc>: %s: %.*s:%.*s
```

For BOLT — strip removes section markers, so the CI build log is the
authoritative source (next section). If you need binary-level proof,
build with `strip = false` locally (`cargo pgo bolt optimize` on your
machine), then:

```bash
llvm-readelf --sections ./<binary> | grep -E '\.bolt|\.text.hot'
```

### In the CI build log

Grep the Build job log for these exact markers:

```
Rust build optimisation: channel=release, allocator=jemalloc, lto=fat, pgo=on, bolt=on
PGO: building instrumented binary for <triple>
cargo pgo build -- --target <triple> --features jemalloc
PGO instrumentation build finished successfully
<your workload output — pgo-workload: ...>
Found 1 PGO profile file with total size X.XX MiB
PGO: building optimised binary
PGO-optimized binary <name> built successfully
llvm-bolt shim: ~/.local/bin/llvm-bolt -> /usr/bin/llvm-bolt-22
merge-fdata shim: ~/.local/bin/merge-fdata -> /usr/bin/merge-fdata-22
ld.lld shim: ~/.local/bin/ld.lld -> /usr/bin/ld.lld-22
BOLT: building instrumented binary for <triple> (linker forced to lld)
BOLT: building instrumented binary
cargo pgo bolt build -- --target <triple> --features jemalloc
BOLT: optimising binary
```

If any are missing, a tier wasn't applied. See [Troubleshooting](#troubleshooting).

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

Library crates (no `[[bin]]`) skip this whole path — consumers choose
their own build profile when compiling from crates.io source. hyperi-ci
detects library-only crates and doesn't try to apply allocator/LTO
overrides or PGO.

---

## Local developer hygiene

Not strictly CI, but relevant: concurrent multi-project Rust development
on a shared machine. This section is an **example setup** from
`desktop-derek` — adapt to your machine's specifics.

### Per-project target directories

A shared `CARGO_TARGET_DIR` serialises builds (file lock) and causes
massive duplication across projects that use different rustflags.
Per-project target directories via symlinks on a fast disk unlock
concurrent builds and enable per-project `cargo clean`.

```bash
# Remove any global CARGO_TARGET_DIR env var (keep CARGO_HOME)
sudo sed -i '/^CARGO_TARGET_DIR=/d' /etc/environment

# Create per-project targets under a fast cache disk
sudo mkdir -p /cache/cargo-targets && sudo chown "$USER:$USER" /cache/cargo-targets

for proj in /projects/*/Cargo.toml; do
    dir=$(dirname "$proj"); name=$(basename "$dir")
    mkdir -p "/cache/cargo-targets/${name}"
    rm -rf "${dir}/target"
    ln -sfn "/cache/cargo-targets/${name}" "${dir}/target"
done
```

CI impact: none. `build.py` falls back to `target/` when
`CARGO_TARGET_DIR` is unset; symlinks are transparent to Cargo.

### sccache (object-level dedup)

With per-project targets, duplication across projects is still possible
— sccache caches the compiled objects themselves, independent of target
directory.

```bash
cargo install sccache --locked
```

```toml
# ~/.cargo/config.toml
[build]
rustc-wrapper = "sccache"
```

Cache size ~5-10 GB, controlled by `SCCACHE_CACHE_SIZE`.

### mold linker (native x86_64 only)

Linking is often the bottleneck in incremental builds. mold is 5-10×
faster than `ld` and 2-3× faster than `lld`.

```bash
sudo apt install mold
```

```toml
# ~/.cargo/config.toml
[target.x86_64-unknown-linux-gnu]
linker = "clang"
rustflags = ["-C", "link-arg=-fuse-ld=mold"]
```

**Native x86_64 only.** Cross-compilation to aarch64 must use BFD —
hyperi-ci's `build.py` already enforces this via linker wrapper scripts
that pass `-fuse-ld=bfd`. Don't touch the cross-compile linker config.

**Per-project rustflags override this** — Cargo does NOT merge rustflags
across configs. If your project has
`[target.x86_64-unknown-linux-gnu] rustflags = ["-C", "target-cpu=x86-64-v3"]`,
add the mold flag alongside:

```toml
[target.x86_64-unknown-linux-gnu]
rustflags = ["-C", "target-cpu=x86-64-v3", "-C", "link-arg=-fuse-ld=mold"]
```

### Parallelism (jobs)

With per-project target dirs, bump Cargo's `jobs` from the common `2`
override to something matching your core count. On a 32-core machine:

```toml
# ~/.cargo/config.toml
[build]
jobs = 8
```

Remove `jobs = 2` from per-project configs. Each concurrent build will
use 8 cores; 4 builds at once = 32 cores max, but compile and link
phases stagger in practice.

### Cache hygiene

```bash
cargo install cargo-sweep --locked
# Clean artifacts unused for 7 days, per project
for proj in /projects/*/Cargo.toml; do
    (cd "$(dirname "$proj")" && cargo sweep --time 7 2>/dev/null)
done
```

Weekly cron or on-demand.

---

## Troubleshooting

### Build / Tier 1

| Symptom | Fix |
|---|---|
| "allocator 'jemalloc' requested but feature not declared" | Add `jemalloc = ["dep:tikv-jemallocator"]` to your Cargo.toml `[features]` |
| jemalloc symbols absent from published binary | Check `cargo tree --features jemalloc` resolves correctly. Check your `#[cfg(feature = "jemalloc")]` allocator wiring actually compiled in |
| Build log says `channel=spike` on a release dispatch | You dispatched via the wrong path — use `hyperi-ci release <tag>`, not `gh workflow run` |

### Tier 2 / PGO

| Symptom | Fix |
|---|---|
| "no workload_cmd configured" warning | Set `build.rust.optimize.pgo.workload_cmd` in `.hyperi-ci.yaml` |
| "profile data too small" | Your workload didn't run long enough or didn't exercise hot paths. See PGO-WORKLOAD-GUIDE.md |
| "cargo-pgo unavailable — falling back to plain release build" | cargo-pgo install failed. Check network egress to crates.io, `cargo install cargo-pgo --locked` works locally. Non-fatal — Tier 1 still applies |
| "PGO workload failed — aborting" | Workload exited non-zero. Common causes: missing tooling on runner (use coreutils only), privileged port binding (use unprivileged), testcontainer advertised-listener mismatch (check readiness via host, not `docker exec`) |
| Release build is 3× slower than before | Expected with PGO+BOLT. Accept the cost or set `bolt.enabled: false` |
| Cross-compile (arm64 from amd64) PGO produces slow binary | PGO profiles are arch-specific. Cross-compile PGO is skipped — use a native arm64 runner (hyperi-ci's `ubuntu-24.04-arm` does this) |
| "Binary not found: `<name>`" | Binary auto-detection picked up a feature-gated helper bin. Add `required-features = ["..."]` to the secondary `[[bin]]` |

### Tier 2 / BOLT

| Symptom | Fix |
|---|---|
| "BOLT skipped — not a Linux target" | Expected on macOS/Windows targets. Non-fatal |
| "llvm-bolt not installed — skipping BOLT step" | `bolt-NN` apt package didn't install. Check runner egress to apt.llvm.org, GPG key fetch succeeded, `dpkg -l bolt-22` on the runner |
| "Cannot find merge-fdata: cannot find binary path" | The `bolt-NN` package ships both binaries; missing merge-fdata means the package didn't install. Same root cause as above. Fixed in hyperi-ci v1.10.4+ |
| "linking with `cc` failed: ld terminated with signal 11" (mold segfault) OR "ld: final link failed: invalid operation" (BFD) during `cargo pgo bolt build` | BOLT's `-Wl,-q` (`--emit-relocs`) isn't supported by mold/BFD. hyperi-ci v1.10.7+ forces `-fuse-ld=lld` for BOLT steps via `CARGO_TARGET_<TRIPLE>_RUSTFLAGS` (lld-NN shipped by the `lld-NN` apt package). On older versions, strip `-fuse-ld=mold` from the project's `[target.*] rustflags` to unblock |

### Local developer

| Symptom | Fix |
|---|---|
| Builds serialise despite removing `CARGO_TARGET_DIR` | Some projects still have `target/` as a real dir rather than a symlink. Re-run the per-project symlink loop |
| `cargo build` slower than expected after adding sccache | First build is cold — sccache populates. Check `sccache --show-stats` after 2-3 builds to confirm hits |
| mold linker error on aarch64 cross-compile | mold is x86_64-native only. Your project's `[target.aarch64-unknown-linux-gnu]` config must NOT use mold — hyperi-ci's wrapper enforces BFD |

---

## Lessons learned (v1.15.7 canary)

Every one of these cost a re-dispatch during the dfe-receiver canary.
You benefit from the fix already being in hyperi-ci v1.10.4+; knowing
*why* helps debugging.

1. **Tier 2 runs only on `release` dispatch, not every push.** Gated on
   `HYPERCI_CHANNEL=release` which the workflow sets when `inputs.tag`
   is non-empty. Push-to-main builds use plain release + Tier 1 only.

2. **Workload must self-terminate.** The `duration_secs + 600s` absolute
   timeout is a safety net, not extra runtime. If your workload hangs,
   PGO aborts and the canary fails.

3. **Feature-gated bins need `required-features`.** Without it,
   hyperi-ci's bin auto-detection tries to build the helper during PGO
   and trips on missing features. The workload driver binary in
   dfe-receiver is feature-gated behind `pgo-driver` for this reason.

4. **`bolt-NN` ships binaries version-suffixed only.** No unversioned
   `/usr/bin/llvm-bolt` or `/usr/bin/merge-fdata`. hyperi-ci's
   `_ensure_llvm_bolt_available()` shims both into `~/.local/bin/` at
   the configured LLVM version.

5. **apt.llvm.org isn't in default Ubuntu repos.** hyperi-ci adds it via
   `native-deps/rust.yaml` if missing; self-hosted runners that
   pre-provision trigger our scheme-agnostic dedup and are left alone.

6. **LLVM version is a parameter.** `HYPERCI_LLVM_VERSION` env var
   (default `22`) controls which bolt-NN gets used. Consumer projects
   don't override unless they need a specific LLVM major.

---

## Release cost

GitHub Actions minutes per release dispatch (private repo):

| Stage | Runtime | Runner | Cost |
|---|---|---|---|
| Quality | ~2 min | self-hosted ARC | $0 (fixed VM cost) |
| Test | ~5 min | self-hosted ARC | $0 (fixed VM cost) |
| Build amd64 (Tier 2) | ~16 min | GH-hosted amd64 @ $0.008/min | ~$0.13 |
| Build arm64 (Tier 2) | ~16 min | GH-hosted arm64 @ $0.005/min | ~$0.08 |
| Container + Publish | ~2 min | self-hosted ARC + network | ~$0.02 |

**Total per release: ~$0.23.** Weekly releases = ~$12/year per project.
Don't optimise this line — it's trivial.

---

## References

- [`PGO-WORKLOAD-GUIDE.md`](PGO-WORKLOAD-GUIDE.md) — how to write a good PGO workload script
- [`templates/pgo-workload/`](../templates/pgo-workload/) — reusable workload skeletons
- [dfe-receiver v1.15.7 canary notes](/projects/dfe-receiver/TODO.md) — *Canary run notes* section
- `hyperi-ai/standards/rules/rust.md` — internal design rationale + standards
- [`MIGRATION-GUIDE.md`](MIGRATION-GUIDE.md) — general onboarding to hyperi-ci

## Changelog

- v1.8.0 — Tier 1 + Tier 2 handler code added
- v1.10.1 — workload timeout widened to `duration_secs + 600s`
- v1.10.2 — versioned `llvm-bolt` shim
- v1.10.4 — `merge-fdata` added to shim (full BOLT toolchain)
