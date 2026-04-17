#!/usr/bin/env bash
# TEMPLATE: PGO workload for a service that CONSUMES from Kafka
# (loader-style: reads Kafka, batches, writes to downstream sink).
#
# See docs/PGO-WORKLOAD-GUIDE.md.
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

# TODO: topic the binary consumes from
TOPIC="${PGO_WORKLOAD_TOPIC:-events_land}"

# TODO: the downstream sink (ClickHouse, Postgres, etc.). Many loaders
# need a live sink — either spin up a container or disable the sink
# write with a config flag so only the consume+parse+route paths are
# profiled.
SINK_IMAGE="${PGO_WORKLOAD_SINK_IMAGE:-}"   # e.g. clickhouse/clickhouse-server:latest

# TODO: desired production rate (msgs/sec). The producer loop matches
# this so steady-state consumer backlog is modest, matching production.
PRODUCE_RPS="${PGO_WORKLOAD_PRODUCE_RPS:-1000}"

command -v docker >/dev/null 2>&1 || { echo "error: docker required" >&2; exit 1; }

WORKDIR=$(mktemp -d -t pgo-workload-XXXXXX)
BINARY_PID=""
KAFKA_CID=""
SINK_CID=""
PRODUCER_PID=""

cleanup() {
    local rc=$?
    [[ -n "$PRODUCER_PID" ]] && kill -TERM "$PRODUCER_PID" 2>/dev/null || true
    [[ -n "$BINARY_PID" ]] && kill -TERM "$BINARY_PID" 2>/dev/null || true
    sleep 2
    [[ -n "$BINARY_PID" ]] && kill -KILL "$BINARY_PID" 2>/dev/null || true
    [[ -n "$KAFKA_CID" ]] && docker rm -f "$KAFKA_CID" >/dev/null 2>&1 || true
    [[ -n "$SINK_CID" ]] && docker rm -f "$SINK_CID" >/dev/null 2>&1 || true
    rm -rf "$WORKDIR"
    exit $rc
}
trap cleanup EXIT INT TERM

# -------- Kafka -----------------------------------------------------------

echo "pgo-workload: starting Kafka"
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

# -------- Downstream sink (optional) --------------------------------------

if [[ -n "$SINK_IMAGE" ]]; then
    echo "pgo-workload: starting sink ($SINK_IMAGE)"
    # TODO: customise port + env for your specific sink
    SINK_CID=$(docker run -d --rm -p 8123:8123 "$SINK_IMAGE")
    sleep 5   # TODO: replace with proper readiness check
fi

# -------- Binary config + start -------------------------------------------

# TODO: replace with your project's config schema
cat > "$WORKDIR/config.yaml" <<YAML
kafka:
  brokers:
    - "localhost:19092"
  consumer:
    topics: ["$TOPIC"]
    group_id: "pgo-workload-consumer"
clickhouse:
  url: "http://localhost:8123"  # TODO: adjust if your sink differs
YAML

"$BINARY" --config "$WORKDIR/config.yaml" >"$WORKDIR/server.log" 2>&1 &
BINARY_PID=$!
sleep 5   # let the consumer join the group

# -------- Steady producer (pumps messages into Kafka) ---------------------

echo "pgo-workload: producing ~${PRODUCE_RPS} msg/s into $TOPIC for ${DURATION}s"
(
    end=$(( $(date +%s) + DURATION ))
    # Emit batches of messages via kafka-console-producer.
    # TODO: replace the message payload with a production-representative shape.
    while [[ $(date +%s) -lt $end ]]; do
        for _ in $(seq 1 "$PRODUCE_RPS"); do
            printf '{"ts":"%s","msg":"pgo payload","seq":%d}\n' \
                "$(date -Iseconds)" "$RANDOM"
        done | docker exec -i "$KAFKA_CID" /opt/kafka/bin/kafka-console-producer.sh \
            --bootstrap-server localhost:9092 \
            --topic "$TOPIC" >/dev/null 2>&1 || true
        sleep 1
    done
) &
PRODUCER_PID=$!

# Wait for duration; the binary keeps consuming throughout
sleep "$DURATION"

echo "pgo-workload: done"
