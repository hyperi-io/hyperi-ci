# Rust CI troubleshooting

Symptom-to-fix tables for the Rust build pipeline, the lessons each canary re-dispatch bought, and what a release dispatch costs in Actions minutes.

The tiers and their config keys are [rust.md](rust.md). The log markers that say which tier applied are [rust-release-verification.md](rust-release-verification.md).

---

## Build / Tier 1

| Symptom | Fix |
|---|---|
| "allocator 'jemalloc' requested but feature not declared" | Add `jemalloc = ["dep:tikv-jemallocator"]` to your Cargo.toml `[features]` |
| jemalloc symbols absent from published binary | Check `cargo tree --features jemalloc` resolves correctly. Check your `#[cfg(feature = "jemalloc")]` allocator wiring actually compiled in |
| Build log says `channel=alpha` when you expected a release build | The run is not a release - `channel=alpha` is correct for every non-release run. Release with `hyperi-ci push --release` or `hyperi-ci release` |

## Tier 2 / PGO

| Symptom | Fix |
|---|---|
| "no workload_cmd configured" warning | Set `build.rust.optimize.pgo.workload_cmd` in `.hyperi-ci.yaml` |
| "profile data too small" | Your workload didn't run long enough or didn't exercise hot paths. See [pgo-bolt.md](../runtime/pgo-bolt.md) |
| "cargo-pgo unavailable - falling back to plain release build" | cargo-pgo install failed. Check network egress to crates.io, `cargo install cargo-pgo --locked` works locally. Non-fatal - Tier 1 still applies |
| "PGO workload failed - aborting" | Workload exited non-zero. Common causes: missing tooling on runner (use coreutils only), privileged port binding (use unprivileged), testcontainer advertised-listener mismatch (check readiness via host, not `docker exec`) |
| Release build is 3x slower than before | Expected with PGO+BOLT. Accept the cost or set `bolt.enabled: false` |
| Cross-compile (arm64 from amd64) PGO produces slow binary | PGO profiles are arch-specific. Cross-compile PGO is skipped - use a native arm64 runner (hyperi-ci's `ubuntu-24.04-arm` does this) |
| "Binary not found: `<name>`" | Binary auto-detection picked up a feature-gated helper bin. Add `required-features = ["..."]` to the secondary `[[bin]]` |

## Tier 2 / BOLT

| Symptom | Fix |
|---|---|
| "BOLT skipped - not a Linux target" | Expected on macOS/Windows targets. Non-fatal |
| "llvm-bolt not installed - skipping BOLT step" | `bolt-NN` apt package didn't install. Check runner egress to apt.llvm.org, GPG key fetch succeeded, `dpkg -l bolt-23` on the runner |
| "Cannot find merge-fdata: cannot find binary path" | The `bolt-NN` package ships both binaries; missing merge-fdata means the package didn't install. Same root cause as above. Fixed in hyperi-ci v1.10.4+ |
| "linking with `cc` failed: ld terminated with signal 11" (mold segfault) OR "ld: final link failed: invalid operation" (BFD) during `cargo pgo bolt build` | BOLT's `-Wl,-q` (`--emit-relocs`) isn't supported by mold/BFD. hyperi-ci v1.10.7+ forces `-fuse-ld=lld` for BOLT steps via `CARGO_TARGET_<TRIPLE>_RUSTFLAGS` (lld-NN shipped by the `lld-NN` apt package). On older versions, strip `-fuse-ld=mold` from the project's `[target.*] rustflags` to unblock |

## Local developer

| Symptom | Fix |
|---|---|
| Builds serialise despite removing `CARGO_TARGET_DIR` | Some projects still have `target/` as a real dir rather than a symlink. Re-run the per-project symlink loop |
| `cargo build` slower than expected after adding sccache | First build is cold - sccache populates. Check `sccache --show-stats` after 2-3 builds to confirm hits |
| mold linker error on aarch64 cross-compile | mold is x86_64-native only. Your project's `[target.aarch64-unknown-linux-gnu]` config must NOT use mold - hyperi-ci's wrapper enforces BFD |

The setup these refer to is [rust-local-dev.md](rust-local-dev.md).

---

## Lessons learned (v1.15.7 canary)

Every one of these cost a re-dispatch during the dfe-receiver canary.
You benefit from the fix already being in hyperi-ci v1.10.4+; knowing
*why* helps debugging.

1. **Tier 2 runs only on a build that ships, not every push.** Gated on
   `HYPERCI_CHANNEL=release`, which rust-ci.yml sets on the build step when
   `plan.outputs.will-release` is true. Validate-only builds use plain
   release + Tier 1 only.

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
   (default `23`) controls which bolt-NN gets used. Consumer projects
   don't override unless they need a specific LLVM major.

