# HyperI GitOps

Single source of truth for HyperI platform configuration. Everything
the platform team owns lives here:

- **`topologies/`** — DeploymentTopology declarations. Each topology
  describes which apps + third-party charts compose into a deployable
  HyperI stack. Consumed by `hyperi-ci stitch` to produce umbrella
  Helm charts in `oci://ghcr.io/hyperi-io/helm-charts/`.
- **`argocd/`** — ArgoCD ApplicationSets, AppProjects, and bootstrap
  manifests. The reconciliation source-of-truth.
- **`values/`** — Per-topology + per-environment values overrides
  applied on top of the umbrella charts by ArgoCD multi-source
  Applications.
- **`terraform/`** — OpenTofu IaC for cluster provisioning (`.tf`, run
  with `tofu`). AWS (EKS) and Rancher (RKE2) live here as sibling subtrees.
- **`docs/`** — Reference documentation rendered to GitHub Pages
  (MkDocs Material) and mirrored to GitBook for the public site.

## Quickstart

See [`docs/quickstart.md`](docs/quickstart.md).

## How a deployment ships

```
per-app repo (scalo-rs/scalo-py)
   │  emit-chart → helm push to OCI
   ▼
oci://ghcr.io/hyperi-io/helm-charts/<app>:<version>

  +

hyperi-io/gitops/topologies/<topology>/topology.yaml
   │  CI: hyperi-ci stitch → helm push umbrella
   ▼
oci://ghcr.io/hyperi-io/helm-charts/hyperi-deployment-<topology>:<version>

  +

hyperi-io/gitops/argocd/applicationsets/*.yaml
hyperi-io/gitops/values/<topology>/<env>.yaml
   │  ArgoCD reconciles
   ▼
K8s cluster
```

## Repository structure

```
.
├── .github/workflows/        # CI: validate, stitch+publish, docs
├── .gitbook.yaml             # GitBook GitHub Sync config
├── CODEOWNERS                # platform-team ownership
├── LICENSE                   # BUSL-1.1
├── README.md                 # this file
├── docs/                     # MkDocs Material site (→ GitHub Pages)
├── mkdocs.yml                # docs config
│
├── topologies/<name>/        # DeploymentTopology declarations
│   ├── topology.yaml
│   ├── values.yaml
│   ├── values.{dev,staging,prod}.yaml
│   └── glue/                 # Helm-template glue (Strimzi CRs etc.)
│
├── argocd/
│   ├── appprojects/          # AppProject CRDs
│   ├── applicationsets/      # ApplicationSet CRDs
│   └── bootstrap/            # root app-of-apps
│
├── values/<topology>/<env>.yaml  # ArgoCD multi-source values
│
└── terraform/
    ├── aws/{environments,modules}/
    └── rancher/{clusters,modules}/
```

## Tooling

| Tool | Purpose |
|---|---|
| `hyperi-ci stitch <topology>` | Compose topology → umbrella chart |
| `hyperi-ci init-gitops <dir>` | Scaffold a new gitops repo (this) |
| `helm install` | Install umbrella charts directly (non-ArgoCD) |
| ArgoCD | Reconcile cluster state from `argocd/` |
| OpenTofu (`tofu`) | Provision AWS / Rancher infra under `terraform/` |
| MkDocs Material | Render `docs/` → GitHub Pages |

## Contributing

Per `CODEOWNERS`, platform-team approval required for changes outside
`topologies/`. Topology PRs require:

1. Schema validation passing (CI `validate` job)
2. `helm lint` passing on the stitched umbrella (CI preview)
3. One platform-team reviewer

PRs to `terraform/` require a `tofu plan` artifact in the PR
description.
