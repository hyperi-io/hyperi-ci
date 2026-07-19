# Disaster Recovery

This document covers recovering the deployment platform itself — not application
data recovery (which is handled by individual service runbooks).

## Scenario 1: Lost cluster — restore from GitOps

The entire cluster state is recoverable from this repo. All desired state is in git.

### Steps

1. **Provision a new cluster** using OpenTofu:

   ```bash
   tofu -chdir=terraform/aws init
   tofu -chdir=terraform/aws apply -var-file=environments/production.tfvars
   ```

2. **Install ArgoCD** on the new cluster:

   ```bash
   kubectl create namespace argocd
   kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
   ```

3. **Register the cluster** with ArgoCD CLI:

   ```bash
   argocd cluster add <new-context> --name production
   ```

4. **Apply bootstrap manifests**:

   ```bash
   kubectl apply -f argocd/bootstrap/
   ```

5. **Apply AppProjects and ApplicationSets**:

   ```bash
   kubectl apply -f argocd/appprojects/
   kubectl apply -f argocd/applicationsets/
   ```

6. **Wait for sync**. ArgoCD will pull the latest chart from GHCR and reconcile.

   ```bash
   argocd app list   # monitor sync status
   ```

## Scenario 2: ArgoCD itself is unavailable

If ArgoCD is down but the cluster is healthy, you can deploy directly with Helm:

```bash
# Stitch the topology locally
hyperi-ci stitch topologies/production/ --output-dir /tmp/stitched/production

# Deploy directly
helm upgrade --install hyperi-deployment-production /tmp/stitched/production \
  -n hyperi --create-namespace \
  -f values/production.yaml

# Once ArgoCD is restored it will reconcile back to the same state
```

## Scenario 3: Helm registry (GHCR) is unavailable

The stitched chart source is this repository. You can always re-stitch and
deploy locally even without GHCR access:

```bash
hyperi-ci stitch topologies/production/ --output-dir /tmp/stitched/production
helm upgrade --install ... /tmp/stitched/production
```

## Recovery time objectives

| Component | RTO | RPO | Recovery method |
|-----------|-----|-----|----------------|
| Cluster | ~30 min | 0 (git is source of truth) | OpenTofu + bootstrap |
| ArgoCD | ~10 min | 0 | `kubectl apply -f argocd/` |
| Application state | ~5 min | Depends on app | Helm install from stitch |

## Contact

For incidents affecting production, follow the on-call runbook in your incident
management system. This document covers the GitOps platform layer only.
