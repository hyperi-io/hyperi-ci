# Project:   HyperI CI
# File:      src/hyperi_ci/argocd/__init__.py
# Purpose:   ArgoCD Application generation + push into central GitOps repo
#
# License:   Proprietary - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""ArgoCD stage: generate Application YAML + push into hyperi-io/gitops.

Stage flow (per overlay-framework spec section 10.5):

  1. Invoke consumer's ``emit-argocd`` subcommand
  2. Apply ``publish.argocd.overlays`` to the YAML
  3. Clone the GitOps repo into a temp dir
  4. Write the Application YAML to ``applications/<app>/<env>.yaml``
  5. git commit + push (or open a PR per env policy)

The GitOps repo (hyperi-io/gitops) is the public single-source-of-truth
for ArgoCD cluster state. Its bootstrap spec lives at
``docs/superpowers/specs/2026-05-15-gitops-repo-bootstrap-spec.md``.
"""
