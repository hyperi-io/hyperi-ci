# Topologies

Each subdirectory in this folder defines one **deployment topology** — a named
collection of HyperI applications and third-party charts that are deployed together
as a unit.

## Structure

```
topologies/
└── <topology-name>/
    ├── topology.yaml   # Declarative spec (apps, charts, ArgoCD config)
    ├── values.yaml     # Per-topology Helm value overrides
    ├── glue/           # Optional Helm glue charts (templates + helpers)
    └── README.md       # Purpose and operational notes for this topology
```

## Scaffold a new topology

```bash
hyperi-ci init-topology <name> --app dfe-loader --app dfe-receiver
```

This creates the directory skeleton and seeds `topology.yaml` and `values.yaml`
with sensible defaults.

## Lint and validate

```bash
hyperi-ci stitch topologies/<name>/ --output-dir /tmp/stitched/<name>
helm lint /tmp/stitched/<name>
```

CI runs this check automatically on every pull request.

## Publish

Merging to `main` triggers the **Stitch and Publish** workflow which packages the
stitched umbrella chart and pushes it to the Helm OCI registry.
