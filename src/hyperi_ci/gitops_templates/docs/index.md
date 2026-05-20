# GitOps Runbook

Welcome to the deployment runbook for this GitOps repository.

This repository is the single source of truth for all HyperI application
deployments. It defines **what** runs, **where** it runs, and **how** it is
configured — using declarative topology specifications that are validated,
packaged, and reconciled automatically by CI and ArgoCD.

## Quick links

| Task | Where to look |
|------|---------------|
| Deploy a new environment | [Add an Environment](how-to/add-environment.md) |
| Add a new application | [Add an Application](how-to/add-app.md) |
| Create a new topology | [Add a Topology](how-to/add-topology.md) |
| Roll back a bad deploy | [Rollback](operations/rollback.md) |
| Understand the architecture | [Architecture](concepts/architecture.md) |

## How CI works

1. You open a pull request.
2. The **Validate** workflow runs `hyperi-ci stitch` + `helm lint` on every topology.
3. You merge to `main`.
4. The **Stitch and Publish** workflow packages and pushes the updated Helm chart to GHCR.
5. ArgoCD detects the new chart version and reconciles the cluster.

All of this is automatic once the PR is merged.
