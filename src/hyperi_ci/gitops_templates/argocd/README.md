# ArgoCD Configuration

This directory contains ArgoCD manifests that control how topologies are
reconciled into target clusters.

## Directory layout

```
argocd/
├── appprojects/        # AppProject manifests (one per team / environment boundary)
├── applicationsets/    # ApplicationSet manifests (one per topology, drives App-of-Apps)
├── bootstrap/          # Cluster-bootstrap manifests applied once at cluster registration
└── README.md           # This file
```

## AppProjects

`AppProject` manifests scope what repositories, clusters, and namespaces a set of
ArgoCD Applications may target. One project per trust boundary.

```yaml
apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata:
  name: platform
  namespace: argocd
spec:
  sourceRepos:
    - 'https://github.com/hyperi-io/helm-charts'
  destinations:
    - namespace: '*'
      server: 'https://kubernetes.default.svc'
  clusterResourceWhitelist:
    - group: '*'
      kind: '*'
```

## ApplicationSets

`ApplicationSet` manifests generate one `Application` per target environment from
a single template, using the **List** or **Git** generator.

```yaml
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: default-topology
  namespace: argocd
spec:
  generators:
    - list:
        elements:
          - cluster: staging
            url: https://staging.k8s.example.com
          - cluster: production
            url: https://production.k8s.example.com
  template:
    metadata:
      name: 'default-topology-{{cluster}}'
    spec:
      project: platform
      source:
        repoURL: oci://ghcr.io/hyperi-io/helm-charts
        chart: hyperi-deployment-default
        targetRevision: '*'
      destination:
        server: '{{url}}'
        namespace: hyperi
      syncPolicy:
        automated:
          prune: true
          selfHeal: true
```

## Validation

CI validates all manifests in this directory using `kubectl --dry-run=client apply`.
