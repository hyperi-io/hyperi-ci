# Rust Build Optimisation Plan

## Status: Approved — ready to implement

This plan covers changes to the local developer workstation (`desktop-derek`) and
alignment with hyperi-ci's Rust build pipeline. The goal is to unlock concurrent
multi-project Rust builds on the dev machine while preserving (and improving) the
shared-cache benefits used by CI.

---

## Problem Statement

Derek works on 4-10 Rust projects concurrently under `/projects/`, switching between
them while builds complete. The current configuration **serialises all builds** because
every project shares a single Cargo target directory.

### Current environment (`desktop-derek`)

| Item | Value |
|------|-------|
| CPU | 32 cores |
| RAM | 60 GB |
| `/cache` disk | 503 GB (45 GB used, 433 GB free) |
| Rust projects | 18 under `/projects/` |
| `CARGO_HOME` | `/cache/cargo` (set in `/etc/environment`) |
| `CARGO_TARGET_DIR` | `/cache/cargo-target` (set in `/etc/environment`) |
| `SCCACHE_DIR` | `/cache/sccache` (set in `/etc/environment`, but **sccache is not installed**) |
| `CCACHE_DIR` | `/cache/ccache` (set in `/etc/environment`) |
| Linkers available | lld-19, clang-19 (mold **not** installed) |
| Per-project `jobs` | `2` in most `.cargo/config.toml` files |

### Why the shared `CARGO_TARGET_DIR` is harmful

1. **Build serialisation** — Cargo takes a file lock on the target directory. When
   project B starts building while project A holds the lock, B blocks until A finishes.
   No concurrency at all.

2. **Cross-project cache invalidation** — Different projects compile with different
   `rustflags` (e.g. `-C target-cpu=native` vs `-C target-cpu=x86-64-v3`). Artifacts
   clobber each other, triggering unnecessary recompilation.

3. **Massive duplicate bloat** — Analysis of `/cache/cargo-target/debug/deps/` shows:
   - 26 copies of `libitertools` (different metadata hashes)
   - 26 copies of `libgetrandom`
   - 22 copies of `libprost_types` / `libprost`
   - 688 unique crate builds total, 37 GB
   - Nothing can be cleaned per-project because `cargo clean` nukes everything.

4. **rust-analyzer contention** — VSCode runs a rust-analyzer instance per open
   project. Each does check builds that also fight for the shared target lock.

### Impact on CI (hyperi-ci)

- The GitHub Actions workflows in `rust-ci.yml` cache `target/` and `~/.cargo/` via
  `actions/cache@v5`. They do **not** rely on `CARGO_TARGET_DIR`.
- `build.py` reads `CARGO_TARGET_DIR` only to locate built artifacts (lines 889, 1045).
  It falls back to `target/` when the env var is unset. No change needed there.
- Self-hosted ARC runners on the DevEx k8s cluster may inherit `/etc/environment` if
  they run on this machine. Changes here should be tested on a runner.
- `local_jobs: 2` is defined in `src/hyperi_ci/config/defaults.yaml` but **never wired
  into `build.py`** — it's dead config.

---

## Changes

### 1. Per-project target directories on `/cache`

**What:** Remove the global `CARGO_TARGET_DIR` env var. Create per-project target
directories on the `/cache` disk via symlinks.

**Why:** Unlocks concurrent builds. Each project gets its own lock, its own incremental
cache, and can be cleaned independently.

**How:**

```bash
# 1. Remove CARGO_TARGET_DIR from /etc/environment
#    (keep CARGO_HOME, SCCACHE_DIR, CCACHE_DIR)
sudo sed -i '/^CARGO_TARGET_DIR=/d' /etc/environment

# 2. Create the per-project target root
sudo mkdir -p /cache/cargo-targets
sudo chown derek:derek /cache/cargo-targets

# 3. Symlink each project's target/ to /cache/cargo-targets/<name>/
for proj in /projects/*/Cargo.toml; do
    dir=$(dirname "$proj")
    name=$(basename "$dir")
    mkdir -p "/cache/cargo-targets/${name}"
    # Remove existing target dir/symlink if present
    rm -rf "${dir}/target"
    ln -sfn "/cache/cargo-targets/${name}" "${dir}/target"
done

# 4. Clean up the old shared target dir (37 GB recovered)
#    Do this AFTER verifying builds work with the new layout
rm -rf /cache/cargo-target
```

**Disk impact:** Individual project targets are typically 1-4 GB each. With 18 projects
the total will be 30-60 GB, but:
- No more 26x duplicate crate explosion
- Each project can be `cargo clean`ed independently
- `cargo-sweep` can age out stale artifacts (see step 6)

**CI alignment:** `build.py` already falls back to `target/` when `CARGO_TARGET_DIR` is
unset. The symlinks are transparent to Cargo. No changes to hyperi-ci needed for this.

### 2. Install and configure sccache

**What:** Install sccache and configure it as the rustc wrapper. This provides
cross-project compilation caching **without** the serialisation of a shared target dir.

**Why:** When project B needs the same `tokio` that project A already compiled (same
source, same flags), sccache serves the cached object instantly. This is the correct
deduplication layer — object-level, not directory-level.

**How:**

```bash
# Install sccache (goes to /cache/cargo/bin/ via CARGO_HOME)
cargo install sccache --locked

# SCCACHE_DIR is already set in /etc/environment: /cache/sccache
# Configure in ~/.cargo/config.toml:
```

Add to `~/.cargo/config.toml`:
```toml
[build]
rustc-wrapper = "sccache"
```

**Disk impact:** sccache cache is typically 5-10 GB, controllable via
`SCCACHE_CACHE_SIZE` (default 10 GB).

**CI alignment:** Consider adding sccache to the ARC runner base image and/or the
`rust-ci.yml` workflow. For self-hosted runners with persistent storage, sccache
across builds is a significant win. For GitHub-hosted runners, it's less useful since
the cache is ephemeral. This is optional and can be a follow-up.

### 3. Install mold linker

**What:** Install the mold linker and configure it for native x86_64 builds.

**Why:** Linking is often the bottleneck in incremental Rust builds. mold is 5-10x
faster than the default `ld` and 2-3x faster than `lld`. The difference is most
noticeable on large binaries (dfe-receiver, vrl, hyperi-rustlib).

**How:**

```bash
sudo apt install mold
```

Add to `~/.cargo/config.toml`:
```toml
[target.x86_64-unknown-linux-gnu]
linker = "clang"
rustflags = ["-C", "link-arg=-fuse-ld=mold"]
```

**Important:** mold is for **native builds only**. Cross-compilation to aarch64 must
continue using BFD (as hyperi-ci's `build.py` already enforces via linker wrapper
scripts that pass `-fuse-ld=bfd`). The per-project `.cargo/config.toml` cross-compile
settings for `[target.aarch64-unknown-linux-gnu]` are unaffected.

**CI alignment:** For CI cross-compilation, mold must NOT be used. `build.py` already
handles this correctly. For native CI builds on ARC runners, mold could be added to the
runner image as an optimisation. Optional follow-up.

### 4. Raise Cargo parallelism from 2 to 8

**What:** Set `jobs = 8` globally and remove the per-project `jobs = 2` overrides.

**Why:** With per-project target dirs (no lock contention), multiple builds run
genuinely concurrently. `jobs = 8` means each build uses 8 cores. If 4 builds run at
once that's 32 cores (the machine's max), but in practice compile and link phases
stagger, so the machine won't be constantly saturated. This leaves headroom for
GNOME/RDP/rust-analyzer.

**How:**

Add to `~/.cargo/config.toml`:
```toml
[build]
jobs = 8
```

Remove `jobs = 2` from each project's `.cargo/config.toml`:
- `/projects/dfe-archiver/.cargo/config.toml`
- `/projects/dfe-fetcher/.cargo/config.toml`
- `/projects/dfe-loader/.cargo/config.toml` (shares file with dfe-fetcher)
- `/projects/dfe-protocol-sdk/.cargo/config.toml`
- `/projects/dfe-receiver/.cargo/config.toml`
- `/projects/dfe-transform-elastic/.cargo/config.toml`
- `/projects/dfe-transform-splack/.cargo/config.toml`
- `/projects/dfe-transform-vector/.cargo/config.toml`
- `/projects/dfe-transform-vrl/.cargo/config.toml`
- `/projects/dfe-transform-wasm/.cargo/config.toml`
- `/projects/vrl/.cargo/config.toml` (currently has `jobs = 4`)

**Do NOT touch:**
- `/projects/hyperi-rustlib/.cargo/config.toml` — this has no `jobs` setting, has its
  own complex cross-compile and clippy config.

**CI alignment:** Update `src/hyperi_ci/config/defaults.yaml`:
```yaml
rust:
  local_jobs: 8
```
Note: `local_jobs` is currently dead config (never read by `build.py`). This change
makes it ready for when it's wired up. Consider wiring it into `build.py` as a
follow-up.

### 5. Create `~/.cargo/config.toml`

This file does not currently exist. Create it with the combined settings from steps
2-4:

```toml
# Global Cargo configuration for desktop-derek
#
# Per-project .cargo/config.toml files override these where they conflict.
# Cross-compilation settings remain in per-project configs.

[build]
jobs = 8
rustc-wrapper = "sccache"

[target.x86_64-unknown-linux-gnu]
linker = "clang"
rustflags = ["-C", "link-arg=-fuse-ld=mold"]
```

**Note on rustflags conflicts:** Several per-project configs set
`[target.x86_64-unknown-linux-gnu] rustflags` (e.g. `-C target-cpu=x86-64-v3`). Cargo
does NOT merge rustflags — per-project overrides the global entirely. This means those
projects will lose the mold linker flag. Options:

- **Option A (recommended):** Add `-C link-arg=-fuse-ld=mold` to each per-project
  `[target.x86_64-unknown-linux-gnu] rustflags` array alongside their existing flags.
- **Option B:** Use `CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER=clang` env var
  (linker setting is separate from rustflags, so doesn't conflict).
- **Option C:** Move target-cpu flags to `[build] rustflags` in global config and keep
  per-project configs minimal.

Option A is safest — explicit and per-project. The projects that need this are all the
`dfe-*` projects that set `target-cpu=x86-64-v3`.

### 6. Install cargo-sweep for cache hygiene

**What:** Install `cargo-sweep` to periodically clean stale build artifacts.

```bash
cargo install cargo-sweep --locked
```

Usage:
```bash
# Clean artifacts not used in the last 7 days for a specific project
cd /projects/dfe-receiver && cargo sweep --time 7

# Or sweep all projects
for proj in /projects/*/Cargo.toml; do
    (cd "$(dirname "$proj")" && cargo sweep --time 7 2>/dev/null)
done
```

This can be set up as a weekly cron job or run manually when disk gets tight.

---

## Per-project `.cargo/config.toml` changes

For each project that has `jobs = 2` and `[target.x86_64-unknown-linux-gnu] rustflags`,
the changes are:

1. Remove `jobs = 2` (or `jobs = 4` for vrl)
2. Add `-C link-arg=-fuse-ld=mold` to the x86_64 rustflags array

Example (dfe-fetcher):
```toml
# Before:
[target.x86_64-unknown-linux-gnu]
rustflags = ["-C", "target-cpu=x86-64-v3"]

[build]
rustflags = ["-C", "target-cpu=native"]
jobs = 2

# After:
[target.x86_64-unknown-linux-gnu]
rustflags = ["-C", "target-cpu=x86-64-v3", "-C", "link-arg=-fuse-ld=mold"]

[build]
rustflags = ["-C", "target-cpu=native"]
# jobs removed — inherited from ~/.cargo/config.toml (8)
```

---

## Verification

After applying all changes:

1. **Confirm env var is gone:** `env | grep CARGO_TARGET_DIR` should return nothing
   (requires re-login or `source /etc/environment`)
2. **Confirm symlinks:** `ls -la /projects/dfe-receiver/target` should show symlink to
   `/cache/cargo-targets/dfe-receiver/`
3. **Confirm sccache:** `sccache --show-stats` should work
4. **Confirm mold:** `mold --version` should work
5. **Test concurrent builds:** Open two terminals, run `cargo build` in two different
   projects simultaneously — both should proceed without blocking
6. **Test sccache hit:** `cargo clean` in one project, rebuild, check
   `sccache --show-stats` for cache hits
7. **Test CI:** Run a build on an ARC runner to confirm `build.py` still finds
   artifacts correctly (falls back to `target/` when `CARGO_TARGET_DIR` is unset)

---

## Summary

| Change | Benefit | Risk |
|--------|---------|------|
| Per-project target symlinks on `/cache` | Unlocks concurrent builds, stops cross-project invalidation | Must create symlinks for new projects |
| sccache | Cross-project dedup at object level, ~5-10 GB vs 37 GB bloat | Minor: first build is cold |
| mold linker | 5-10x faster linking on incremental builds | Must not use for aarch64 cross-compile |
| jobs 2 -> 8 | Better utilisation of 32 cores | Higher peak CPU, but staggered in practice |
| cargo-sweep | Per-project cache hygiene | None |
