# Rollback

Rolling back a bad deployment is a git revert. There is no separate rollback
command — the GitOps model means the desired state is always in git.

## Option 1: Revert the PR (preferred)

Find the commit that introduced the bad change and revert it:

```bash
git log --oneline -10   # find the bad commit SHA
git revert <SHA>
git push origin main
```

GitHub also provides a **Revert** button on merged pull requests.

CI validates the revert and the Stitch and Publish workflow runs automatically,
publishing a new chart version. ArgoCD reconciles the cluster within minutes.

## Option 2: Pin to a previous chart version (fast, temporary)

If you need to roll back immediately without waiting for a new chart push,
you can pin the ApplicationSet to a previous chart version:

```yaml
# argocd/applicationsets/<name>.yaml
spec:
  template:
    spec:
      source:
        targetRevision: '1.4.2'   # pin to last-known-good version
```

!!! warning
    This is a temporary measure. The pin must be removed once a proper revert
    commit is merged and the bad chart version is no longer in circulation.

## Option 3: Manual helm rollback (emergency only)

If ArgoCD auto-sync is keeping you from rolling back manually, you can:

1. Disable auto-sync in the ArgoCD UI for the affected Application.
2. Run `helm rollback <release-name> <revision> -n <namespace>`.
3. Investigate the root cause.
4. Re-enable auto-sync once a fix is merged.

!!! warning
    Manual helm operations bypass GitOps and will be overwritten on the next
    ArgoCD sync. Always follow up with a proper git revert.

## Verifying the rollback

```bash
# Check ArgoCD application status
argocd app get <app-name>

# Check pod status
kubectl get pods -n <namespace>

# Check application logs
kubectl logs -n <namespace> -l app=<app-label> --tail=100
```

## Preventing future incidents

After a rollback, open a post-incident review and update the relevant
`values.yaml` or `topology.yaml` files to add guardrails (resource limits,
PodDisruptionBudget, pre-sync hooks) that would have caught the issue in CI.
