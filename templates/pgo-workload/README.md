# PGO Workload Templates

Copy-paste starting points for writing a `pgo-workload.sh` script for
your project. Each template is a **skeleton** — customise the traffic
mix, payload shapes, and duration to match your production profile.

See `docs/PGO-WORKLOAD-GUIDE.md` for the four rules every workload must
follow.

## Quick pick

| Your project looks like… | Start from |
|---|---|
| Single-protocol HTTP server | `http-server.sh` |
| Single-protocol gRPC server | `grpc-server.sh` |
| Service that **writes** to Kafka (receiver/proxy) | `kafka-producer.sh` |
| Service that **reads** from Kafka (loader/worker) | `kafka-consumer.sh` |
| Many protocols on many listeners (receiver with 9+ ingest paths) | `multi-protocol.sh` |

## How to use a template

1. Copy to `scripts/pgo-workload.sh` in your project.
2. Make it executable: `chmod +x scripts/pgo-workload.sh`.
3. Customise the placeholders (marked `# TODO:`):
   - Binary path handling (how to locate your app's binary)
   - Config generation (what your app needs to boot)
   - Load-driver invocation (tool + args)
   - Cleanup (any extra containers/processes)
4. Run locally to validate (see PGO-WORKLOAD-GUIDE.md).
5. Opt in via `.hyperi-ci.yaml`:

   ```yaml
   build:
     rust:
       optimize:
         pgo:
           enabled: true
           workload_cmd: "bash scripts/pgo-workload.sh"
           duration_secs: 300
         bolt:
           enabled: true
   ```

## Common requirements

All templates assume:

- `$1` is the path to the instrumented binary (passed by cargo-pgo).
- Docker is available (for testcontainers-style dependencies).
- The workload script is `set -euo pipefail` and traps EXIT for cleanup.
- Minimum duration 60s (hyperi-ci enforces this).

## Required tools

Templates list prerequisites at the top. Install any missing tools in
your CI runner image or at the top of the script with install guards:

```bash
command -v oha >/dev/null 2>&1 || cargo install oha
```

Be conservative with auto-install — if CI runs in a sandbox without
internet, `cargo install` will fail. Prefer pre-installed tools where
possible.

## Reference implementations

- `dfe-receiver` → `multi-protocol.sh` pattern (see that project's
  `scripts/pgo-workload.sh` + `src/bin/pgo-driver.rs`)
