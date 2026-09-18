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

For BOLT, read the note llvm-bolt writes into every binary it rewrites.
`strip` keeps it, so it is there in the file that shipped:

```bash
readelf -p .note.bolt_info ./<binary>
# Expect: BOLT revision: <...>
```

No note means the shipped file is not BOLT output, whatever the log says.
hyperi-ci runs the same check on the packaged file and fails the build when an
arch it reported as BOLT-optimised carries no note.

For `-C target-cpu=x86-64-v3` on amd64, compare VEX-encoded SSE against legacy
SSE. With AVX enabled the compiler encodes ordinary SSE as VEX, so the ratio
inverts wholesale - and a ratio inside one binary needs no size-matched control:

```bash
objdump -d --no-show-raw-insn -M intel ./<binary> | grep -cE '\sv[a-z0-9]+\s+xmm'
objdump -d --no-show-raw-insn -M intel ./<binary> | grep -cE '\s(movups|movaps|movdqu|movdqa|pxor)\s+xmm'
```

VEX several times legacy means v3; legacy larger means baseline. One program
built both ways (dfe-transform-vector v1.0.41, each arm forced through
`RUSTFLAGS`): baseline 20,617 VEX against 108,947 legacy, v3 75,168 against
17,471.

Back it with mnemonics a compiler emits and hand-written asm does not - `blsr`
and `bzhi` are the sharpest, zero in a baseline build:

```bash
objdump -d --no-show-raw-insn -M intel ./<binary> | grep -cE '\s(shlx|sarx|shrx|bzhi|blsr|andn)\s'
```

Do NOT count BMI2 as a whole. `mulx`, `adcx`, `adox` and `rorx` arrive with
dependency asm: aws-lc's bignum code gives dfe-loader and dfe-fetcher the same
4,210 `adcx` to the instruction, one built v3 and one not. `ymm` proves nothing
either - memchr and friends dispatch AVX2 at run time.

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
BOLT: <bin>-bolt-optimized installed as <bin> for packaging
optimised: pgo=yes bolt=yes allocator=jemalloc
BOLT verified in <bin>-linux-<arch> (.note.bolt_info)
```

The last two BOLT lines are the ones that matter. cargo-pgo leaves its result
beside the cargo output rather than in place of it, so "BOLT optimized build
finished successfully" from the tool says nothing about the file that ships.

Your workload's own output appears between the two PGO lines, prefixed
`pgo-workload:`.

cargo-pgo prints its own progress alongside ours -- lines such as
`PGO instrumentation build finished successfully` and `Found 1 PGO profile
file with total size X.XX MiB`. Those are useful to read but they belong to
the tool, not to us, so do not treat one as a required marker: cargo-pgo is
free to reword them.

If one of OUR lines is missing, a tier wasn't applied. See
[rust-troubleshooting.md](rust-troubleshooting.md).

The `optimised:` line closes each arch's build group and is the one to read
first; the `BOLT verified` lines follow in the packaging group.
Every Tier 2 skip is warn-only, so a green run proves nothing by itself - a
`bolt=no` or `allocator=system` there says that arch shipped unoptimised, and
a warn line earlier in the same group names the reason.

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
