<!--
Project:   HyperI CI
File:      docs/quality-gate-doc-linting.md
Purpose:   The five markdown checks in lint-docs, gate semantics, and adoption impact

License:   BUSL-1.1 -- HYPERI PTY LIMITED
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Quality gate: documentation linting

`hyperi-ci lint-docs <dir>`
runs the same five checks as inside the quality stage. All five default to
`warn` and gate only where a repo has promoted one: a new lint introduced as
blocking on an existing tree fails every PR until the backlog clears, so it
lands at warn and is promoted per repo at zero violations. doc-paths, lychee
and mermaid-parse are deterministic and carry no style opinion, so they are
the ones ready to promote first.

lychee runs `--offline`, so it answers about THIS repo only. External links
fail for rate limits and outages that have nothing to do with the commit, and
that check is deliberately NOT built here - it belongs on a schedule.
mermaid-parse uses `mermaid.parse` via Node (`mermaid` + `linkedom`), never
`mmdc`, which needs headless Chrome and has exited 0 on syntax it could not
draw. Without Node the structural half still runs; a repo that has promoted the
check to blocking fails in CI rather than passing unproven.

On CI every tool is installed by hyperi-ci, pinned in `versions.yaml`. lychee is a digest-checked release tarball. markdownlint-cli2, mermaid and linkedom come from one `npm ci` against `src/hyperi_ci/config/node-tools/package-lock.json`, whose sha512 per package pins the whole tree. The runner's own `node` and `npm` do that install, and the GitHub-hosted and ARC native images both carry them. A tool already on PATH, or mermaid + linkedom in the repo's own `node_modules`, wins. Bumping one of the three npm pins means `uv run scripts/relock-node-tools.py` in the same commit, which resolves the tree as of 7 days ago so a transitive dependency gets the same soak as the pin. A transitive package with an advisory its parent pins past (lodash-es under chevrotain) gets a `versions.yaml` entry and a place in `NODE_OVERRIDES` in `quality/node_tools.py`, which renders it as an npm `overrides` entry. Relock the same way.

**Vale is not adopted.** The reasons recorded in hyperi-ai's
`standards/universal/documentation-structure.md` hold here: it is a string
matcher aimed at an intent problem, its Google/Microsoft packages encode
American English against a house style that is Australian, and a second prose
vocabulary is the drift that standard exists to prevent. General English usage
remains the one thing it would be right for, as its own build with a
maintained rule package - not a module of this dimension.

## Gate semantics

hadolint gates on **error severity only**: a blocking hadolint fails on an
error-level finding (a broken `RUN` shell caught by ShellCheck), while
warning/info (DL3008 apt-pin, DL4006 pipefail, ...) surface but never fail.
kubeconform fails on a schema-invalid manifest. droast, kube-linter and checkov
are advisory by default and never fail the build (checkov can be escalated to
`blocking` per repo once its findings are tuned).

## How findings surface (the layered stack)

Every tool parses its output into one shared surface (`quality/findings.py`):

- **GitHub annotations** - a *bounded* set of inline pointers, errors first.
  GitHub caps annotations at 10 error + 10 warning per step and silently drops
  the rest; the whole quality stage is one step, so that budget is shared across
  all tools. When it is exhausted the log says "+N more, see summary".
- **Job summary** - the *complete* findings list as a markdown table
  (`$GITHUB_STEP_SUMMARY`), bounded at 1000 rows (well past any real run) with a
  truncation note, so it stays under GitHub's 1MiB/step ceiling. The
  authoritative record.
- **SARIF** - opt-in via `--sarif <path>` on the verb. Writing the file is
  always safe; UPLOADING it into code scanning needs GitHub Code Security (a
  paid add-on on private repos), so the *workflow* does the upload, gated to
  where it is enabled - hyperi-ci never uploads and never triggers the "must
  enable" error.

## Config

Modes are the usual `blocking` / `warn` / `disabled` under `quality.<tool>`
(cross-language, top-level - not per-language). `quality.<tool>` may also be a
**dict** carrying a `mode` plus tool options:

```yaml
quality:
  hadolint: blocking          # or warn / disabled
  droast: warn
  kubeconform:
    mode: blocking
    schema_locations:         # extra CRD schema locations for kubeconform
      - /path/to/crd-schemas
  checkov:
    mode: warn
    frameworks: [kubernetes, helm, terraform]
    skip: [CKV_K8S_35]        # skip check IDs (e.g. an ExternalSecret false positive)
    skip_paths: ['.*/vendor/.*']  # extra path regexes to exclude (on top of .worktrees / .tmp)
```

## Coverage caveats (a green gate is not full proof)

- kubeconform runs with `-ignore-missing-schemas`: a CRD with no schema anywhere
  is **skipped**, not validated. A curated CRD schema location (the datreeio
  catalogue is included by default) covers the common operators; the rest are
  reported skipped, not green-lit.
- For multi-source ArgoCD apps, the rendered manifest uses **in-repo default
  values only** - the real cluster manifest depends on an external overlay, so a
  passing kubeconform gate validates the chart-under-defaults, not the deployed
  result.

## Does this break existing projects?

No, by design - the failure surface is deliberately narrow:

- **No Dockerfile, no k8s, no `.tf` -> nothing runs.** hadolint/droast auto-detect
  Dockerfiles and info-skip a repo with none. The k8s/IaC tools only run when you
  explicitly call `lint-manifests`. A plain Rust/Python library sees zero change.
- **The k8s/IaC tools never run in the normal quality stage.** kubeconform,
  kube-linter and checkov are *only* reachable through the `lint-manifests` verb
  (Path B). Bumping hyperi-ci does not add them to any language project's CI - a
  gitops repo has to opt in by calling the verb.
- **The one gate that auto-runs (hadolint) fails on ERROR severity only.** Routine
  Dockerfile noise (DL3008 unpinned apt, DL4006 pipefail, base-image pinning) is
  warning-tier and is surfaced, not failed. Only a genuine defect - chiefly a
  broken `RUN` shell caught by embedded ShellCheck - fails the build.
- **A missing tool never breaks the local loop.** `hyperi-ci check` warn-skips a
  linter that is not installed; in CI hadolint auto-installs, and if that install
  itself fails the stage does not crash (it reports and, for a blocking gate,
  fails rather than falsely pass).
- **Everything is opt-out** per repo: `quality.hadolint: disabled` (or `warn`),
  and the same for each tool.

**The one real behaviour change:** a project that *has* a Dockerfile whose hadolint
finds an **error-severity** issue will newly fail CI where it previously had no
Dockerfile check at all. That is the intended gate (a broken `RUN` is a real bug),
but it is a change - so on first adoption, run `hyperi-ci run quality` locally, or
set `quality.hadolint: warn` for a migration window, fix the findings, then flip it
back to `blocking`.

## Adopting it on a new or external project

- **A language project (Python/Rust/Go/TS) that already uses hyperi-ci** - nothing
  to do. hadolint + droast light up automatically the next time `run quality`
  executes, *iff* the repo has a Dockerfile. Tune with `quality.hadolint` /
  `quality.droast` if needed.
- **A brand-new project** - onboard it with `hyperi-ci init` / `/onboard` as usual;
  the linting is part of the quality stage it scaffolds. No extra wiring.
- **A gitops / infra repo (Helm charts, k8s manifests, `.tf`)** - even one that is
  GitHub-Actions-native with no `.hyperi-ci.yaml` - add one step to its workflow:
  `hyperi-ci lint-manifests .`. It needs `helm` on the runner for the kubeconform
  schema gate (kube-linter/checkov still run without it). A CRD-heavy cluster repo
  will want `quality.kubeconform.schema_locations` for its operators and a
  `quality.checkov.skip` list for known false positives (e.g. External-Secrets
  `ExternalSecret` CRs). The gitops scaffold (`hyperi-ci init-gitops`) ships a
  `validate.yaml` that already calls the verb.
- **An external / third-party project you are migrating in** - start every tool at
  `warn` (advisory) so the first run is a report, not a wall of failures; triage
  the findings; then promote hadolint (and, if wanted, checkov) to `blocking` once
  the repo is clean. The auto-detect + opt-out model means adoption is incremental,
  never all-or-nothing.

## See also

- [quality-gate.md](quality-gate.md) -- mode resolution and the override mechanisms
- [quality-gate-tools.md](quality-gate-tools.md) -- the per-tool reference, including the Container + k8s + IaC tools this page's gate semantics cover
