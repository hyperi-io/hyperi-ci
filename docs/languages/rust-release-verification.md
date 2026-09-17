# Rust release dispatch and verification

What a Tier 2 release dispatch does minute by minute, and how to confirm on the binary and in the log that each tier actually applied.

The tiers themselves and the `.hyperi-ci.yaml` keys are [rust.md](rust.md).

---

## Release dispatch flow

```
0:00  Setup: runners claimed (arc-runner-16cpu on amd64, ubuntu-24.04-arm on arm64)
0:30  Native deps install — bolt-23, binutils via apt.llvm.org
1:00  cargo install cargo-pgo --locked
2:00  Cargo build deps (cached after first run)
2:00  hyperi-ci: "Rust build optimisation: channel=release, allocator=jemalloc, lto=fat, pgo=on, bolt=on"
2:00  PGO: building instrumented binary
7:00  Instrumented build complete
7:00  Workload: Kafka container up, pgo-driver compiled, traffic driven for 300s
12:00 Workload complete, profile data: ~5 MiB collected
12:00 PGO: building optimised binary (fresh compile with profile data)
16:00 PGO-optimised build complete
16:00 llvm-bolt + merge-fdata + ld.lld shim: ~/.local/bin/* -> /usr/bin/*-23
16:00 BOLT: building instrumented binary
18:00 BOLT: applying profile, emitting final binary
18:00 Artifact upload
```

Both archs run concurrently. Release pipeline critical path ~ 20 min
from dispatch to published artifacts.

---

## Verification

### On the shipped binary

Binary is stripped - `nm` won't show symbols. Use `strings`:

```bash
strings /path/to/binary | grep -iE 'jemalloc|je_mallctl' | head
# Expect: jemalloc_bg_thd, jemalloc, <jemalloc>: %s: %.*s:%.*s
```

For BOLT - strip removes section markers, so the CI build log is the
authoritative source (next section). If you need binary-level proof,
build with `strip = false` locally (`cargo pgo bolt optimize` on your
machine), then:

```bash
llvm-readelf --sections ./<binary> | grep -E '\.bolt|\.text.hot'
```

### In the CI build log

Grep the Build job log for these. hyperi-ci emits each one itself, so a
missing line here does mean the stage did not run:

```
Rust build optimisation: channel=release, allocator=jemalloc, lto=fat, pgo=on, bolt=on
PGO: building instrumented binary for <triple>
PGO: building optimised binary for <triple>
llvm-bolt shim: ~/.local/bin/llvm-bolt -> /usr/bin/llvm-bolt-23
merge-fdata shim: ~/.local/bin/merge-fdata -> /usr/bin/merge-fdata-23
ld.lld shim: ~/.local/bin/ld.lld -> /usr/bin/ld.lld-23
BOLT: building instrumented binary for <triple> (linker forced to lld)
BOLT: optimising binary for <triple> (using PGO + BOLT profiles, linker=lld)
```

Your workload's own output appears between the two PGO lines, prefixed
`pgo-workload:`.

cargo-pgo prints its own progress alongside ours -- lines such as
`PGO instrumentation build finished successfully` and `Found 1 PGO profile
file with total size X.XX MiB`. Those are useful to read but they belong to
the tool, not to us, so do not treat one as a required marker: cargo-pgo is
free to reword them.

If one of OUR lines is missing, a tier wasn't applied. See
[rust-troubleshooting.md](rust-troubleshooting.md).

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
Don't optimise this line - it's trivial.
