# Sync Waves

ArgoCD sync waves control the order in which resources are applied within a
single sync operation. Resources in lower-numbered waves are applied and must
become healthy before the next wave starts.

## Why waves matter for DFE pipelines

The DFE data pipeline has a dependency order:

1. **Ingress** (receivers, fetchers) — must be up before the pipeline has data.
2. **Processing** (loaders, transformers) — consume from ingress outputs.
3. **Storage sinks** (archivers) — drain from processing outputs.
4. **Observability** (monitoring stacks) — should be available before traffic
   arrives, but non-blocking.

If processing components start before ingress, they briefly have no data source.
If ingress starts before processing, backpressure builds. Waves prevent these
ordering issues.

## Recommended wave assignments

| Wave | Components | Rationale |
|------|-----------|-----------|
| 0 | `dfe-receiver`, `dfe-fetcher` | Ingress — accept data first |
| 1 | `dfe-loader`, `dfe-transform-vrl`, `dfe-transform-vector` | Processing — start after ingress is healthy |
| 1 | `dfe-archiver` | Sink — can start alongside processing |
| 2 | Third-party observability (Prometheus, Grafana) | Non-blocking; useful to have running during ramp-up |

## Configuring waves

Set `spec.argocd.syncWaves` in `topology.yaml`:

```yaml
argocd:
  syncWaves:
    dfe-receiver: 0
    dfe-fetcher: 0
    dfe-loader: 1
    dfe-archiver: 1
    dfe-transform-vrl: 1
    monitoring: 2
```

## ArgoCD annotation

`hyperi-ci stitch` injects the `argocd.argoproj.io/sync-wave` annotation onto
the Helm release resource for each application based on the value in `syncWaves`.

If `syncWaves` is omitted for an app, ArgoCD uses wave `0` by default.

## Caveats

- Waves only apply within a **single sync operation**. A healthy App-of-Apps
  deployment may have all waves visible simultaneously in ArgoCD UI — the wave
  annotations only enforce ordering when ArgoCD is performing a sync.
- Waves do not replace readiness probes. Ensure all apps have correct
  `readinessProbe` configured so ArgoCD knows when a wave is complete.
