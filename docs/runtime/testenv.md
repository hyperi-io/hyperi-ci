# CI Message Broker: Kafka -> Redpanda

> The canonical docker patterns are copyable references in `templates/testenv/`
> (see [SSoT - reference patterns](#ssot---reference-patterns-not-a-dependency)).

Kafka is core to most of DFE, so its CI test broker matters everywhere. This page covers why CI uses **Redpanda** (not Apache Kafka), the exact setup, and the gotchas, so projects stop re-solving them independently. The setup was proven on dfe-archiver, dfe-transform-vector and dfe-transform-vrl.

## Why Redpanda, not Apache Kafka, in CI

The **hard deck for every CI runner job is 4GB** (the default GitHub-hosted
OSS-runner size, see [the 4GB envelope](#the-4gb-envelope)).

- **Apache Kafka** runs on the JVM: a 1.5-2.5GB heap. On a 4GB runner that
  starves the workload-under-test (a PGO-instrumented binary + load driver),
  causing OOM kills.
- **Redpanda** is a C++/Seastar reimplementation of the Kafka wire protocol.
  In `dev-container` mode with `--memory 512M` it fits comfortably, leaving
  headroom for the build + binary + driver inside 4GB.

Same Kafka wire protocol -> **application code and the broker endpoint are
unchanged**; only the CI fixture differs.

## Canonical Redpanda CI setup

```bash
# Start: dev-container mode, single core, hard 512M cap, advertised on host.
# TAG: take it from templates/testenv/redpanda.compose.yaml, which pins it once.
docker run -d --rm \
    -p 19092:9092 \
    docker.redpanda.com/redpandadata/redpanda:<tag> \
    redpanda start \
        --mode dev-container \
        --smp 1 \
        --memory 512M \
        --kafka-addr PLAINTEXT://0.0.0.0:9092 \
        --advertise-kafka-addr PLAINTEXT://localhost:19092
```

`--mode dev-container` bundles `--overprovisioned`, `--reserve-memory 0M`,
`--check=false`, `--unsafe-bypass-fsync` - i.e. throughput-irrelevant safety
checks off, minimal reserved memory. The explicit `--memory 512M` is the
budget cap.

**Readiness** - gate on the admin API, never a bare TCP-open probe (the port
opens before the broker serves):

```bash
docker exec "$CID" rpk cluster health | grep -q "Healthy:.*true"
```

## Gotchas (the expensive-to-learn bits)

### 1. The image entrypoint auto-prepends `rpk`

`docker run <redpanda-image> topic create events ...` actually execs
`/usr/bin/rpk topic create events ...` - the entrypoint prepends `rpk` for any
non-`redpanda` first arg. So:

- Right: `<image> topic create events ...` -> runs `rpk topic create ...`
- Wrong: `<image> rpk topic create events ...` -> runs `rpk rpk topic create ...`

Don't add `rpk` yourself for the one-shot client form. (The in-container
`docker exec <cid> rpk ...` form *does* need `rpk` - different invocation.)

### 2. No topic auto-create on consumer SUBSCRIBE

Redpanda auto-creates a topic on **produce**, but **not** when a consumer
subscribes. Apache Kafka auto-created on subscribe, which masked an ordering bug. A consumer-first service (e.g. dfe-archiver) subscribes to a topic that doesn't exist yet and never reaches ready. The load driver starts only after the service is ready, so it never produces: deadlock.

**Fix: pre-create topics** before starting the consumer:

```bash
# --network host so the client reaches the *advertised* localhost:19092
# (an in-container client follows the advertised address, which only
# resolves on the host).
docker run --rm --network host <image> topic create events -p 3 -X brokers=localhost:19092
```

Pre-create is mandatory for any consumer-first workload. Topic names are
project-specific; everything else is generic.

### 3. Advertised-address resolution

A one-shot admin client must reach the **advertised** listener
(`localhost:19092`). Run it `--network host` so `localhost` resolves to the
runner, not the client container.

## The 4GB envelope

Every CI job - **including integration tests** - must run within **4GB** and
**must not require any external service** (no remote ClickHouse, no remote
Kafka). Remote services are a *local-dev* speed convenience only (Docker is too
slow for fast local iteration); CI is always self-contained.

CI **may** use larger runners (the LARGE ARC runners cost nothing extra on the DevEx cluster) as an opportunistic speedup. Nothing in CI may *require* more than 4GB or any external dependency, so design for the 4GB free-runner floor.

Rough budget on a 4GB runner during a PGO workload:

| Component | Budget |
|---|---|
| Redpanda (`--memory 512M`) | ~0.5 GB |
| PGO-instrumented binary + buffers | ~1-1.5 GB |
| Load driver + OS + docker | ~1 GB |
| Headroom | remainder |

## Current state (the duplication problem)

The setup above is **copy-pasted** into each canary project's
`scripts/pgo-workload.sh` <!-- doc-paths: ignore --> (dfe-archiver, dfe-transform-vector, dfe-transform-vrl). Each copy carries the same Redpanda fixes (topic pre-create, readiness, entrypoint form). The next section replaces that duplication.

```mermaid
flowchart TB
  subgraph today["Duplicated per project"]
    A1["dfe-archiver/scripts/pgo-workload.sh<br/>(redpanda setup)"]
    A2["dfe-transform-vector/...pgo-workload.sh<br/>(redpanda setup)"]
    A3["dfe-transform-vrl/...pgo-workload.sh<br/>(redpanda setup)"]
  end
```

## SSoT - reference patterns, not a dependency

hyperi-ci is the single place that gets the docker tuning right, as **copyable
reference patterns** - `templates/testenv/`:

```mermaid
flowchart TB
  SSOT["templates/testenv/ (SSoT reference)<br/>redpanda.compose.yaml, clickhouse.compose.yaml<br/>+ clickhouse-low-mem.xml, 4GB-tuned"]
  SSOT -.->|copy snippet| C1["dfe-archiver"]
  SSOT -.->|copy snippet| C2["dfe-transform-vector"]
  SSOT -.->|copy snippet| C3["dev laptop / any project"]
```

- **Reference, not a dependency.** Projects copy the service block into their
  own `docker-compose.dev.yaml` - no `hyperi-ci testenv` command, no
  auto-provisioning, nothing mandatory. Deliberately not over-engineered.
- Generic, 4GB-tuned infra (image, caps, readiness) lives once here; per-project
  data (Redpanda topics, ClickHouse schema from `dfe-schemas`) stays the
  project's own.
- **Redpanda:** `--memory 512M`, dev-container - the campaign-proven setup.
- **ClickHouse:** `mem_limit: 2g` (reliable single-node floor) + the
  `clickhouse-low-mem.xml` profile. Defaults assume 16GB+; 2g ingests + queries
  fine but slower. Local dev keeps a remote-CH escape hatch for speed; CI stays
  self-contained.
