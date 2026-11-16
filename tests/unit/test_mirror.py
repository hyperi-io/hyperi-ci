# Project:   HyperI CI
# File:      tests/unit/test_mirror.py
# Purpose:   Which Docker Hub pins the mirror finds, and which input it refuses
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Pin extraction and validation for ``hyperi-ci mirror``.

The fixtures are the shapes the fleet's repos hold on their default branches.
"""

import json

import pytest

from hyperi_ci.config import load_org_config
from hyperi_ci.mirror import (
    Pin,
    dockerhub_name,
    extract,
    parse_pins,
    parse_ref,
    renovate_patterns,
)

KAFKA = "sha256:77e3df9054047a88b520d0cc46e16696d3b22022e1d580aeccd2632df6532837"
ALPINE = "sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6"
DEBIAN = "sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad"

# renovate-config default.json's custom manager, as the mirror reads it.
SHARED = {
    "customManagers": [
        {
            "customType": "regex",
            "managerFilePatterns": ["/\\.rs$/", "/\\.py$/", "/\\.sh$/"],
            "matchStrings": [
                "(?:#|//)\\s*renovate:\\s*datasource=(?<datasource>[a-z-]+)\\s+"
                "depName=(?<depName>[^\\s]+)[^\\n]*\\n[^\\n\"']*[\"']"
                "(?<currentValue>[^\"'\\s:]+)[\"'](?:[^\\n]*\\n(?:[ \\t]*(?:#|//)"
                "[ \\t]*[^r\\s][^\\n]*\\n)?[^\\n\"'=]*=[ \\t]*(?:\\n[ \\t]*)?[\"']"
                "(?<currentDigest>sha256:[a-f0-9]{64})[\"'])?"
            ],
        },
        {"customType": "jsonata", "fileFormat": "json", "matchStrings": ["x"]},
    ]
}
PATTERNS = renovate_patterns(SHARED)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("alpine", "library/alpine"),
        ("docker.io/alpine", "library/alpine"),
        ("index.docker.io/library/alpine", "library/alpine"),
        ("apache/kafka", "apache/kafka"),
        ("ghcr.io/kafbat/kafka-ui", None),
        ("harbor.devex.hyperi.io:8443/dockerhub/library/alpine", None),
        ("localhost/thing", None),
        ("mcr.microsoft.com/mssql/server", None),
        ("Upper/Case", None),
        ("", None),
    ],
)
def test_dockerhub_name(name: str, expected: str | None) -> None:
    assert dockerhub_name(name) == expected


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        (f"apache/kafka:4.3.1@{KAFKA}", Pin("apache/kafka", "4.3.1", KAFKA)),
        ("python:3.12-slim", Pin("library/python", "3.12-slim", "")),
        ("debian:${DEBIAN_TAG}", None),
        ("scratch", None),
        ("builder", None),
        ("ghcr.io/hyperi-io/dfe-toolbox-base:1.0", None),
        ("alpine:3@sha256:short", None),
        ("{{ .Values.image }}", None),
    ],
)
def test_parse_ref(ref: str, expected: Pin | None) -> None:
    assert parse_ref(ref) == expected


@pytest.mark.parametrize(
    ("path", "text", "expected"),
    [
        pytest.param(
            "scripts/pull-test-images.sh",
            "# renovate: datasource=docker depName=apache/kafka\n"
            'KAFKA_TAG="4.3.1"\n'
            "# The digest of KAFKA_TAG, on its own line.\n"
            f'KAFKA_DIGEST="{KAFKA}"\n',
            {Pin("apache/kafka", "4.3.1", KAFKA)},
            id="shell-tag-comment-digest",
        ),
        pytest.param(
            "crates/fetcher/tests/e2e/container.rs",
            "/// renovate: datasource=docker depName=alpine\n"
            'const ALPINE_TAG: &str = "3.24.2";\n'
            "\n"
            "/// Digest of `ALPINE_TAG`, apart from it.\n"
            "/// This is the multi-arch index digest.\n"
            "const ALPINE_DIGEST: &str =\n"
            f'    "{ALPINE}";\n',
            {Pin("library/alpine", "3.24.2", ALPINE)},
            id="rust-digest-past-the-shared-regex",
        ),
        pytest.param(
            "tests/otel_export.rs",
            "// renovate: datasource=docker depName=otel/opentelemetry-collector\n"
            'const COLLECTOR_TAG: &str = "0.162.0";\n'
            "\n"
            "// renovate: datasource=docker depName=prom/prometheus\n"
            'const PROM_TAG: &str = "v3.15.0";\n'
            f'const PROM_DIGEST: &str = "{KAFKA}";\n',
            {
                Pin("otel/opentelemetry-collector", "0.162.0", ""),
                Pin("prom/prometheus", "v3.15.0", KAFKA),
            },
            id="digest-of-the-next-annotation-not-borrowed",
        ),
        pytest.param(
            "tests/common/peers.rs",
            'pub const FLUENT: &str = "fluent/fluent-bit:5.1.5@' + KAFKA + '";\n'
            'pub const UI: &str = "ghcr.io/kafbat/kafka-ui:v1.5.0@' + KAFKA + '";\n',
            {Pin("fluent/fluent-bit", "5.1.5", KAFKA)},
            id="inline-literal-other-registry-skipped",
        ),
        pytest.param(
            "Dockerfile",
            f"FROM debian:trixie-slim@{DEBIAN}\n"
            "FROM --platform=$BUILDPLATFORM python:3.12-slim AS build\n"
            "FROM debian:${DEBIAN_TAG}\n"
            "FROM build\n",
            {
                Pin("library/debian", "trixie-slim", DEBIAN),
                Pin("library/python", "3.12-slim", ""),
            },
            id="dockerfile-from",
        ),
        pytest.param(
            "docker-compose.dev.yaml",
            f"  kafka:\n    image: apache/kafka:4.3.1@{KAFKA}\n"
            '    image: "python:3.12-slim"\n'
            "    image: {{ .Values.image }}\n",
            {
                Pin("apache/kafka", "4.3.1", KAFKA),
                Pin("library/python", "3.12-slim", ""),
            },
            id="compose-image-lines",
        ),
        pytest.param(
            "src/app.py",
            "# renovate: datasource=github-releases depName=apache/kafka\n"
            'VERSION = "4.3.1"\n'
            f'IMAGE = "apache/kafka:4.3.1@{KAFKA}"\n',
            {Pin("apache/kafka", "4.3.1", KAFKA)},
            id="other-datasource-ignored-tag-only-folded",
        ),
        pytest.param(
            "README.md",
            f"apache/kafka:4.3.1@{KAFKA}\n",
            {Pin("apache/kafka", "4.3.1", KAFKA)},
            id="literal-counts-in-any-scanned-file",
        ),
        pytest.param("empty.rs", "", set(), id="empty"),
    ],
)
def test_extract(path: str, text: str, expected: set[Pin]) -> None:
    assert extract(path, text, PATTERNS) == expected


def test_shared_regex_only_applies_to_its_files() -> None:
    text = "# renovate: datasource=docker depName=alpine\nTAG='3.24.2'\n"
    assert extract("values.toml", text, PATTERNS) == set()
    assert extract("pull.sh", text, PATTERNS) == {Pin("library/alpine", "3.24.2")}


@pytest.mark.parametrize(
    "payload",
    [
        "{}",
        "not json",
        json.dumps([{"name": "ghcr.io/x/y", "tag": "1"}]),
        json.dumps([{"name": "alpine", "tag": "1"}]),
        json.dumps([{"name": "library/alpine", "tag": "1; rm -rf /"}]),
        json.dumps([{"name": "library/alpine", "tag": "1", "digest": "sha256:no"}]),
        json.dumps([{"name": "library/alpine", "tag": "1", "extra": "x"}]),
        json.dumps(["library/alpine:1"]),
        json.dumps([{"name": "library/alpine", "tag": "1"}] * 201),
    ],
)
def test_parse_pins_refuses(payload: str) -> None:
    with pytest.raises(ValueError):
        parse_pins(payload)


def test_parse_pins_round_trip() -> None:
    pins = [Pin("library/alpine", "3.24.2", ALPINE), Pin("prom/prometheus", "v3")]
    payload = json.dumps(
        [{"name": p.name, "tag": p.tag, "digest": p.digest} for p in pins]
    )
    assert parse_pins(payload) == pins
    assert parse_pins("[]") == []


def test_pin_refs() -> None:
    prefix = load_org_config().mirror_prefix
    assert prefix == "ghcr.io/hyperi-io/mirror"
    pin = Pin("library/alpine", "3.24.2", ALPINE)
    assert pin.source == f"docker://docker.io/library/alpine@{ALPINE}"
    assert (
        Pin("prom/prometheus", "v3").source == "docker://docker.io/prom/prometheus:v3"
    )
    assert pin.mirrored(prefix) == "ghcr.io/hyperi-io/mirror/library/alpine:3.24.2"
