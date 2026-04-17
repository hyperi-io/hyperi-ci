#!/usr/bin/env bash
# TEMPLATE: PGO workload for a gRPC server (generic).
#
# Copy to scripts/pgo-workload.sh, customise placeholders marked `# TODO:`.
# See docs/PGO-WORKLOAD-GUIDE.md for the four rules.
#
# Usage: pgo-workload.sh <instrumented-binary-path>

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <path-to-instrumented-binary>" >&2
    exit 1
fi
BINARY="$1"
[[ -x "$BINARY" ]] || { echo "error: $BINARY not executable" >&2; exit 1; }

DURATION="${PGO_WORKLOAD_DURATION_SECS:-300}"

# TODO: select a gRPC load driver. `ghz` is the common choice for
# sustained load. Fallback to grpcurl in a loop if ghz not available.
command -v ghz >/dev/null 2>&1 || {
    echo "error: ghz not found. Install:" >&2
    echo "  go install github.com/bojand/ghz/cmd/ghz@latest" >&2
    echo "  or wrap grpcurl in a sustained loop" >&2
    exit 1
}

# TODO: point at your .proto file(s) for reflection-free mode
PROTO_FILE="${PGO_WORKLOAD_PROTO:-proto/service.proto}"
PROTO_IMPORT_PATHS="${PGO_WORKLOAD_PROTO_PATHS:-proto}"

# TODO: the fully-qualified RPC name
RPC_METHOD="${PGO_WORKLOAD_RPC:-my.package.Service/Method}"

BIND_ADDR="127.0.0.1:6000"
WORKDIR=$(mktemp -d -t pgo-workload-XXXXXX)

# TODO: config for your binary
cat > "$WORKDIR/config.yaml" <<YAML
grpc:
  enabled: true
  bind_address: "$BIND_ADDR"
  auth:
    mode: "none"
YAML

# TODO: a representative request body in JSON (ghz converts to proto)
cat > "$WORKDIR/request.json" <<'JSON'
{"field1": "value", "field2": 42}
JSON

BINARY_PID=""
cleanup() {
    local rc=$?
    [[ -n "$BINARY_PID" ]] && kill -TERM "$BINARY_PID" 2>/dev/null || true
    sleep 1
    [[ -n "$BINARY_PID" ]] && kill -KILL "$BINARY_PID" 2>/dev/null || true
    rm -rf "$WORKDIR"
    exit $rc
}
trap cleanup EXIT INT TERM

echo "pgo-workload: starting $BINARY"
"$BINARY" --config "$WORKDIR/config.yaml" >"$WORKDIR/server.log" 2>&1 &
BINARY_PID=$!

# TCP readiness poll (gRPC servers rarely expose /healthz unless the app
# adds it explicitly; TCP accept is a good enough ready signal)
for i in $(seq 1 60); do
    kill -0 "$BINARY_PID" 2>/dev/null || { echo "error: server died" >&2; exit 1; }
    if (echo > /dev/tcp/127.0.0.1/6000) 2>/dev/null; then
        break
    fi
    [[ $i -eq 60 ]] && { echo "error: not ready in 60s" >&2; exit 1; }
    sleep 1
done

echo "pgo-workload: driving gRPC load for ${DURATION}s"

# ghz runs for a fixed duration with rate limiting
ghz --insecure \
    --proto "$PROTO_FILE" \
    --import-paths "$PROTO_IMPORT_PATHS" \
    --call "$RPC_METHOD" \
    -D "$WORKDIR/request.json" \
    --duration "${DURATION}s" \
    --concurrency 50 \
    --rps 200 \
    "$BIND_ADDR" \
    || echo "warn: ghz reported errors" >&2

echo "pgo-workload: done"
