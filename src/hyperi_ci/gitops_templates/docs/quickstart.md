# Quickstart

Get from zero to a running deployment in under 15 minutes.

## Prerequisites

- `hyperi-ci` installed: `uv tool install hyperi-ci`
- `helm` 3.16+: `brew install helm` or see [Helm docs](https://helm.sh/docs/intro/install/)
- `kubectl` pointing at a target cluster
- ArgoCD installed on the target cluster

## 1. Clone this repo

```bash
git clone https://github.com/<YOUR_ORG>/<YOUR_GITOPS_REPO>.git
cd <YOUR_GITOPS_REPO>
```

## 2. Scaffold a topology

```bash
hyperi-ci init-topology my-deployment \
  --app dfe-loader \
  --app dfe-receiver \
  --app dfe-archiver
```

This creates `topologies/my-deployment/` with a `topology.yaml` and `values.yaml`.

## 3. Validate locally

```bash
hyperi-ci stitch topologies/my-deployment/ --output-dir /tmp/stitched/my-deployment
helm lint /tmp/stitched/my-deployment
```

Fix any lint errors before proceeding.

## 4. Open a pull request

```bash
git checkout -b add-my-deployment
git add topologies/my-deployment/
git commit -m "feat: add my-deployment topology"
git push origin add-my-deployment
```

CI will run validation automatically. Review the workflow output in the PR.

## 5. Merge and watch ArgoCD

Once CI passes and the PR is approved, merge to `main`. The **Stitch and Publish**
workflow packages the chart and pushes it to GHCR. ArgoCD picks it up within its
next sync interval (default: 3 minutes).

Open the ArgoCD UI to watch the sync:

```bash
kubectl port-forward svc/argocd-server -n argocd 8080:443
# Open https://localhost:8080
```

## Next steps

- [Add another environment](how-to/add-environment.md)
- [Understand sync waves](reference/sync-waves.md)
- [Learn the full architecture](concepts/architecture.md)
