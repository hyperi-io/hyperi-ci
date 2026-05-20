# How to add an application

This guide covers adding a new HyperI application to an existing topology.

## Prerequisites

- The application's Helm chart must be published to the Helm registry that
  `hyperi-ci stitch` is configured to pull from.
- You need at least one existing topology to add the app to. If not, see
  [Add a Topology](add-topology.md) first.

## 1. Add the app to topology.yaml

Open `topologies/<topology-name>/topology.yaml` and add an entry under `spec.apps`:

```yaml
spec:
  apps:
    - name: dfe-loader
      version: "^1.0"
    - name: dfe-receiver
      version: "^1.0"
    # Add new app here:
    - name: dfe-transform-vrl
      version: "^1.0"
```

## 2. Set a sync wave

Add the app to `spec.argocd.syncWaves`. Lower numbers deploy first.

```yaml
  argocd:
    syncWaves:
      dfe-receiver: 0   # ingress first
      dfe-loader: 1     # processing second
      dfe-transform-vrl: 1   # same wave as loader — parallel
```

See [Sync Waves reference](../reference/sync-waves.md) for wave ordering guidance.

## 3. Add value overrides

Edit `topologies/<topology-name>/values.yaml`:

```yaml
dfe-transform-vrl:
  enabled: true
  replicaCount: 2
  resources:
    requests:
      cpu: 250m
      memory: 256Mi
```

## 4. Validate and PR

```bash
hyperi-ci stitch topologies/<topology-name>/ --output-dir /tmp/stitched/<topology-name>
helm lint /tmp/stitched/<topology-name>

git checkout -b add-dfe-transform-vrl
git add topologies/<topology-name>/
git commit -m "feat: add dfe-transform-vrl to <topology-name>"
git push origin add-dfe-transform-vrl
```

## Adding a third-party chart

For non-HyperI charts (e.g. Prometheus, Grafana), add them under `spec.thirdParty`
instead of `spec.apps`:

```yaml
spec:
  thirdParty:
    - name: prometheus
      repo: https://prometheus-community.github.io/helm-charts
      chart: kube-prometheus-stack
      version: "^56.0"
      alias: monitoring
```

Then add values under the alias key in `values.yaml`:

```yaml
monitoring:
  enabled: true
  grafana:
    enabled: true
```
