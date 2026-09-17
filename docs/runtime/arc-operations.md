# ARC runner operations

The commands that rebuild the runner image, redeploy the scale sets, and carry a dep-install change out to the fleet.

What the image contains and the YAML that drives it are [runner-image.md](runner-image.md). Runner selection and the build cache are [runners.md](runners.md).

## Operations (hyperi-infra)

### Build + push runner images (one step)

```bash
env -C /projects/hyperi-infra \
  ansible-playbook -i ansible/inventories/prod/inventory.yml \
  ansible/playbooks/k8s-arc-runners.yml --tags image \
  -e harbor_admin_password=$(scripts/bao-admin kv get -field=admin_password kv/services/harbor)
```

Takes ~25-30 min. Builds both `arc-runner:latest` and `arc-runner-debian:latest`,
pushes to `harbor.devex.hyperi.io:8443`. New scale-set pods pick up the new image
automatically on next job spawn (`imagePullPolicy: Always`) - no Helm redeploy
needed for image-only changes.

### Redeploy runner scale sets (Helm-values changes)

```bash
ansible-playbook -i ansible/inventories/prod/inventory.yml \
  ansible/playbooks/k8s-arc-runners.yml --tags deploy
```

### Verify healthy

```bash
ssh ubuntu@k8s-1.devex.hyperi.io
sudo kubectl -n arc-runners get pods
sudo kubectl -n arc-system get pods \
  -l app.kubernetes.io/component=runner-scale-set-listener
```

Scale-to-zero when idle - an empty `arc-runners` namespace is normal.

### Verify a YAML change without a full rebuild

```bash
OS_CODENAME=trixie uv run python -c "
from hyperi_ci.native_deps import _load_dep_groups
for g in _load_dep_groups('llvm', category='toolchains'):
    print(f'{g.name:30} bake={g.bake} packages={g.apt_packages[:3]}...')
"

uv run hyperi-ci install-toolchains --dry-run \
  --project-dir /projects/dfe-receiver
```

## Rollout when a dep-install change lands

```mermaid
flowchart LR
    E["edit config/*.yaml<br/>or native_deps.py"] --> PR["PR → main →<br/>semantic-release tags"]
    PR --> PUB["hyperi-ci publish vX →<br/>PyPI"]
    PUB --> BUMP["hyperi-infra: bump<br/>'hyperi-ci>=X.Y' pin,<br/>rebuild image"]
    BUMP --> C1["canary: dfe-receiver<br/>(exercises BOLT)"]
    C1 --> C2["canary: dfe-loader<br/>(ClickHouse/Arrow surface)"]
    C2 --> ALL["broader: archiver, fetcher,<br/>scalo-rs, scalo-py, transforms"]
```

Step by step:

1. **hyperi-ci**: branch, edit `config/*.yaml` or `native_deps.py`.
2. **hyperi-ci**: open PR, merge to main, semantic-release tags `vX.Y.Z`.
3. **hyperi-ci**: `hyperi-ci publish vX.Y.Z` dispatches the publish workflow to PyPI.
4. **hyperi-infra**: to force a Docker cache-miss, bump the `'hyperi-ci>=X.Y'`
   pin in BOTH `containers/arc-runner/Dockerfile` and
   `containers/arc-runner-debian/Dockerfile`, then commit and rebuild the image
   (see Operations above).
5. **canary**: `dfe-receiver` runs first. New pods pull `:latest`
   (`imagePullPolicy: Always`), so the next job uses the new image. Watch the
   BOLT flow specifically - cargo-pgo exercises most of the new surface.
6. **second canary**: `dfe-loader` - same shape, different deps
   (ClickHouse-client, Arrow, columnar), broader apt surface.
7. **broader rollout**: `dfe-archiver`, `dfe-fetcher`, `scalo-rs`, `scalo-py`,
   the transform projects.

Each canary surfaces missing coverage or apt conflicts - iterate on the YAML.
