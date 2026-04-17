#!/usr/bin/env bash
# TEMPLATE: PGO workload for a service that PRODUCES to Kafka
# (receiver-style: accepts HTTP/gRPC ingress, forwards to Kafka).
#
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
KAFKA_IMAGE="${PGO_WORKLOAD_KAFKA_IMAGE:-apache/kafka:3.8.0}"

command -v docker >/dev/null 2>&1 || { echo "error: docker required" >&2; exit 1; }
command -v oha >/dev/null 2>&1 || { echo "error: oha required (cargo install oha)" >&2; exit 1; }

WORKDIR=$(mktemp -d -t pgo-workload-XXXXXX)
BINARY_PID=""
KAFKA_CID=""

cleanup() {
    local rc=$?
    [[ -n "$BINARY_PID" ]] && kill -TERM "$BINARY_PID" 2>/dev/null || true
    [[ -n "$KAFKA_CID" ]] && docker rm -f "$KAFKA_CID" >/dev/null 2>&1 || true
    rm -rf "$WORKDIR"
    exit $rc
}
trap cleanup EXIT INT TERM

# -------- Kafka container (KRaft single-node) -----------------------------

echo "pgo-workload: starting Kafka ($KAFKA_IMAGE)"
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
    if docker exec "$KAFKA_CID" /opt/kafka/bin/kafka-topics.sh \
        --bootstrap-server localhost:9092 --list >/dev/null 2>&1; then
        break
    fi
    [[ $i -eq 30 ]] && { echo "error: Kafka not ready" >&2; exit 1; }
    sleep 2
done

# -------- Binary config ---------------------------------------------------

# TODO: replace with your project's config schema. The key thing is
# that Kafka brokers point at the container (`localhost:19092`).
cat > "$WORKDIR/config.yaml" <<YAML
server:
  bind_address: "127.0.0.1:8080"
  auth: { mode: "none" }
kafka:
  brokers:
    - "localhost:19092"
  client_id: "pgo-workload"
destinations:
  default: "kafka"
routing:
  default_source: "pgo"
  topic_suffix: "_land"
YAML

# -------- Start binary ----------------------------------------------------

"$BINARY" --config "$WORKDIR/config.yaml" >"$WORKDIR/server.log" 2>&1 &
BINARY_PID=$!

for i in $(seq 1 60); do
    kill -0 "$BINARY_PID" 2>/dev/null || { echo "error: binary died" >&2; exit 1; }
    if curl -sf -o /dev/null --max-time 1 "http://127.0.0.1:8080/health/ready"; then
        break
    fi
    [[ $i -eq 60 ]] && { echo "error: not ready" >&2; exit 1; }
    sleep 1
done

# -------- Drive producer-side load ----------------------------------------

echo "pgo-workload: driving ingress (Kafka producer path) for ${DURATION}s"

cat > "$WORKDIR/payload.json" <<'JSON'
{"timestamp":"2026-04-17T12:34:56Z","level":"info","msg":"pgo workload"}
JSON

oha -z "${DURATION}s" \
    -q 200 -c 50 \
    -m POST -T application/json \
    -d "@$WORKDIR/payload.json" \
    "http://127.0.0.1:8080/" \
    || echo "warn: load generator reported errors" >&2

# Give librdkafka a moment to flush its internal queues before the
# binary is torn down (so the profile captures flush code paths too).
sleep 3

echo "pgo-workload: done"
