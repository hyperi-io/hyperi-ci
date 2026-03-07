# ARC Runner Image & Deployment

Self-hosted GitHub Actions runners via ARC (Actions Runner Controller) on the
RKE2 cluster. All runners use a single Docker image with pre-baked build
toolchains — pods are ephemeral and scale to zero when idle.

## Architecture

```
harbor.devex.hyperi.io:8443/library/arc-runner:latest
    ↓ imagePullPolicy: Always
arc-runner-2cpu   (2 cores,  4 GB)  — lint, test, publish
arc-runner-4cpu   (4 cores,  8 GB)  — Python, Node builds
arc-runner-8cpu   (8 cores, 16 GB)  — standard builds
arc-runner-16cpu  (16 cores, 28 GB) — C++/Rust release builds
```

All sizes share the same image. Size YAML files only differ in resource
requests/limits and maxRunners.

## Key Files

All runner infrastructure lives in `hyperi-io/hyperi-infra`:

| File | Purpose |
|------|---------|
| `containers/arc-runner/Dockerfile` | Runner image definition |
| `k8s/arc-runner-set-values.yaml` | Shared Helm base values |
| `k8s/arc-runner-{2,4,8,16}cpu.yaml` | Per-size resource config |
| `ansible/playbooks/k8s-arc-runners.yml` | Build + deploy playbook |

## Pre-baked Tools

The runner image includes everything HyperI CI workflows need. Workflows
use `command -v <tool> || install <tool>` so they still work on
`ubuntu-latest` (free mode) where tools aren't pre-installed.

### Languages & Package Managers
- Python 3.12 + uv
- Node.js 22 LTS + npm + pnpm (corepack)
- Go (latest stable) at `/usr/local/go/bin`
- Rust stable + nightly at `/opt/rust/`

### Rust Toolchain
- Components: clippy, rustfmt
- Targets: x86_64-unknown-linux-gnu (native), aarch64-unknown-linux-gnu (cross)
- Cargo tools: cargo-audit, cargo-deny, cargo-nextest, sccache
- Cross-compilation: gcc-aarch64-linux-gnu, g++-aarch64-linux-gnu, libc6-dev-arm64-cross
- Native deps: librdkafka-dev, libssl-dev, libsasl2-dev, libcurl4-openssl-dev, protobuf-compiler

### Go Quality Tools
- golangci-lint, gosec, govulncheck (installed to `/opt/go/bin`)

### C/C++ Toolchain
- LLVM/Clang 19, 20 (19 default via update-alternatives)
- mold linker (default for native builds via LDFLAGS in runner YAML)
- cmake, ninja-build, ccache

### CI/CD
- semantic-release + all plugins (commit-analyzer, release-notes-generator, changelog, exec, git, github)
- GitHub CLI (gh)
- Docker CLI (DinD sidecar provides daemon)

### Security
- gitleaks, trivy, hadolint, shellcheck, actionlint

## Build Cache Strategy

| Cache type | Mount | Reason |
|-----------|-------|--------|
| sccache, ccache | NFS PVC (`/mnt/cache/`) | Compilation cache persists across ephemeral pods |
| cargo, uv, pip, npm | Local emptyDir | Metadata-heavy I/O not suited to NFS |

The NFS PVC is backed by SSD (nvme-fast StorageClass) on the storage VM.

## Runner Environment Variables

Set in each `arc-runner-{size}.yaml`, not the Dockerfile:

- `CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS="-C link-arg=-fuse-ld=mold"` — native Rust uses mold
- `LDFLAGS="-fuse-ld=mold"` — native C/C++ uses mold
- `RUSTC_WRAPPER="sccache"` — all Rust compilation goes through sccache

**Cross-compilation note:** The `LDFLAGS` mold setting will break cross
targets. The hyperi-ci `build.py` clears `LDFLAGS`, `CFLAGS`, `CXXFLAGS`
when setting up cross-compilation environments (per CI-LESSONS.md).

## Operations

### Rebuild runner image

When the Dockerfile changes (new tools, version bumps):

```bash
# Via Ansible (from a host with the right SSH key path)
ansible-playbook -i inventories/prod/inventory.yml \
  playbooks/k8s-arc-runners.yml --tags image

# Direct (from desktop-derek where SSH key is at ~/.ssh/devex-ssh)
scp containers/arc-runner/Dockerfile ubuntu@infra.devex.hyperi.io:/tmp/
ssh ubuntu@infra.devex.hyperi.io
  cd /tmp && mkdir -p arc-runner-build && cp Dockerfile arc-runner-build/
  sudo docker build -t harbor.devex.hyperi.io:8443/library/arc-runner:latest arc-runner-build/
  sudo docker login harbor.devex.hyperi.io:8443
  sudo docker push harbor.devex.hyperi.io:8443/library/arc-runner:latest
```

New pods automatically pick up the new image (`imagePullPolicy: Always`).
No Helm redeploy needed for image-only changes.

### Redeploy runner scale sets

When Helm values change (resource limits, env vars, maxRunners):

```bash
ansible-playbook -i inventories/prod/inventory.yml \
  playbooks/k8s-arc-runners.yml --tags deploy

# Or a single size:
ansible-playbook -i inventories/prod/inventory.yml \
  playbooks/k8s-arc-runners.yml --tags deploy -e 'arc_runner_sizes=["16cpu"]'
```

### Verify runners are healthy

```bash
ssh ubuntu@k8s-1.devex.hyperi.io
sudo /var/lib/rancher/rke2/bin/kubectl --kubeconfig /etc/rancher/rke2/rke2.yaml \
  get pods -n arc-runners
sudo /var/lib/rancher/rke2/bin/kubectl --kubeconfig /etc/rancher/rke2/rke2.yaml \
  get pods -n arc-system -l app.kubernetes.io/component=runner-scale-set-listener
```

Runners scale to zero when idle — no pods visible is normal.

## Ansible SSH Key Note

The inventory references `/projects/hyperi-infra/.ssh/devex-ssh` but on
`desktop-derek` the key lives at `~/.ssh/devex-ssh`. Either symlink or
override with `--private-key ~/.ssh/devex-ssh` when running playbooks
from this workstation.
