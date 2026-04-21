# ARC Runner Image & Dep-Install SSOT

Self-hosted GitHub Actions runners via ARC (Actions Runner Controller) on the
RKE2 cluster. **hyperi-ci is the single source of truth for all apt-driven
dependency installation** — runner image builds consume it via PyPI, CI jobs
on vanilla GH runners consume it via `pip install hyperi-ci`. One data
format, one install code path, two invocation modes.

## Cross-Project Dependency Graph

```
            ┌─────────────────────────────────────────────┐
            │  hyperi-ci  (this repo)                     │
            │  ├── config/toolchains/*.yaml (LLVM, GCC)   │
            │  ├── config/native-deps/*.yaml (per-lang)   │
            │  └── src/hyperi_ci/native_deps.py (driver)  │
            │  Published: PyPI (hyperi-ci package)        │
            └────────────┬──────────────────┬─────────────┘
                         │                  │
         `hyperi-ci       │                  │   `pip install hyperi-ci`
         install-         │                  │   `hyperi-ci install-*`
         toolchains       │                  │   (conditional per-project)
         --all`           │                  │
         (runner bake)    ▼                  ▼
  ┌────────────────────────────┐   ┌───────────────────────────┐
  │  hyperi-infra              │   │  dfe-receiver (canary 1)  │
  │  containers/arc-runner/    │   │  dfe-loader (canary 2)    │
  │  containers/arc-runner-    │   │  vanilla GH runners (arm) │
  │    debian/ Dockerfile      │   │  → hyperi-ci auto-installs│
  │  → runner image in Harbor  │   │    only what the project  │
  │    harbor.devex.hyperi.io  │   │    manifest triggers      │
  │    :8443/library/          │   └───────────────────────────┘
  │    arc-runner[-debian]     │
  └────────────────────────────┘

  hyperi-pylib is a runtime dep of hyperi-ci (logger, config cascade, etc.)
  — bumping it = bumping hyperi-ci at next release.
```

## Two Invocation Modes

| Mode | Who uses it | Behaviour |
|------|-------------|-----------|
| `hyperi-ci install-toolchains --all`<br>`hyperi-ci install-native-deps <lang> --all` | Runner image bake (hyperi-infra Dockerfile) | Install every entry unconditionally. Ignores manifest patterns. Entries with `bake: false` are skipped (see "Non-coinstallable toolsets" below). |
| `hyperi-ci install-toolchains`<br>`hyperi-ci install-native-deps <lang>` | CI-time on vanilla `ubuntu-latest` or arm64 GH runners | Conditional. Install only entries whose `patterns` match files named in `manifest_files` in the project. |

## YAML Schema

Shared across `config/native-deps/*.yaml` (per-language conditional deps) and
`config/toolchains/*.yaml` (multi-version apt families).

```yaml
- name: <label for log lines>
  bake: true                        # optional, default true; see below
  versions: [19, 20, 21, 22]        # optional; expands {V} into N entries
  patterns:                         # substrings searched in manifest_files
    - "Cargo.toml"
    - "CMakeLists.txt"
  manifest_files:                   # relative to project root
    - Cargo.toml
    - CMakeLists.txt
    - .hyperi-ci.yaml
  dpkg_check: clang-{V}             # skip if dpkg -s succeeds
  apt_repos:                        # optional repos to add before install
    - key_url: https://apt.llvm.org/llvm-snapshot.gpg.key
      keyring: /usr/share/keyrings/llvm.gpg
      url: https://apt.llvm.org/${OS_CODENAME}/
      codename: llvm-toolchain-${OS_CODENAME}-{V}
  apt_packages:
    - clang-{V}
    - clang-tools-{V}
    - bolt-{V}
```

### Template variables

| Placeholder | Source | Example |
|-------------|--------|---------|
| `{V}` | Per-version expansion (when `versions:` is set) | `19`, `20`, `21`, `22` |
| `${OS_CODENAME}` | `lsb_release -cs` or `OS_CODENAME` env var | `noble`, `trixie`, `resolute` |
| `${HYPERCI_LLVM_VERSION}` | `HYPERCI_LLVM_VERSION` env var (default `22`) | Used by native-deps/rust.yaml for BOLT version pin |

### `bake` flag (non-coinstallable toolsets — the standard)

When an apt package declares `Conflicts: <package>-x.y`, **only one version
may be installed at a time**. Examples on apt.llvm.org: `libc++-N-dev`,
`libc++abi-N-dev`, `libomp-N-dev`, `libunwind-N-dev`, and `lldb-N` via its
`python3-lldb-N` dep. Baking a default would lock out any CI job needing a
different version.

Pattern: put the non-coinstallable packages in a **single entry with
`bake: false`**. It becomes install-on-demand only — the runner image skips
it (`--all` ignores `bake: false`), and CI-time installs apply it
conditionally when project patterns match.

```yaml
- name: llvm-non-coinstallable
  bake: false                       # skipped in --all; installed on-demand
  patterns: ["Cargo.toml", "CMakeLists.txt"]
  manifest_files: [Cargo.toml, CMakeLists.txt, .hyperi-ci.yaml]
  dpkg_check: libc++-22-dev
  apt_repos: [...]                  # apt.llvm.org for v22
  apt_packages:
    - lldb-22
    - libc++-22-dev
    - libc++abi-22-dev
    - libomp-22-dev
    - libunwind-22-dev
```

This pattern applies to **any** toolset — not just LLVM. Future families
(GCC beta versions, JDK preview builds, etc.) follow the same convention.

## What the Runner Image Contains

The `containers/arc-runner/Dockerfile` (Ubuntu noble) and
`containers/arc-runner-debian/Dockerfile` (Debian trixie) both do:

```dockerfile
RUN pip install --no-cache-dir --break-system-packages 'hyperi-ci>=1.12' && \
    OS_CODENAME=noble hyperi-ci install-toolchains --all
```

This produces the following pre-baked toolchains per the shipped YAML:

### LLVM (coinstallable v19/20/21/22)
`clang-N`, `clang-tools-N`, `clangd-N`, `lld-N`, `llvm-N`, `llvm-N-dev`,
`llvm-N-tools`, `libclang-N-dev`, `libclang-rt-N-dev`, `bolt-N`

### GCC (coinstallable v13/14)
`gcc-N`, `g++-N`, `libstdc++-N-dev`

### Default `clang`, `lld`, `ld.lld` alternatives
Point at v19 (ClickHouse OSS compatibility). BOLT's cargo-pgo flow invokes
the unversioned `ld.lld`; hyperi-ci's `_ensure_llvm_bolt_available()` in
`languages/rust/pgo.py` shims versioned binaries into `~/.local/bin` at
runtime when a specific `HYPERCI_LLVM_VERSION` is requested.

### Skipped at image bake (install-on-demand)
`lldb-22`, `libc++-22-dev`, `libc++abi-22-dev`, `libomp-22-dev`,
`libunwind-22-dev` — `bake: false` entries. Jobs that need them incur a
~5s apt-get at runtime. Projects that need a different version install
theirs themselves.

### Still baked inline in Dockerfile
Bootstrap packages (`python3`, `python3-pip`, `curl`, `gnupg`,
`ca-certificates`), the internal CA chain, base apt packages
(`build-essential`, `cmake`, `ninja-build`, `mold`, …), Python/Rust/Node
runtimes, CI tool binaries (`gh`, `hadolint`, `shellcheck`, `actionlint`),
arm64 cross-compile sources.

**Migrating these into hyperi-ci is phases 2-5** (tracked in TODO.md).

## Cross-Project Rollout Flow

Step-by-step flow when a dep-install change lands:

1. **hyperi-ci**: branch + edit `config/*.yaml` or `native_deps.py`
2. **hyperi-ci**: open PR, merge to main, semantic-release tags `vX.Y.Z`
3. **hyperi-ci**: `hyperi-ci release vX.Y.Z` dispatches publish workflow → PyPI
4. **hyperi-infra**: if the pin needs bumping to force Docker cache-miss,
   edit both `containers/arc-runner/Dockerfile` and
   `containers/arc-runner-debian/Dockerfile` (`'hyperi-ci>=X.Y'`), commit
5. **hyperi-infra**: run
   ```
   ansible-playbook -i inventories/prod/inventory.yml \
     playbooks/k8s-arc-runners.yml --tags image \
     -e harbor_admin_password=$(scripts/bao-admin kv get -field=admin_password kv/services/harbor)
   ```
   Builds Ubuntu + Debian variants, pushes to `harbor.devex.hyperi.io:8443`.
6. **canary**: `dfe-receiver` is the first downstream consumer. New runner
   pods pull `:latest` (imagePullPolicy Always), so next job run uses the
   new image. Watch for issues with the BOLT flow specifically — cargo-pgo
   exercises most of the new surface.
7. **second canary**: `dfe-loader` — same shape as receiver, different deps
   (ClickHouse-client, Arrow, columnar). Broader apt surface.
8. **broader rollout**: `dfe-archiver`, `dfe-fetcher`, `hyperi-rustlib`,
   `hyperi-pylib`, the transform projects.

Each canary produces concrete feedback about missing coverage or apt
conflicts; iterate on hyperi-ci YAML accordingly.

## Operations

### Build + push runner images (one step)

```bash
# From hyperi-infra checkout
env -C /projects/hyperi-infra \
  ansible-playbook -i ansible/inventories/prod/inventory.yml \
  ansible/playbooks/k8s-arc-runners.yml --tags image \
  -e harbor_admin_password=$(scripts/bao-admin kv get -field=admin_password kv/services/harbor)
```

Takes ~25-30 min. Builds both `arc-runner:latest` and
`arc-runner-debian:latest`, pushes to Harbor.

New scale-set pods pick up the new image automatically on next job spawn
(`imagePullPolicy: Always`). No Helm redeploy needed for image-only changes.

### Redeploy runner scale sets (Helm values changes)

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

Scale-to-zero when idle — empty `arc-runners` namespace is normal.

### Verify a YAML change without a full rebuild

```bash
# Load and inspect expansion/substitution locally
OS_CODENAME=trixie uv run python -c "
from hyperi_ci.native_deps import _load_dep_groups
for g in _load_dep_groups('llvm', category='toolchains'):
    print(f'{g.name:30} bake={g.bake} packages={g.apt_packages[:3]}...')
"

# Dry-run against a specific project
uv run hyperi-ci install-toolchains --dry-run \
  --project-dir /projects/dfe-receiver
```

## Build Cache

Unchanged — sccache + ccache on NFS PVC (`/mnt/cache/`), Rust `target/`
persists in pod emptyDir, cargo/uv/pip/npm metadata caches stay local.
See the existing "Build Cache Strategy" guidance in `CI-LESSONS.md` and
runner YAML files for the full detail — that section survives intact.

## Rust Cross-Compilation Cache Integrity

Unchanged — `hyperi-ci`'s Rust `build.py` `_clean_stale_sys_crates()`
still handles wrong-arch object cleanup. See CI-LESSONS.md "ARC Persistent
Cache + Rust Cross-Compilation".
