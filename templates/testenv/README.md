# testenv — reference docker patterns

Canonical, 4GB-tuned docker patterns for the two services most DFE projects
need in integration tests: **Redpanda** (Kafka wire protocol) and
**ClickHouse**. They live here so there's one place that gets the tuning right
and devs know it exists.

**These are a reference, not a dependency.** Copy the bits you need into your
project's `docker-compose.dev.yaml`; hyperi-ci does not run, manage, or require
them. Nothing here is mandatory.

## Use

- Need a broker → copy the `redpanda` service from `redpanda.compose.yaml`.
- Need ClickHouse → copy the `clickhouse` service from `clickhouse.compose.yaml`
  **and** the sibling `clickhouse-low-mem.xml` (the service mounts it).
- Per-project data is yours: pre-create Redpanda topics, load your ClickHouse
  schema (e.g. from dfe-schemas) after the healthcheck passes.

## Why the tuning

Every CI job has a **4GB hard deck** (free GitHub OSS runners) and may not
require any external service. So:

| Service | Cap | Notes |
|---|---|---|
| Redpanda | `--memory 512M` | dev-container, single core. Fits comfortably. |
| ClickHouse | `mem_limit: 2g` | Reliable single-node floor. Defaults assume 16GB+; 2g ingests + queries fine (slower). Below ~1.5g flaky; <1g needs swap. |

Running both + your app is ~2.5GB of services on a 4GB runner — snug. That's
why this is opt-in/copy: take only what a given test needs.

Background + gotchas (Redpanda readiness, topic pre-create, why not Apache
Kafka): `docs/runtime/testenv.md`.
