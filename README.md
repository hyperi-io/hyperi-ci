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

We now have a defined, mature pattern for what CI actually needs to do
across our polyglot stack. This repo rationalises everything back into a
single Python CLI tool, distributed as a standard package via
`uv tool install`. Consumer projects get a five-line reusable workflow
and a Makefile — no submodules, no composite actions, no bash dispatch
chains. The same tool runs locally and in GitHub Actions.

The old repo will be archived read-only once all consumer projects have
cut over.

## Install

```bash
uv tool install hyperi-ci
```

## Usage

```bash
# Run CI stages
hyperi-ci run quality
hyperi-ci run test
hyperi-ci run build

# Detect project language
hyperi-ci detect

# Show merged config
hyperi-ci config

# Via Makefile (consumer projects)
make quality
make test
make build
```

## How It Works

```
Consumer Project                     hyperi-ci (this repo)
├── .github/workflows/               ├── .github/workflows/
│   └── ci.yml (5 lines)            │   ├── rust-ci.yml     (reusable)
│       uses: hyperi-io/hyperi-ci/   │   ├── python-ci.yml   (reusable)
│         .github/workflows/         │   ├── ts-ci.yml       (reusable)
│         python-ci.yml@v1.0         │   └── go-ci.yml       (reusable)
├── .hyperi-ci.yaml                  ├── src/hyperi_ci/
├── Makefile                         │   ├── cli.py          (Typer entry point)
│   quality: hyperi-ci run quality   │   ├── config.py       (typed config + loader)
│   test:    hyperi-ci run test      │   ├── dispatch.py     (stage dispatcher)
│   build:   hyperi-ci run build     │   ├── detect.py       (language detection)
└── (no ci/ submodule)               │   └── languages/
                                     │       ├── python/
                                     │       ├── rust/
                                     │       ├── typescript/
                                     │       └── golang/
                                     └── config/
                                         ├── org.yaml
                                         └── defaults.yaml
```

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

Minimal, zero-dependency projects for fast CI iteration (<2 min builds):

| Language | Path |
|----------|------|
| Rust | `test-projects/ci-test-rust-minimal/` |
| Python | `test-projects/ci-test-python-minimal/` |
| TypeScript | `test-projects/ci-test-ts-minimal/` |
| Go | `test-projects/ci-test-go-minimal/` |

## Licence

Proprietary — HYPERI PTY LIMITED
