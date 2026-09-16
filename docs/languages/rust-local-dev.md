# Rust local development

Concurrent multi-project Rust development on a shared machine: per-project target directories, sccache, mold, and how many cores to hand each build.

Symptoms and fixes for this setup are in [rust-troubleshooting.md](rust-troubleshooting.md) under *Local developer*. The CI-side build pipeline is [rust.md](rust.md).

---

## Local developer hygiene

Not strictly CI, but relevant: concurrent multi-project Rust development
on a shared machine. This section is an **example setup** from
`desktop-derek` - adapt to your machine's specifics.

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
 - sccache caches the compiled objects themselves, independent of target
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

Linking is often the bottleneck in incremental builds. mold is 5-10x
faster than `ld` and 2-3x faster than `lld`.

```bash
sudo apt install mold
```

```toml
# ~/.cargo/config.toml
[target.x86_64-unknown-linux-gnu]
linker = "clang"
rustflags = ["-C", "link-arg=-fuse-ld=mold"]
```

**Native x86_64 only.** Cross-compilation to aarch64 must use BFD -
hyperi-ci's `build.py` already enforces this via linker wrapper scripts
that pass `-fuse-ld=bfd`. Don't touch the cross-compile linker config.

**Per-project rustflags override this** - Cargo does NOT merge rustflags
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
