#!/usr/bin/env bash
# TEMPLATE: PGO workload for an HTTP server (generic).
#
# Copy to scripts/pgo-workload.sh, customise placeholders marked `# TODO:`,
# then opt in via .hyperi-ci.yaml. See docs/PGO-WORKLOAD-GUIDE.md.
#
# Usage: pgo-workload.sh <instrumented-binary-path>
#
# Env overrides (optional):
#   PGO_WORKLOAD_DURATION_SECS  (default 300; hyperi-ci enforces >= 60)
#   PGO_WORKLOAD_RPS            (default 200)
#   PGO_WORKLOAD_CONCURRENCY    (default 50)

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <path-to-instrumented-binary>" >&2
    exit 1
fi
BINARY="$1"
[[ -x "$BINARY" ]] || { echo "error: $BINARY not executable" >&2; exit 1; }

DURATION="${PGO_WORKLOAD_DURATION_SECS:-300}"
RPS="${PGO_WORKLOAD_RPS:-200}"
CONCURRENCY="${PGO_WORKLOAD_CONCURRENCY:-50}"

# TODO: choose a bind address + port for your server
BIND_ADDR="127.0.0.1:8080"

# TODO: choose a load generator. `oha` is lightweight and available on PATH.
#   cargo install oha  (if not installed — takes ~30s)
command -v oha >/dev/null 2>&1 || {
    echo "error: oha not found; install with: cargo install oha" >&2
    exit 1
}

# Workload directory for temp files (config, logs, payloads)
WORKDIR=$(mktemp -d -t pgo-workload-XXXXXX)

# TODO: if your binary needs a config file, write one here. Keep it
# minimal — enable only the listeners that will be driven.
# Example:
cat > "$WORKDIR/config.yaml" <<YAML
# TODO: replace with your project's config schema
server:
  bind_address: "$BIND_ADDR"
  auth:
    mode: "none"
YAML

# TODO: include a representative request body (or multiple sizes).
cat > "$WORKDIR/payload.json" <<'JSON'
{
  "timestamp": "2026-04-17T12:34:56Z",
  "level": "info",
  "service": "app",
  "message": "representative payload",
  "fields": {"user_id": 42, "action": "login"}
}
JSON

# -------- Cleanup ---------------------------------------------------------

BINARY_PID=""
cleanup() {
    local rc=$?
    if [[ -n "$BINARY_PID" ]] && kill -0 "$BINARY_PID" 2>/dev/null; then
        kill -TERM "$BINARY_PID" 2>/dev/null || true
        for _ in 1 2 3 4 5; do
            kill -0 "$BINARY_PID" 2>/dev/null || break
            sleep 1
        done
        kill -KILL "$BINARY_PID" 2>/dev/null || true
    fi
    rm -rf "$WORKDIR"
    exit $rc
}
trap cleanup EXIT INT TERM

# -------- Start server ----------------------------------------------------

echo "pgo-workload: starting binary: $BINARY"
# TODO: adjust flags to match your binary's CLI
"$BINARY" --config "$WORKDIR/config.yaml" >"$WORKDIR/server.log" 2>&1 &
BINARY_PID=$!

# Wait for readiness
# TODO: replace /healthz with your binary's readiness endpoint
for i in $(seq 1 60); do
    kill -0 "$BINARY_PID" 2>/dev/null || {
        echo "error: server died during startup" >&2
        tail -50 "$WORKDIR/server.log" >&2
        exit 1
    }
    if curl -sf -o /dev/null --max-time 1 "http://$BIND_ADDR/healthz"; then
        break
    fi
    [[ $i -eq 60 ]] && { echo "error: server not ready in 60s" >&2; exit 1; }
    sleep 1
done

# -------- Drive load ------------------------------------------------------

echo "pgo-workload: driving ${RPS} rps for ${DURATION}s @ concurrency=${CONCURRENCY}"

oha -z "${DURATION}s" \
    -q "$RPS" \
    -c "$CONCURRENCY" \
    -m POST \
    -T "application/json" \
    -d "@$WORKDIR/payload.json" \
    "http://$BIND_ADDR/" \
    || { echo "warn: load generator reported errors (continuing)" >&2; }

echo "pgo-workload: done"
