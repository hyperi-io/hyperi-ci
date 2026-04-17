#!/usr/bin/env bash
# TEMPLATE: Multi-protocol PGO workload (receiver-style — many listeners).
#
# For services that accept multiple ingress protocols (HTTP, gRPC,
# OTLP, syslog, Prom RW, Splunk HEC, Lumberjack, Fluent, GELF, ...).
#
# Pattern: a dedicated Rust driver binary that links your project's lib
# (to reuse proto types + config) drives each protocol at a configurable
# rate. This shell script just orchestrates the lifecycle.
#
# Reference implementation: dfe-receiver
#   scripts/pgo-workload.sh        (this file's model)
#   src/bin/pgo-driver.rs          (the Rust driver)
#   Cargo.toml                     (pgo-driver feature + [[bin]] entry)
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
if [[ "$DURATION" -lt 60 ]]; then
    echo "error: duration must be >= 60s (short workloads produce bad profiles)" >&2
    exit 1
fi

# -------- Locate the PGO driver -------------------------------------------
#
# Build separately before invoking this script:
#   cargo build --release --features pgo-driver --bin pgo-driver
#
# TODO: adjust the search path if your driver has a different name.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PGO_DRIVER_PATH="${PGO_DRIVER_PATH:-}"
if [[ -z "$PGO_DRIVER_PATH" ]]; then
    for c in \
        "$PROJECT_ROOT/target/release/pgo-driver" \
        "$PROJECT_ROOT/target/debug/pgo-driver"; do
        [[ -x "$c" ]] && { PGO_DRIVER_PATH="$c"; break; }
    done
fi
[[ -x "$PGO_DRIVER_PATH" ]] || {
    echo "error: pgo-driver not found. Build with:" >&2
    echo "  cargo build --release --features pgo-driver --bin pgo-driver" >&2
    exit 1
}

# -------- Cleanup ---------------------------------------------------------

BINARY_PID=""
KAFKA_CID=""
WORKDIR=""

cleanup() {
    local rc=$?
    [[ -n "$BINARY_PID" ]] && kill -TERM "$BINARY_PID" 2>/dev/null || true
    sleep 2
    [[ -n "$BINARY_PID" ]] && kill -KILL "$BINARY_PID" 2>/dev/null || true
    [[ -n "$KAFKA_CID" ]] && docker rm -f "$KAFKA_CID" >/dev/null 2>&1 || true
    [[ -n "$WORKDIR" && -d "$WORKDIR" ]] && rm -rf "$WORKDIR"
    exit $rc
}
trap cleanup EXIT INT TERM

# -------- Kafka container (most multi-protocol receivers produce here) ----

KAFKA_IMAGE="${PGO_WORKLOAD_KAFKA_IMAGE:-apache/kafka:3.8.0}"
KAFKA_CID=$(docker run -d --rm \
    -p 19092:9092 \
    -e KAFKA_NODE_ID=1 \
    -e KAFKA_PROCESS_ROLES=broker,controller \
    -e KAFKA_LISTENERS='PLAINTEXT://0.0.0.0:9092,CONTROLLER://0.0.0.0:9093' \
    -e KAFKA_ADVERTISED_LISTENERS='PLAINTEXT://localhost:19092' \
    -e KAFKA_LISTENER_SECURITY_PROTOCOL_MAP='CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT' \
    -e KAFKA_CONTROLLER_QUORUM_VOTERS='1@localhost:9093' \
    -e KAFKA_CONTROLLER_LISTENER_NAMES=CONTROLLER \
    -e KAFKA_INTER_BROKER_LISTENER_NAME=PLAINTEXT \
    -e KAFKA_AUTO_CREATE_TOPICS_ENABLE=true \
    -e CLUSTER_ID="$(printf '%s' "pgo$(date +%s)$$" | base64 | head -c 22)" \
    "$KAFKA_IMAGE")

for i in $(seq 1 30); do
    # Readiness: the broker advertises localhost:<host-port>, which isn't
    # routable inside the container. Check the host-mapped port instead.
    if (echo > /dev/tcp/127.0.0.1/19092) 2>/dev/null; then
        sleep 2  # give the broker a beat to finish RAFT bootstrap
        break
    fi
    [[ $i -eq 30 ]] && { echo "error: Kafka not ready" >&2; exit 1; }
    sleep 2
done

# -------- All-listeners config --------------------------------------------

WORKDIR=$(mktemp -d -t pgo-workload-XXXXXX)

# TODO: enable every listener your project supports, route to the
# Kafka container. Below is the dfe-receiver pattern; adjust port
# numbers, schema names, and protocol enablement to match your app.
cat > "$WORKDIR/config.yaml" <<YAML
server:
  bind_address: "127.0.0.1:8080"
  auth: { mode: "none" }
# TODO: enable each protocol your driver will exercise
# grpc: { enabled: true, bind_address: "127.0.0.1:6000" }
# otlp: { enabled: true, grpc_bind_address: "127.0.0.1:4317", http_bind_address: "127.0.0.1:4318" }
# prometheus_rw: { enabled: true, bind_address: "127.0.0.1:9091" }
# splunk_hec: { enabled: true, bind_address: "127.0.0.1:8088" }
# syslog: { enabled: true, udp_bind_address: "127.0.0.1:514", tcp_bind_address: "127.0.0.1:514" }
kafka:
  brokers: ["localhost:19092"]
destinations:
  default: "kafka"
routing:
  default_source: "pgo"
  topic_suffix: "_land"
YAML

# -------- Start binary ----------------------------------------------------

"$BINARY" --config "$WORKDIR/config.yaml" >"$WORKDIR/server.log" 2>&1 &
BINARY_PID=$!

# Wait for readiness
for i in $(seq 1 60); do
    kill -0 "$BINARY_PID" 2>/dev/null || { echo "error: binary died" >&2; exit 1; }
    if curl -sf -o /dev/null --max-time 1 "http://127.0.0.1:8080/health/ready"; then
        break
    fi
    [[ $i -eq 60 ]] && { echo "error: binary not ready" >&2; exit 1; }
    sleep 1
done
sleep 2   # let Kafka client join

# -------- Drive all protocols concurrently --------------------------------

echo "pgo-workload: driving multi-protocol load for ${DURATION}s"

# The driver binary reads PGO_DRIVER_* env vars — see the
# dfe-receiver reference for the full list. At minimum set duration.
PGO_DRIVER_DURATION_SECS="$DURATION" \
    "$PGO_DRIVER_PATH"

# Give Kafka producer flush a moment to complete so the profile
# captures flush code paths too.
sleep 3

echo "pgo-workload: done"
