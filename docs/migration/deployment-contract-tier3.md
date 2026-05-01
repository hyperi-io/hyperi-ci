# Migrating to the Deployment Contract — Tier 3

For apps that **don't** use hyperi-rustlib or hyperi-pylib (bash, TypeScript,
Go, ad-hoc projects). Tier 1 (Rust) and Tier 2 (Python) migration guides
will be added once the rustlib 2.7+ / pylib 2.x producers ship.

Adopting the deployment contract means:

- Replacing your hand-rolled `Dockerfile` (or set of CI templates) with a
  single source-of-truth JSON contract.
- Letting `hyperi-ci emit-artefacts` regenerate the Dockerfile, Helm
  chart, ArgoCD `Application`, and container manifest from that contract
  — same logic that Rust and Python apps use, no per-language drift.
- Getting a CI-enforced drift check that fails when committed artefacts
  diverge from what the contract says.

## Pre-flight checks

| Check | Command | Expected |
|---|---|---|
| hyperi-ci on PATH | `hyperi-ci --version` | `1.15.0` or newer |
| Repo has no rustlib/pylib dep | `grep -E 'hyperi-rustlib\|hyperi-pylib' Cargo.toml pyproject.toml 2>/dev/null` | no match |
| Existing `ci/` not present, or backup made | `ls ci/ 2>/dev/null` | empty or backed up |

If your repo is on **rustlib** or **pylib**, follow the matching tier
guide instead — Tier 3 commits the JSON manually, while Tier 1/2 emit it
from the app's source.

## Step 1 — Scaffold the contract

```bash
hyperi-ci init-contract --app-name my-app
```

Writes `ci/deployment-contract.json` derived from your `--app-name`.
The defaults follow DFE conventions (`/healthz`, `/readyz`, `/metrics`,
metrics on `:9090`, config at `/etc/<app>/<app>.yaml`).

The scaffolded file validates against the Pydantic `DeploymentContract`
out of the box, so step 3 will work without further editing — but
in practice you'll want to fill in app-specific bits in step 2.

`hyperi-ci init-contract` refuses to overwrite an existing
`ci/deployment-contract.json`. Pass `--force` or delete the file first
if you want to start over.

## Step 2 — Fill in app-specific fields

Open `ci/deployment-contract.json` and adjust:

```jsonc
{
  "app_name": "my-app",                    // already set
  "binary_name": "my-app",                 // override if your binary differs

  "description": "",                       // ← FILL THIS IN

  "metrics_port": 9090,                    // change if your app uses a different port
  "health": {
    "liveness_path": "/healthz",
    "readiness_path": "/readyz",
    "metrics_path": "/metrics"
  },

  "env_prefix": "MY_APP",                  // SCREAMING_SNAKE form of app name
  "metric_prefix": "my_app",               // snake form (Prometheus namespace)
  "config_mount_path": "/etc/my-app/my-app.yaml",

  // Fields you'll often want to add:

  "extra_ports": [
    { "name": "http", "port": 8080, "protocol": "TCP" }
  ],

  "secrets": [
    {
      "group_name": "kafka",
      "env_vars": [
        { "env_var": "MY_APP__KAFKA__PASSWORD",
          "key_name": "password",
          "secret_key": "kafka-password" }
      ]
    }
  ],

  "depends_on": ["kafka", "clickhouse"],   // for docker-compose dev

  "keda": {                                // omit entirely if not using KEDA
    "min_replicas": 1,
    "max_replicas": 10,
    "kafka_lag_threshold": 1000,
    "cpu_threshold": 80
  },

  "image_profile": "production",           // or "development"

  "oci_labels": {
    "title": "My App",
    "description": "Short description for the OCI image"
  },

  "native_deps": {                         // apt packages your runtime needs
    "apt_packages": ["libssl3", "zlib1g"]
  }
}
```

Validate as you go:

```bash
hyperi-ci emit-artefacts /tmp/check --from ci/deployment-contract.json
```

If the contract has a schema error, the Pydantic validator will print
the field path and reason. No artefacts are written until validation
passes.

## Step 3 — Generate the artefacts

> **Status:** the templating engine itself is Phase 2 of the plan and
> blocked on hyperi-rustlib 2.8.0 shipping the parity fixture suite.
> Until then, `emit-artefacts` returns exit code 5
> (`EXIT_NOT_IMPLEMENTED`) after validating the contract. The
> command's CLI surface, exit-code contract, and tier dispatch are
> stable — once Phase 2 lands, the same flow produces real files.

When Phase 2 lands:

```bash
hyperi-ci emit-artefacts ci/
```

Writes (under `ci/`):

- `Dockerfile` — for local `docker build .`
- `Dockerfile.runtime` — fragment CI composes with
- `container-manifest.json` — image name, platforms, OCI labels
- `argocd-application.yaml` — ArgoCD `Application` CR
- `chart/` — Helm chart directory
- `deployment-contract.schema.json` — schema reference (for editor support)

Commit everything under `ci/` to git. The repo's committed `ci/` exists
for visibility (PR diffs, ops review) and drift detection — CI builds
use the freshly regenerated `ci-tmp/`, so a stale commit can't poison
a release.

## Step 4 — Wire CI

Add `.hyperi-ci.yaml` (or update the existing one):

```yaml
publish:
  container:
    enabled: auto    # detection picks Tier 3 because ci/deployment-contract.json exists
    platforms:
      - linux/amd64
      - linux/arm64
```

The next push triggers:

1. **Quality** — drift check: regenerates to `/tmp/drift/`, diffs against
   `ci/`. Fails if you edited the contract without re-running
   `emit-artefacts`.
2. **Generate** — regenerates fresh artefacts to `ci-tmp/`.
3. **Container** — builds the Dockerfile from `ci-tmp/`, pushes to GHCR.

> **Status:** the CI workflow YAML changes (Phase 5.4–5.7) are also
> staged behind Phase 2. The `generate` stage handler is wired into
> `dispatch.py` and callable via `hyperi-ci run generate`, but the
> reusable workflows don't yet add it as a job.

## Step 5 — Remove the old Dockerfile (optional)

If you had a hand-rolled `Dockerfile` at the repo root, you can keep it
as long as it matches `ci/Dockerfile` byte-for-byte (the drift check
covers `ci/`, not the root). Most projects delete the root Dockerfile
once Tier 3 is in place — `docker build .` from the root then uses
`ci/Dockerfile` via the `dockerfile:` field in `.hyperi-ci.yaml`:

```yaml
publish:
  container:
    dockerfile: ci/Dockerfile
```

## Step 6 — Iterate

Whenever you change deployment-relevant config (a new env var, new
secret, new port, KEDA threshold), edit `ci/deployment-contract.json`
and run:

```bash
hyperi-ci emit-artefacts ci/
git add ci/
git commit -m "fix: update deployment contract for X"
```

The drift check in CI then enforces that you remembered the
`emit-artefacts` step. If you forget, the next push fails Quality with
a clear "drift detected — run hyperi-ci emit-artefacts ci/" message.

## Troubleshooting

**"contract validation failed: Field required"**  
A required field is missing. Pydantic's error message includes the
field path. Most often: `app_name`, `metrics_port`, `health`,
`env_prefix`, `metric_prefix`, `config_mount_path`. The scaffolder
sets all of these — if you're seeing this, the contract was hand-edited
into an invalid state.

**"contract declares schema_version=N but this hyperi-ci supports up to M"**  
Your contract was emitted by a newer producer than this hyperi-ci.
Run `uv tool upgrade hyperi-ci` (or `hyperi-ci upgrade`) and retry.

**"Generate (Tier 3): emit-artefacts: artefact templater is not yet implemented"**  
Expected until Phase 2 ships. Track [the plan](../superpowers/plans/2026-04-30-deployment-contract-three-tier.md)
to see when that lands.

**"Drift check: artefacts under ci/ drift from the contract"**  
You edited `ci/deployment-contract.json` without re-running
`hyperi-ci emit-artefacts ci/`. Re-run it and commit the regenerated
files.

## See also

- [User guide](../deployment-contract.md) — concept + reference
- [Spec](../superpowers/specs/2026-04-30-deployment-contract-three-tier-design.md)
- [Plan](../superpowers/plans/2026-04-30-deployment-contract-three-tier.md)
