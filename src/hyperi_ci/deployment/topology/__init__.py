# Project:   HyperI CI
# File:      src/hyperi_ci/deployment/topology/__init__.py
# Purpose:   Operational tooling for deployment topologies (resolver + stitcher + CLI)
#
# License:   Proprietary - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Topology operational tooling.

The Pydantic schema for ``DeploymentTopology`` lives in
``hyperi_pylib.deployment.topology`` (shared cross-language data type).
This package holds hyperi-ci's operational tooling that consumes that
schema:

- ``resolve`` - OCI chart version resolution (semver range -> concrete version)
- ``stitch`` - umbrella chart generation from a topology

Consumers should import data types from pylib and operational helpers
from here:

.. code-block:: python

    from hyperi_pylib.deployment.topology import DeploymentTopology, load_topology
    from hyperi_ci.deployment.topology.resolve import resolve_versions
    from hyperi_ci.deployment.topology.stitch import stitch_topology
"""

from hyperi_ci.deployment.topology.resolve import (
    ChartVersionResolver,
    resolve_versions,
)
from hyperi_ci.deployment.topology.stitch import (
    StitchResult,
    generate_chart_yaml,
    stitch_topology,
)

__all__ = [
    "ChartVersionResolver",
    "StitchResult",
    "generate_chart_yaml",
    "resolve_versions",
    "stitch_topology",
]
