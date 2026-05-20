# Overview

## What is a GitOps repo?

A GitOps repository is the **single source of truth** for what should be running
in your clusters. Instead of applying changes with `kubectl apply` or `helm install`
by hand, you commit the desired state to git and let automation reconcile the cluster
to match.

Benefits:

- **Auditability** — every change is a commit with an author, timestamp, and diff.
- **Rollback** — reverting a bad deploy is `git revert`.
- **Consistency** — staging and production run the same chart, different values.
- **Review** — infrastructure changes go through the same PR process as code.

## Core concepts

### Topology

A **topology** is a named collection of HyperI applications (and optional
third-party charts) that are deployed together as a unit. Topologies are defined
in `topologies/<name>/topology.yaml` using the `DeploymentTopology` schema.

### Stitch

**Stitching** is the process of composing a topology into a single umbrella Helm
chart. `hyperi-ci stitch` reads `topology.yaml`, resolves dependencies, applies
value overrides, and writes a ready-to-install chart to an output directory.

### ArgoCD ApplicationSet

An **ApplicationSet** generates one ArgoCD `Application` per target environment
from a single template. When `helm push` publishes a new chart version, ArgoCD
detects the change and reconciles all generated Applications automatically.

### Sync waves

ArgoCD **sync waves** control the order in which resources are applied within a
single sync operation. Ingress components (receivers, fetchers) deploy before
processing components (loaders, transformers) to ensure the pipeline is ready
to accept data before upstream traffic is allowed in.

See [Sync Waves reference](../reference/sync-waves.md) for details.
