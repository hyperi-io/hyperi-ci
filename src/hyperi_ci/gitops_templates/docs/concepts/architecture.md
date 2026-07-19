# Architecture

## Repository layout

```
<gitops-repo>/
├── topologies/         # Deployment topology specs
│   └── <name>/
│       ├── topology.yaml
│       ├── values.yaml
│       └── glue/
├── argocd/             # ArgoCD manifests
│   ├── appprojects/
│   ├── applicationsets/
│   └── bootstrap/
├── values/             # Shared environment value overrides
├── terraform/          # IaC (cluster provisioning, networking)
├── docs/               # This documentation
└── .github/workflows/  # CI pipelines
```

## Deployment pipeline

```
Developer
    │
    ▼
git push / PR
    │
    ▼
.github/workflows/validate.yaml
  ├── hyperi-ci stitch (topology schema + dep resolution)
  └── helm lint (chart structure + template validity)
    │
    ▼ (merge to main)
    │
.github/workflows/stitch-and-publish.yaml
  ├── hyperi-ci stitch (full build)
  ├── helm package
  └── helm push → ghcr.io/<org>/helm-charts
    │
    ▼
ArgoCD ApplicationSet
  ├── detects new chart version in OCI registry
  └── generates Application per environment
    │
    ▼
ArgoCD Application (per cluster × topology)
  └── helm install / upgrade → cluster
```

## Multi-environment model

A single topology definition is deployed to multiple environments by varying only
the values. The ApplicationSet generator iterates over a list of environments;
each generates an ArgoCD Application pointing at the same chart version.

```
topology.yaml   ──► stitched umbrella chart ──► GHCR OCI registry
                                                      │
                                           ┌──────────┼──────────┐
                                           ▼          ▼          ▼
                                       staging    production   dev
                                     (values A)  (values B) (values C)
```

## Security model

- The gitops repo itself is the trust boundary. Who can merge = who can deploy.
- CODEOWNERS assigns review responsibility per directory.
- AppProject manifests in ArgoCD constrain which repositories and clusters each
  set of Applications may target.
- OpenTofu state is stored remotely with appropriate IAM/RBAC controls.
