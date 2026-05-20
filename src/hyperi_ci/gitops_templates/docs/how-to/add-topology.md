# How to add a topology

A topology groups one or more HyperI applications into a single deployable unit.

## 1. Scaffold the skeleton

```bash
hyperi-ci init-topology <name> \
  --app dfe-loader \
  --app dfe-receiver \
  --app dfe-archiver
```

Replace `<name>` with a lowercase RFC-1123-style identifier, e.g. `prod-au` or
`staging-eu`.

This creates:

```
topologies/<name>/
├── topology.yaml   # Generated spec — edit to add chart sources, sync waves
├── values.yaml     # Per-app value overrides
├── glue/           # Empty — add glue charts here if needed
└── README.md       # Describe the purpose of this topology
```

## 2. Edit topology.yaml

Open `topology.yaml` and fill in the fields relevant to your deployment:

```yaml
apiVersion: hyperi.io/v1
kind: DeploymentTopology
metadata:
  name: <name>
spec:
  umbrella:
    name: hyperi-deployment-<name>
    description: "My topology description"
    appVersion: "1.0"
  apps:
    - name: dfe-loader
      version: "^1.0"
    - name: dfe-receiver
      version: "^1.0"
  thirdParty: []
  glue: []
  argocd:
    appOfApps: true
    appProject: platform
    syncWaves:
      dfe-receiver: 0
      dfe-loader: 1
```

See the [Topology Schema reference](../reference/topology-schema.md) for all
available fields.

## 3. Add value overrides

Edit `values.yaml` to set application-specific values:

```yaml
dfe-loader:
  enabled: true
  replicaCount: 2
  resources:
    requests:
      cpu: 500m
      memory: 512Mi

dfe-receiver:
  enabled: true
  replicaCount: 3
```

## 4. Validate locally

```bash
hyperi-ci stitch topologies/<name>/ --output-dir /tmp/stitched/<name>
helm lint /tmp/stitched/<name>
```

Fix any errors before opening a PR.

## 5. Open a pull request

```bash
git checkout -b add-topology-<name>
git add topologies/<name>/
git commit -m "feat: add <name> topology"
git push origin add-topology-<name>
```

CI validates and a reviewer is auto-assigned via CODEOWNERS.

## 6. After merge

The Stitch and Publish workflow packages and pushes the chart. Create or update
an `argocd/applicationsets/<name>.yaml` to deploy it to target clusters.
