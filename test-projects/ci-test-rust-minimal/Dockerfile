# Project:   ci-test-rust-minimal
# File:      Dockerfile
# Purpose:   Minimal container image for CI publish pipeline testing

# ── Build stage ──────────────────────────────────────────────────────────────
FROM rust:1.82-slim AS builder

WORKDIR /build

# Copy manifests first for layer caching (no deps to fetch, but keeps pattern consistent)
COPY Cargo.toml Cargo.lock* rust-toolchain.toml ./
COPY src/ ./src/

RUN cargo build --release --bin ci-test

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl netcat-openbsd \
    && rm -rf /var/lib/apt/lists/* \
    && userdel -r ubuntu 2>/dev/null || true \
    && useradd --create-home --uid 1000 --shell /bin/bash appuser

COPY --from=builder --chown=appuser:appuser /build/target/release/ci-test /usr/local/bin/ci-test

USER appuser

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ci-test || exit 1

CMD ["ci-test"]
