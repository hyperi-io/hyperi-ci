# Writing a PGO Workload for hyperi-ci

This guide teaches you to write a PGO workload script that actually
improves your binary rather than hurting it.

## Invocation contract (important)

hyperi-ci invokes your `workload_cmd` with the **instrumented binary
path as the first positional argument** (`$1`). The env var
`HYPERCI_PGO_INSTRUMENTED_BINARY` is also exported as a convenience,
but `$1` is the canonical contract.

```bash
# Your script starts like this:
#!/usr/bin/env bash
set -euo pipefail
RECEIVER_BIN="$1"
[[ -x "$RECEIVER_BIN" ]] || { echo "usage: $0 <binary>" >&2; exit 1; }
```

So if your `.hyperi-ci.yaml` says:
```yaml
pgo:
  workload_cmd: "bash scripts/pgo-workload.sh"
```
hyperi-ci effectively runs:
```bash
bash scripts/pgo-workload.sh /path/to/target/<triple>/release/<binary>
```

PGO (Profile-Guided Optimisation) records which code paths run hot
during a representative workload, then rebuilds the binary with that
knowledge. A **good** workload gives the compiler a realistic picture
of production and yields 10-20% speedup. A **bad** workload mis-teaches
the compiler and produces measurably slower code.

For the CI contract, see [`rust.md`](rust.md) → *Tier 2 — PGO + BOLT*.
For copy-paste starting points, see `templates/pgo-workload/`.

## The Four Rules

### Rule 1 — Exercise data-processing hot paths, not startup

Your workload MUST drive the code paths that production drives most
often. In practice that means: parse, validate, transform, route,
serialise, send. For a receiver: HTTP POST with realistic bodies. For
a loader: dequeue Kafka messages and run them through to ClickHouse.
For a batch processor: process an input file from start to finish.

**Never profile startup, health checks, or config loading.** Those
paths run once per process lifetime and are irrelevant to steady-state
performance. If the profile is dominated by startup code, the
optimiser happily inlines startup branches into your hot path and
slows production down.

**Example — what NOT to do:**

```bash
# WRONG: this profile is 100% readiness checks
for _ in $(seq 1 1000); do
    curl -sf http://localhost:8080/health/ready
done
```

```bash
# WRONG: one-shot send — the process starts, handles 1 request, ends.
# Startup paths dominate the profile.
curl -X POST http://localhost:8080/ -d '{"test":true}'
```

**Example — what TO do:**

```bash
# RIGHT: sustained realistic load that exercises the request pipeline
oha -z 300s -c 50 -m POST -T application/json \
    -D payload.json http://localhost:8080/
```

### Rule 2 — Realistic traffic mix

Profile what production actually sees. If your service serves 80%
GETs and 20% POSTs, your workload should mirror that. If it sees
a mix of payload sizes, include that mix. If errors are rare in
production, they should be rare in your workload.

PGO will optimise for whatever distribution your profile reflects.
An all-POST workload will bias the compiler toward POST-handling code
and make GETs slower.

**Build a `workload_mix.csv` or similar** that documents the
distribution, and keep it under version control alongside the workload
script so reviewers can sanity-check it.

### Rule 3 — Sustained duration, minimum 60s

60 seconds is a hard floor — shorter workloads leave the compiler with
noisy, startup-dominated data. 300s is the recommended default. Longer
helps marginally but with diminishing returns — past 10 minutes the
profile stops changing.

hyperi-ci enforces the 60s floor: workloads shorter than that fail
the build with an error.

### Rule 4 — Deterministic and self-contained

The workload runs in CI, often on a fresh runner. Anything external
(remote API, live Kafka cluster, stale DB state) that could fail means
your release build fails for a transient network reason.

**Use testcontainers**, `docker run` for dependencies, or synthetic
local data. If your workload depends on Kafka, spin up Kafka inside
the workload script. If it depends on a database, run a container.
Clean everything up on EXIT trap.

## Profile quality metrics

hyperi-ci validates profile data after your workload runs. If any of
these fail, the build errors out:

| Check | Threshold | Rationale |
|---|---|---|
| Workload duration | ≥ 60s | Shorter = biased toward startup |
| `.profraw` total size | ≥ 1 MB (default) | Too little = workload didn't hit hot path |
| cargo-pgo merge succeeds | yes | Corrupt profile = abort |

Threshold is configurable via
`build.rust.optimize.pgo.min_profile_bytes`. Raise it if your workload
generates lots of profile data (more coverage = more confidence).

## Anti-patterns

| Anti-pattern | Why it hurts |
|---|---|
| `curl /healthz` in a loop | Profiles health-check code, not hot path |
| Single-request workload (`curl -X POST ...`) | Startup dominates the profile |
| Workload that connects to `prod.example.com` | Non-deterministic, network failure = build failure |
| Hardcoded paths `/home/me/data.json` | Doesn't work on CI runners |
| Workload uses the same payload every request | Branch predictor will memorise one case only |
| Randomised payloads with no size distribution | Profile doesn't match production allocation pattern |
| Skipping Kafka/DB by using an in-memory mock | Skips the hot allocations those drivers do in production |
| Running for 30s | Too short — floor is 60s, recommended 300s |

## Workload shapes

The following patterns cover most DFE Rust binaries. Copy the matching
template from `templates/pgo-workload/` and customise.

### HTTP server (receiver-style)

One or more HTTP listeners accepting POSTed payloads. Drive with
`oha`, `vegeta`, or a custom Rust client. Mix payload sizes (small
event, medium structured, large batch). Include any auth headers
production requires.

Template: `http-server.sh`

### gRPC server

Drive with `grpcurl`, a native gRPC client, or a Rust binary that links
the service's tonic-generated client. Mix request shapes (unary,
server-streaming, bidi-streaming if the service supports them).

Template: `grpc-server.sh`

### Kafka producer (data shipping to Kafka)

Drive producer-side by sending HTTP/gRPC requests that trigger Kafka
produce calls. Must have a real Kafka broker — testcontainers works well.
Include batching behaviour (multiple messages in tight succession).

Template: `kafka-producer.sh`

### Kafka consumer (loader-style)

Drive by producing messages TO the Kafka topic your binary consumes.
Your binary then drains them and does downstream work (insert to
ClickHouse, forward to another service, etc.). Profile captures the
full consume → process → sink path.

Template: `kafka-consumer.sh`

### Multi-protocol (receiver-style with many listeners)

For binaries that accept multiple protocols (HTTP, gRPC, syslog,
OTLP, Prom RW, etc.), drive each protocol proportionally to production
mix. A dedicated Rust driver binary linked to the main project is
often the cleanest approach (so you can reuse the project's protobuf
types, TLS config, etc.).

Template: `multi-protocol.sh` (reference: dfe-receiver's
`src/bin/pgo-driver.rs` + `scripts/pgo-workload.sh`)

## Choosing a workload command

Common tools and their tradeoffs:

| Tool | Best for | Limitations |
|---|---|---|
| `oha` | HTTP load | No gRPC, limited payload shaping |
| `vegeta` | HTTP load with complex scripts | More setup than oha |
| `wrk` / `wrk2` | High-RPS HTTP | No gRPC, lua-scripted |
| `grpcurl` | Ad-hoc gRPC | Slow for sustained load |
| `ghz` | Sustained gRPC load | Less common tooling |
| Custom Rust bin | Any protocol, reuses project types | Requires implementation |

For DFE projects with unusual protocols (OTLP, Lumberjack, Fluent
Forward), a custom Rust driver almost always wins because you can
reuse the project's proto types and TLS config.

## Validating your workload locally

Before pushing a `.hyperi-ci.yaml` opt-in, test the pipeline locally:

```bash
cargo install cargo-pgo
rustup component add llvm-tools-preview

# 1. Instrument
cargo pgo build -- --features jemalloc

# 2. Run your workload against the instrumented binary
bash scripts/pgo-workload.sh ./target/x86_64-unknown-linux-gnu/release/<your-binary>

# 3. Inspect profile quality
ls -la target/pgo-profiles/*.profraw
# Should be at least 1 MB total

# 4. Merge profiles
llvm-profdata merge -o target/pgo-profiles/merged.profdata target/pgo-profiles/*.profraw

# 5. Show top functions by coverage
llvm-profdata show --topn=20 target/pgo-profiles/merged.profdata
# Expected: your request-handler and parsing functions at the top.
# Red flag: your startup / config-loading functions at the top.

# 6. Build optimised
cargo pgo optimize build -- --features jemalloc
```

If step 5 shows startup code at the top, your workload needs more
sustained traffic or needs to start driving load only AFTER the
binary is fully ready.

## Reference implementation

`dfe-receiver` is the first shipping DFE binary with Tier 2. Its
workload implementation is a template for multi-protocol services:

- `scripts/pgo-workload.sh` — orchestrator (Kafka container + binary
  lifecycle + cleanup trap)
- `src/bin/pgo-driver.rs` — feature-gated Rust binary that drives HTTP,
  Prometheus Remote Write (snappy+protobuf), Splunk HEC, OTLP HTTP
  (protobuf), and Syslog UDP/TCP at configurable rates

Read both files together for a working pattern. The driver shows how
to reuse the project's own protobuf types (Prom RW, OTLP) without
duplicating schemas.
