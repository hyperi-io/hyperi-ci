# Topology Schema Reference

A `topology.yaml` file conforms to the `DeploymentTopology` custom resource schema.

## Top-level structure

```yaml
apiVersion: hyperi.io/v1
kind: DeploymentTopology
metadata:
  name: <string>          # topology identifier; must match directory name
spec:
  umbrella: { ... }
  apps: [ ... ]
  thirdParty: [ ... ]
  glue: [ ... ]
  argocd: { ... }
```

## spec.umbrella

Metadata for the generated umbrella Helm chart.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Chart name (e.g. `hyperi-deployment-default`) |
| `description` | string | no | Human-readable description |
| `appVersion` | string | no | Application version baked into chart metadata |

## spec.apps

List of HyperI application charts to include.

```yaml
apps:
  - name: dfe-loader        # chart name in the configured registry
    version: "^1.0"         # semver constraint; resolved at stitch time
    alias: loader           # optional — override the release name
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Chart name |
| `version` | string | yes | Semver constraint (`^1.0`, `>=2.0,<3.0`, `1.2.3`) |
| `alias` | string | no | Release name override (default: `name`) |

## spec.thirdParty

List of third-party Helm charts (from external repositories).

```yaml
thirdParty:
  - name: kube-prometheus-stack
    repo: https://prometheus-community.github.io/helm-charts
    chart: kube-prometheus-stack
    version: "^56.0"
    alias: monitoring       # required if name ≠ chart
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Logical identifier |
| `repo` | string | yes | Helm repository URL |
| `chart` | string | yes | Chart name within the repo |
| `version` | string | yes | Semver constraint |
| `alias` | string | no | Release name (default: `name`) |

## spec.glue

List of local glue chart directories (relative to the topology directory).

```yaml
glue:
  - path: glue/shared-config   # relative to topology directory
    alias: shared-config
```

## spec.argocd

ArgoCD-specific deployment configuration.

```yaml
argocd:
  appOfApps: true
  appProject: platform
  syncWaves:
    dfe-receiver: 0
    dfe-loader: 1
    dfe-transform-vrl: 1
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `appOfApps` | bool | no | Generate App-of-Apps pattern (default: `true`) |
| `appProject` | string | no | ArgoCD AppProject name (default: `default`) |
| `syncWaves` | map[string, int] | no | Per-app sync wave number |

## Example: complete topology.yaml

```yaml
apiVersion: hyperi.io/v1
kind: DeploymentTopology
metadata:
  name: production
spec:
  umbrella:
    name: hyperi-deployment-production
    description: Full DFE pipeline for production
    appVersion: "1.0"
  apps:
    - name: dfe-receiver
      version: "^2.7"
    - name: dfe-loader
      version: "^2.7"
    - name: dfe-archiver
      version: "^2.7"
    - name: dfe-transform-vrl
      version: "^2.7"
  thirdParty:
    - name: kube-prometheus-stack
      repo: https://prometheus-community.github.io/helm-charts
      chart: kube-prometheus-stack
      version: "^56.0"
      alias: monitoring
  glue: []
  argocd:
    appOfApps: true
    appProject: platform
    syncWaves:
      dfe-receiver: 0
      dfe-loader: 1
      dfe-archiver: 1
      dfe-transform-vrl: 1
      monitoring: 2
```
