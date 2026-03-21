# hyperi-ci

Polyglot CI/CD tool for HyperI projects. Python, Rust, TypeScript, and
Go — one CLI, same behaviour locally and in CI.

## Background

The previous CI system (`hyperi-io/ci`) grew organically from its GitLab
origins — running both local and cloud GitLab concurrently — before
migrating to GitHub Actions. That evolution produced ~100 shell scripts,
26 Python scripts, 50+ composite actions, a 1020-line `attach.sh`, and
delivery via git submodule across 14+ consumer projects. The dispatch
hierarchy reached six layers deep (workflow → action → bash → Python →
bash → tools), with config settable in four different places and
significant dead code accumulated along the way.

This repo rationalises everything back into a single Python CLI tool,
distributed as a standard package via `uv tool install`. Consumer
projects get a five-line reusable workflow and a Makefile — no
submodules, no composite actions, no bash dispatch chains. The same tool
runs locally and in GitHub Actions.

The old repo will be archived read-only once all consumer projects have
cut over.

## Install

```bash
uv tool install hyperi-ci
```

## Quick Start

Initialise an existing project:

```bash
cd my-project
hyperi-ci init                          # Auto-detects language
hyperi-ci init --language rust          # Override language
hyperi-ci init --force                  # Overwrite existing files
```

This generates `.hyperi-ci.yaml`, `Makefile`, `.github/workflows/ci.yml`,
and `.releaserc.yaml`. Commit and push — CI runs automatically.

## Usage

### Local Validation (Pre-Push)

Run local checks before pushing — same tool, same code path as CI:

```bash
hyperi-ci check                        # Quality + test (default)
hyperi-ci check --quick                # Quality only (fast)
hyperi-ci check --full                 # Quality + test + build (native target only)
```

`check` stops on first failure. When `--full` includes the build
stage, cross-compilation targets are skipped — only the native host
target is built. Cross-compilation needs CI-specific toolchains and
is validated in CI, not locally.

### CI Stages

```bash
hyperi-ci run quality                   # Lint, format, type check, audit
hyperi-ci run test                      # Run test suite
hyperi-ci run build                     # Build artifacts
hyperi-ci run publish                   # Publish (CI only)
```

### Project Info

```bash
hyperi-ci detect                        # Show detected language
hyperi-ci config                        # Show merged config as JSON
```

### GitHub Actions Commands

```bash
hyperi-ci trigger                       # Trigger workflow run
hyperi-ci trigger --watch               # Trigger and watch to completion
hyperi-ci watch                         # Watch latest run
hyperi-ci watch <RUN_ID>                # Watch specific run
hyperi-ci logs                          # Show latest run logs
hyperi-ci logs --failed                 # Show only failed job logs
hyperi-ci logs --job build --grep error # Filter by job and pattern
```

### Via Makefile (Consumer Projects)

```bash
make quality
make test
make build
make check                             # Runs hyperi-ci check
```

## How It Works

GitHub Actions handles orchestration (job ordering, matrix, caching,
secrets). The CLI handles execution (what tools to run, how to invoke
them). Workflow files stay small, and the same code path runs locally
and in CI.

```
Consumer Project                     hyperi-ci (this repo)
├── .github/workflows/               ├── .github/workflows/
│   └── ci.yml (5 lines)            │   ├── python-ci.yml   (reusable)
│       uses: hyperi-io/hyperi-ci/   │   ├── rust-ci.yml     (reusable)
│         .github/workflows/         │   ├── ts-ci.yml       (reusable)
│         python-ci.yml@v1.0         │   └── go-ci.yml       (reusable)
├── .hyperi-ci.yaml                  │           │
├── Makefile                         │           ▼
│   quality: hyperi-ci run quality   │   uvx hyperi-ci run <stage>
│   test:    hyperi-ci run test      │           │
│   build:   hyperi-ci run build     │           ▼
└── .releaserc.yaml                  ├── src/hyperi_ci/
                                     │   ├── cli.py          (entry point)
                                     │   ├── config.py       (config cascade)
                                     │   ├── dispatch.py     (stage dispatcher)
                                     │   ├── detect.py       (language detection)
                                     │   └── languages/
                                     │       ├── python/     quality, test, build, publish
                                     │       ├── rust/       quality, test, build, publish
                                     │       ├── typescript/ quality, test, build, publish
                                     │       └── golang/     quality, test, build, publish
                                     └── config/
                                         ├── org.yaml        (JFrog, GHCR URLs)
                                         └── defaults.yaml   (default settings)
```

See [docs/DESIGN.md](docs/DESIGN.md) for full architecture documentation.

## Config

Single source of truth: `.hyperi-ci.yaml` in the project root.

Cascade priority (highest wins):
```
CLI flags → ENV vars (HYPERCI_*) → .hyperi-ci.yaml → defaults.yaml → hardcoded
```

### Publish Target

Controls whether artifacts go to JFrog (internal) or public registries (OSS).
Set via GitHub org variable `PUBLISH_TARGET` — repo-level variable overrides org.

| Value | Destination |
|-------|-------------|
| `internal` | JFrog Artifactory (default) |
| `oss` | PyPI, npm, crates.io, GHCR, GitHub Releases |
| `both` | Publish to both |

## Languages

| Language | Quality | Test | Build | Publish |
|----------|---------|------|-------|---------|
| Python | ruff, pyright, bandit, pip-audit | pytest | uv build | uv publish |
| Rust | cargo fmt, clippy, audit, deny | cargo test/nextest | cargo build (cross-compile) | cargo publish |
| TypeScript | eslint, prettier, tsc, npm audit | vitest/jest | npm/pnpm build | npm publish |
| Go | gofmt, go vet, golangci-lint, gosec | go test -race | go build (cross-compile) | go proxy, gh release |

## Cross-Compilation

Rust projects with C/C++ dependencies (e.g. librdkafka) are supported.
The build handler automatically sets `CC`, `CXX`, `AR`, and
`PKG_CONFIG` environment variables for cross-compilation targets.
Configure targets in `.hyperi-ci.yaml`:

```yaml
build:
  rust:
    targets:
      - x86_64-unknown-linux-gnu
      - aarch64-unknown-linux-gnu
```

## Release Channels

Multi-channel semantic-release for staged rollout. Every project gets
`main` (dev) and `release` (GA) by default. Projects with experimental
components can add `alpha` and `beta` channels.

### Channel Model

| Branch | Pre-release Tag | Stability | Example Version |
|--------|----------------|-----------|-----------------|
| `main` | `-dev.N` | Internal dev builds | `v0.2.0-dev.3` |
| `alpha` | `-alpha.N` | Early adopter, API may break | `v0.2.0-alpha.1` |
| `beta` | `-beta.N` | Feature-complete, API freezing | `v0.2.0-beta.2` |
| `release` | (none) | GA stable | `v0.2.0` |

### Setup

```bash
# Default: main + release (two-channel)
hyperi-ci init-release

# Add alpha channel
hyperi-ci init-release --channels alpha

# Add alpha + beta channels
hyperi-ci init-release --channels alpha,beta

# Check current setup
hyperi-ci init-release --check
```

### Releasing

```bash
# Merge main into release (GA)
hyperi-ci release-merge

# Merge main into alpha
hyperi-ci release-merge --base alpha

# Merge main into beta
hyperi-ci release-merge --base beta
```

Each merge creates a PR. Merging the PR triggers semantic-release on
that channel's branch, which creates the appropriate pre-release tag.

### Graduation Flow

```
main (dev) ──> alpha ──> beta ──> release (GA)
```

Use `release-merge --base <channel>` at each stage. Semantic-release
handles version numbering automatically — no manual version bumps.

## Design Principles

1. **NO BASH** — all CI logic is Python. 70% of old CI failures were
   bash syntax issues (env expansion, quoting, jq). `subprocess.run()`
   with list args only.
2. **Semantic release centric** — push to main, semantic-release creates
   tags and publishes. No manual version bumps.
3. **uv for everything** — venv, sync, lock, tool install, build.
4. **Cross-platform** — Linux (CI runners) and macOS (dev laptops).
5. **Self-hosting** — hyperi-ci uses itself for its own CI.

## Test Projects

Minimal projects for fast CI iteration and handler validation:

| Language | Path | Notes |
|----------|------|-------|
| Rust | `test-projects/ci-test-rust-minimal/` | Binary with C/C++ deps (librdkafka) |
| Python | `test-projects/ci-test-python-minimal/` | Zero-dep package |
| TypeScript | `test-projects/ci-test-ts-minimal/` | Zero-dep package |
| Go | `test-projects/ci-test-go-minimal/` | Zero-dep binary |

## Licence

Proprietary — HYPERI PTY LIMITED
