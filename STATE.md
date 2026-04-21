# HyperI CI — Project State

Static context for AI assistants. For tasks and progress see `TODO.md`.

## Dep-Install SSOT (Single Source of Truth)

**hyperi-ci is the SSOT for all apt-driven dependency installation across
the HyperI toolchain.** Both the ARC runner image bake (in `hyperi-infra`)
and per-project CI-time installs (on vanilla GH runners) use the same
YAML data and the same install code path — just in different modes.

- YAML: `config/toolchains/*.yaml` (multi-version apt families) +
  `config/native-deps/*.yaml` (per-language conditional deps)
- Driver: `src/hyperi_ci/native_deps.py`
- CLI: `hyperi-ci install-toolchains [--all]` and
  `hyperi-ci install-native-deps <lang> [--all]`
- Standard for non-coinstallable toolsets: `bake: false` — skipped in
  `--all` (runner image), installed on-demand at CI job time
- Full architecture + cross-project flow: `docs/ARC-RUNNERS.md`

### Cross-project responsibilities

| Repo | Role in the SSOT flow |
|------|----------------------|
| **hyperi-ci** (this repo) | Owns YAML + driver. Published to PyPI. Bump version = new runner image build needed. |
| **hyperi-pylib** | Runtime dep (logger, config cascade). Bumping it = bumping hyperi-ci at next release. |
| **hyperi-infra** | Owns runner image Dockerfiles (`containers/arc-runner*/Dockerfile`) that `pip install hyperi-ci` and call `install-toolchains --all`. Pushes to `harbor.devex.hyperi.io:8443`. Ansible playbook: `ansible/playbooks/k8s-arc-runners.yml --tags image`. |
| **dfe-receiver** | Canary 1 — exercises BOLT/PGO flow, touches most of the surface. |
| **dfe-loader** | Canary 2 — ClickHouse/Arrow deps, broader apt surface. |
| Other dfe-* + hyperi-rustlib | Broader rollout after both canaries land clean. |

## Background

Ground-up rewrite of the HyperI CI system. The previous CI (`hyperi-io/ci`)
grew organically from GitLab origins, producing ~100 shell scripts, 50+
composite actions, a 1020-line `attach.sh`, and delivery via git submodule
across 14+ consumer projects. Six-layer dispatch hierarchy, config settable
in four places, significant dead code.

This repo rationalises everything into a single Python CLI tool (`hyperi-ci`),
distributed via `uv tool install`. Consumer projects get a five-line reusable
workflow and a Makefile. Same tool runs locally and in CI.

The old repo (`hyperi-io/ci`) will be archived once all consumer projects
have cut over. **Reference the old CI** at `/projects/ci` for proven patterns
before reinventing — it has working solutions for system deps, cross-compilation
sysroots, cargo registry config, and binary verification.

**MANDATORY: Read `docs/CI-LESSONS.md` before implementing or debugging any CI
handler.** It contains extracted patterns, gotchas, and solutions from the old
CI. Ignoring it wastes time rediscovering known problems (e.g. mold linker,
multi-arch package conflicts, sysroot approach, integration test threading).

## Hard Design Principles

1. **NO BASH** — all CI logic is Python. `subprocess.run()` with list args.
2. **Semantic release centric** — push to main, semantic-release creates tags.
3. **uv for everything** — venv, sync, lock, tool install, build.
4. **Cross-platform** — Linux (CI) and macOS (dev). Uses `pathlib`, `shutil.which()`.
5. **Self-hosting** — hyperi-ci uses itself for its own CI.
6. **Publish target routing** — `PUBLISH_TARGET` = internal/oss/both controls where artifacts go.

## Architecture

See `docs/DESIGN.md` for full architecture documentation.

```
src/hyperi_ci/
├── cli.py               # Typer CLI (run, check, push, init, detect, config, trigger, watch, logs, release, check-commit)
├── config.py            # CIConfig, OrgConfig, config cascade loader
├── common.py            # Logging, subprocess helpers, GH Actions output
├── detect.py            # Language detection from file markers
├── dispatch.py          # Stage dispatcher → language handlers
├── init.py              # Project scaffolding (config, Makefile, workflow, releaserc, githooks)
├── release.py           # Tag-based publish dispatch (replaces release-merge)
├── publish_binaries.py  # GH Release creation + R2/JFrog binary upload
├── gh.py                # GitHub CLI helpers
├── push.py              # Push wrapper (pre-checks, --release, --no-ci)
├── trigger.py           # Workflow trigger command
├── watch.py             # Run watch command
├── logs.py              # Log fetch command
├── quality/
│   ├── gitleaks.py      # Secret scanning
│   └── commit_validation.py  # Conventional commit enforcement
└── languages/
    ├── python/          # quality, test, build, publish
    ├── rust/            # quality, test, build, publish
    ├── typescript/      # quality, test, build, publish
    └── golang/          # quality, test, build, publish
```

## Handler Interface

Every language handler module exports:

```python
def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Run the stage. Returns exit code (0 = success)."""
```

Dispatch finds handlers via `hyperi_ci.languages.<lang>.<stage>` module path.

## Config Cascade

Priority (highest wins):
```
CLI flags → ENV vars (HYPERCI_*) → .hyperi-ci.yaml → config/defaults.yaml → hardcoded
```

## Key Files

| File | Purpose |
|------|---------|
| `VERSION` | Source of truth for version |
| `config/org.yaml` | Organisation-specific config (JFrog, GitHub, GHCR) |
| `config/defaults.yaml` | Default values for all CI settings |
| `config/commit-types.yaml` | SSOT for commit types and semantic-release rules |
| `config/versions.yaml` | SSOT for action/runtime/tool versions |
| `config/secrets-access.yaml` | Group-based org secret visibility management |
| `pyproject.toml` | Package config, deps, tool config |
| `uv.lock` | Locked dependencies (committed) |
| `.releaserc.yaml` | Semantic release config |
| `.github/workflows/ci.yml` | Self-hosting CI workflow |
| `.github/workflows/rust-ci.yml` | Reusable Rust CI workflow |
| `scripts/update-versions.py` | Version sync/update script |
| `scripts/sync-secrets-access.py` | Secret repo access sync script |
| `docs/DESIGN.md` | Full architecture documentation |
| `docs/CI-LESSONS.md` | Lessons from old CI — MUST READ before handler work |

## Commands

```bash
uv sync                              # Install deps
uv run pytest tests/ -v              # Run tests
uv run ruff check src/ tests/        # Lint
uv run ruff format src/ tests/       # Format
uv run hyperi-ci --version           # Verify CLI
uv run hyperi-ci detect              # Language detection
uv run hyperi-ci config              # Show merged config
uv run hyperi-ci init                # Scaffold a project
uv run hyperi-ci check               # Pre-push: quality + test
uv run hyperi-ci check --full        # Pre-push: quality + test + build (native only)
uv run hyperi-ci check --quick       # Pre-push: quality only
uv run hyperi-ci push                # Check, rebase, push (NEVER use bare git push)
uv run hyperi-ci push --release      # Push + auto-publish if CI passes
uv run hyperi-ci push --no-ci        # Push, skip CI
uv run hyperi-ci release --list      # List unpublished version tags
uv run hyperi-ci release v1.3.0      # Trigger publish for a tag
uv run hyperi-ci check-commit --list # List accepted commit types
```

## Versioning and Publishing

**Single versioning on main.** Semantic-release runs only on `main`, producing
real versions (`1.3.0`, not `1.3.0-dev.8`). No release branch.

**Publish is explicit.** `hyperi-ci release <tag>` dispatches a workflow that
builds from the tag and publishes. Not every version needs to be published.

**Channels** control where artifacts go (`publish.channel` in `.hyperi-ci.yaml`):
- `spike` / `alpha` / `beta` — GH Release (prerelease), R2 channel path, no registries
- `release` — GH Release (GA), R2 versioned path, PyPI/crates.io/npm

**Commit validation** enforced by `.githooks/commit-msg` hook and CI quality stage.
Invalid messages get "Computer says no." with friendly guidance.

See `docs/MIGRATION-GUIDE.md` for migrating projects to this model.

## Consumer Projects

### On Single Versioning (migrated)

- **hyperi-rustlib** — Rust library, crates.io (`publish.channel: release`)
- **hyperi-pylib** — Python library, PyPI (`publish.channel: release`)

### On hyperi-ci (migrating to single versioning)

- **dfe-receiver** — Rust binary, GH Releases + R2
- **dfe-loader** — Rust binary, GH Releases + R2
- **dfe-archiver** — Rust binary, 3-crate workspace, GH Releases + R2
- **dfe-fetcher** — Rust binary, GH Releases + R2
- **dfe-engine** — Python app, JFrog PyPI
- **dfe-transform-vrl/elastic/vector/wasm** — Rust binaries

### Deprecated (Do Not Migrate)

- **dfe-kafka-topic-scaler** — to be archived
- **dfe-control-plane** — to be archived
- **dfe-plugin-loader** — plugin system removed, sidecar pattern instead
- **dfe-protocol-sdk** — plugin system removed
- **dfe-receiver-plugin-syslog** — syslog is built-in transport

## Licensing

Proprietary — HYPERI PTY LIMITED.
