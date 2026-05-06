# hyperi-ci

One CLI for all your CI. Python, Rust, TypeScript, Go — same tool locally
and in GitHub Actions. No bash scripts, no composite actions, no submodules.

## What's New in v2.0

**Version-first single-run pipeline.** A `Publish: true` git trailer on
your head commit is the single signal that a push is a release. The CI
run predicts the next version up front, stamps it into Cargo.toml /
VERSION / pyproject.toml / package.json **before** the build, then tags
+ uploads to all configured registries — all in one workflow. No second
"catch-up" build, no version-stamp drift between binary and tag.

**Tag-on-publish.** A git tag exists iff the artefact is in the
registry. Aligns with kubernetes / rust / python OSS conventions. No
more orphan tags from "tag every fix:, publish later" mode.

**100% FOSS pipeline by default.** Default `publish.target` is `oss`
(GitHub Releases + crates.io / PyPI / npm + GHCR + R2). Internal /
JFrog paths are opt-in and on a 4–6 week deprecation timeline.

See [docs/MIGRATION-GUIDE.md](docs/MIGRATION-GUIDE.md) for the v1 → v2
migration.

## Why Use This

**You get:**

- One command before every push: `hyperi-ci check`
- Same quality / test / build runs locally as in CI — no "works on my machine"
- Automatic versioning via semantic-release (just use conventional commits)
- One-shot publish: `hyperi-ci push --publish` (single CI run, single tag, single registry upload)
- Commit message validation that actually helps ("Computer says no.")

**Your repo gets:**

- A 5-line GitHub Actions workflow (calls our reusable workflow)
- A Makefile with `make check`, `make quality`, `make test`, `make build`
- Semantic-release config that just works
- A commit hook that catches bad messages before they hit CI

## Install

```bash
uv tool install hyperi-ci
```

## Set Up a Project

```bash
cd my-project
hyperi-ci init                          # Auto-detects language, generates everything
git config core.hooksPath .githooks     # Activate commit validation hook
```

This creates `.hyperi-ci.yaml`, `Makefile`, `.github/workflows/ci.yml`,
`.releaserc.yaml`, and `.githooks/commit-msg`. Commit and push.

## Daily Workflow

```bash
# 1. Write code
# 2. Check before pushing (mandatory)
hyperi-ci check                         # Quality + test
hyperi-ci check --quick                 # Quality only (fast)
hyperi-ci check --full                  # Quality + test + build

# 3. Commit (hook validates your message format)
git commit -m "fix: resolve timeout in auth handler"

# 4. Push (validate-only — no tag, no publish)
hyperi-ci push

# That's it. CI runs quality, test, build, and validates the container
# Dockerfile. No tag is created. No registry is touched.
```

## Publishing a Release

You opt in to a release explicitly. Two ways:

### Primary: `hyperi-ci push --publish`

```bash
git commit -m "fix: handle empty tenant id"
hyperi-ci push --publish        # alias: --release
```

This amends your head commit with the `Publish: true` git trailer, then
pushes. The single CI run:

1. Reads the trailer in setup → declares this a publish run
2. Runs `npx semantic-release --dry-run` to predict the next version (e.g. v1.5.4)
3. Stamps that version into Cargo.toml + VERSION before build
4. Builds (binary now embeds CARGO_PKG_VERSION = 1.5.4)
5. Builds + pushes container image to GHCR (multi-arch)
6. Runs `npx semantic-release` for real → creates tag, CHANGELOG commit
7. Uploads binaries to GitHub Release + R2; publishes to crates.io / PyPI / npm

One workflow run, one tag, one publish.

### Secondary: re-publish an existing tag

If a previous publish run failed mid-way (e.g. registry timeout) and you
want to retry without re-tagging:

```bash
hyperi-ci publish v1.5.4         # alias: release v1.5.4
```

This dispatches a `workflow_dispatch` event for the tag and runs
build → container → publish from the existing tagged source.

```bash
hyperi-ci publish --list         # see unpublished tags
```

### What pushes WITHOUT `--publish`

A plain `hyperi-ci push` runs the full pipeline through the build stage
in **validate-only** mode. Container builds (to catch Dockerfile
breakages) but does not push. No tag, no registry upload.

This means the default state of `main` is "all green, ready to ship."
You can release any time by running `hyperi-ci push --publish` on the
next conventional commit.

## Commit Messages

Conventional commits are enforced by a git hook and CI. The format:

```
<type>: <description>
<type>(scope): <description>
```

Get it wrong and you'll hear about it:

```
Computer says no.

  Unknown commit type: "yolo"

  Did you mean one of these?
    style  — code formatting, linting, cosmetic changes
    spike  — experimental, throwaway investigation
```

**Types that bump the version:** `feat:` (minor), `fix:` (patch), `perf:`,
`hotfix:`, `security:` / `sec:` (all patch).

**Types that don't:** `docs`, `test`, `refactor`, `chore`, `ci`, `build`,
`deps`, `style`, `revert`, `wip`, `cleanup`, `data`, `debt`, `design`,
`infra`, `meta`, `ops`, `review`, `spike`, `ui`.

Full list: `hyperi-ci check-commit --list`

## Publish Channels

Control where artifacts go with one line in `.hyperi-ci.yaml`:

```yaml
publish:
  channel: release    # spike | alpha | beta | release
  target: oss         # oss (default, FOSS) | internal (deprecated, JFrog) | both
```

### Channel behaviour

Pre-release channels (`spike`, `alpha`, `beta`) keep packages off the
public registries. Stable releases require `channel: release`.

| Channel | Registry publish | GH Release | R2 binaries | R2 path |
|---|---|---|---|---|
| `spike` | none | Prerelease | Uploaded | `/{project}/spike/v1.3.0/` |
| `alpha` | none | Prerelease | Uploaded | `/{project}/alpha/v1.3.0/` |
| `beta`  | none | Prerelease | Uploaded | `/{project}/beta/v1.3.0/` |
| `release` | configured target | GA | Uploaded | `/{project}/v1.3.0/` |

### Target behaviour

| `publish.target` | Containers | Binaries | Packages |
|---|---|---|---|
| `oss` (default) | GHCR | GitHub Release + R2 | crates.io / PyPI / npm |
| `internal` | JFrog Docker (deprecated) | JFrog Generic (deprecated) | JFrog Cargo / PyPI / npm (deprecated) |
| `both` | GHCR + JFrog | GitHub + R2 + JFrog | All registries |

JFrog targets are kept for back-compat; the path is on a 4–6 week
deprecation timeline. New projects should use `target: oss`.

### Graduating to GA

```
spike → alpha → beta → release
```

Each step is a one-line change to `publish.channel` in `.hyperi-ci.yaml`.
No code changes, no workflow changes.

## Commands

| Command | What it does |
|---|---|
| `hyperi-ci check` | Pre-push validation (quality + test) |
| `hyperi-ci check --quick` | Quality only |
| `hyperi-ci check --full` | Quality + test + build |
| `hyperi-ci push` | Push (validate-only — no tag, no publish) |
| `hyperi-ci push --publish` | Stamp `Publish: true` trailer, push, single-run publish |
| `hyperi-ci push --no-ci` | Push with `[skip ci]` (skip CI entirely) |
| `hyperi-ci publish <tag>` | Retroactive: dispatch publish on existing tag |
| `hyperi-ci publish --list` | List unpublished version tags |
| `hyperi-ci run quality\|test\|build\|generate\|container\|publish` | Run a single stage locally |
| `hyperi-ci init-contract --app-name <name>` | Scaffold `ci/deployment-contract.json` (Tier 3) |
| `hyperi-ci emit-artefacts <output-dir>` | Generate Dockerfile + chart + ArgoCD app from contract |
| `hyperi-ci check-commit --list` | Show all accepted commit types |
| `hyperi-ci detect` | Show detected language |
| `hyperi-ci config` | Show merged config |
| `hyperi-ci trigger [--watch]` | Trigger CI workflow |
| `hyperi-ci watch [--timeout SEC]` | Watch latest CI run (default 3600s; `--timeout 0` disables) |
| `hyperi-ci logs [--failed]` | Show CI run logs |
| `hyperi-ci init` | Scaffold a new project |
| `hyperi-ci upgrade` | Upgrade to latest version |

`hyperi-ci release` is kept as a deprecated alias of `hyperi-ci publish`
and will be removed in v3.0.

## How It Works

```
Your Project                          hyperi-ci
├── .github/workflows/ci.yml          ├── .github/
│   (5 lines — calls reusable)        │   ├── workflows/
├── .hyperi-ci.yaml                   │   │   ├── rust-ci.yml         (per-language)
├── .releaserc.yaml                   │   │   ├── python-ci.yml       (per-language)
├── .githooks/commit-msg              │   │   ├── go-ci.yml           (per-language)
└── Makefile                          │   │   ├── ts-ci.yml           (per-language)
                                      │   │   └── _release-tail.yml   (shared: container + publish)
                                      │   └── actions/
                                      │       └── predict-version/    (shared composite)
                                      └── src/hyperi_ci/
                                          ├── cli.py                  (entry point)
                                          ├── dispatch.py             (stage router)
                                          ├── push.py                 (push --publish)
                                          ├── publish/                (binaries + retro dispatch)
                                          ├── container/              (docker build/push)
                                          ├── deployment/             (contract / artefact gen)
                                          └── languages/              (per-language stage handlers)
```

### Pipeline (push to main, no `Publish: true` trailer)

```mermaid
flowchart LR
    Q[quality] --> S[setup]
    T[test] --> S
    S --> B[build]
    B --> C["container<br/>(validate-only)"]
```

No tag, no registry upload. Default state of main = "validated, ready to ship."

### Pipeline (push to main with `Publish: true` trailer, OR workflow_dispatch)

```mermaid
flowchart LR
    Q[quality] --> S["setup<br/>(predict next-version)"]
    T[test] --> S
    S --> B["build<br/>(stamp version)"]
    B --> C["container<br/>(push to registries)"]
    C --> TP["tag-and-publish<br/>(semantic-release + run publish)"]
```

One workflow, one tag, one publish.

## Config

`.hyperi-ci.yaml` in the project root. Cascade (highest wins):

```
CLI flags -> ENV vars (HYPERCI_*) -> .hyperi-ci.yaml -> defaults.yaml -> hardcoded
```

```yaml
language: rust              # Auto-detected if omitted
publish:
  enabled: true
  target: oss               # oss (default) | internal | both
  channel: release          # spike | alpha | beta | release
build:
  strategies: [native]
  rust:
    targets:
      - x86_64-unknown-linux-gnu
      - aarch64-unknown-linux-gnu
quality:
  gitleaks: blocking
```

## Container Builds & Deployment Artefacts

Every app emits its container image, Helm chart, and ArgoCD `Application`
from a single language-agnostic JSON contract — `ci/deployment-contract.json`.
The Build stage regenerates these via `hyperi-ci run generate`, and the
Quality stage drift-checks the committed `ci/` against the contract.

Three-tier producer model (auto-detected):

| Tier | Detected by | Producer |
|---|---|---|
| **Tier 1** (`rust`) | Cargo.toml + `hyperi-rustlib` dep | `<app> generate-artefacts` (rustlib) |
| **Tier 2** (`python`) | pyproject.toml + `hyperi-pylib` dep | `<app> generate-artefacts` (pylib) |
| **Tier 3** (`other`) | `ci/deployment-contract.json` only | `hyperi-ci emit-artefacts` |
| (none) | nothing | container stage no-ops silently |

All three tiers emit **byte-identical** output for the same JSON contract —
verified by the cross-tier parity test suite.

For Tier 3 onboarding: `hyperi-ci init-contract --app-name my-app`
scaffolds a starter `ci/deployment-contract.json`, then commit it and
run `hyperi-ci emit-artefacts ci/` to regenerate.

See [`docs/deployment-contract.md`](docs/deployment-contract.md) for the
user guide.

Images push to GHCR (`ghcr.io/hyperi-io/<app>`). Tags:

- Push to main with `Publish: true`: `:vX.Y.Z` + `:latest` + `:sha-abc1234`
- workflow_dispatch on tag: same tag set on the existing tagged source

Enable in `.hyperi-ci.yaml`:

```yaml
publish:
  container:
    enabled: auto    # auto | true | false
    platforms: [linux/amd64, linux/arm64]
```

## Languages

| Language | Quality | Test | Build | Publish |
|---|---|---|---|---|
| Python | ruff, ty, bandit, pip-audit | pytest | uv build | uv publish (PyPI) |
| Rust | cargo fmt, clippy, audit, deny, **feature_matrix** | cargo test/nextest | cargo build (cross) | cargo publish (crates.io) |
| TypeScript | eslint, prettier, tsc, npm audit | vitest/jest | npm/pnpm build | npm publish (npmjs / GH Packages) |
| Go | gofmt, go vet, golangci-lint, gosec | go test -race | go build (cross) | go proxy, gh release |

Per-language version stamping (publish runs only):

| Language | Stamps |
|---|---|
| Rust | `Cargo.toml` `[package].version` (and `[workspace.package].version` for workspaces) + `VERSION` |
| Python | `pyproject.toml` `[project].version` + `VERSION` |
| Go | `VERSION` (consumed via `-ldflags "-X main.Version=..."`) |
| TypeScript | `package.json` (via `npm version --no-git-tag-version`) + `VERSION` |

## Rust Feature Matrix Check

Rust projects automatically get a `cargo hack --each-feature --no-dev-deps check --lib`
pass during quality checks. This catches feature-gating bugs where a module behind
feature `X` uses a crate only declared by feature `Y` — without this check,
transitive deps from other features mask the bug until a downstream consumer
enables only `X`.

**Default behaviour** (always on, zero config): runs the bare-crate check
(`cargo check --no-default-features --lib`) plus the each-feature pass.

**Opt out** (requires a reason; CI fails if reason is missing):

```yaml
quality:
  rust:
    feature_matrix:
      enabled: false
      reason: "tracked in dfe-loader#87, remediating 2026-04-18"
```

## Cross-Compilation

Rust projects with C/C++ dependencies (librdkafka, openssl, zstd) are
supported. The build handler auto-detects native `-dev` packages, downloads
cross-arch equivalents into a private sysroot, and sets all compiler/linker
environment variables. Configure targets in `.hyperi-ci.yaml`:

```yaml
build:
  rust:
    targets:
      - x86_64-unknown-linux-gnu
      - aarch64-unknown-linux-gnu
```

Push to main without `Publish: true` builds amd64 only (validation).
Publish runs build the full matrix.

## Design Principles

1. **Version-first** — predict version up front, stamp before build. No catch-up rebuild.
2. **Tag-on-publish** — git tags exist iff the artefact is in the registry.
3. **No silent skips** — fail loud on broken handoffs (missing artefacts, missing handlers, etc.).
4. **No bash** — all CI logic is Python. `subprocess.run()` with list args.
5. **Semantic release** — push to main with `Publish: true` triggers a single-run release.
6. **uv for everything** — venv, sync, lock, tool install, build.
7. **Cross-platform** — Linux (CI) and macOS (dev).
8. **Self-hosting** — hyperi-ci uses itself for its own CI.

## Licence

Proprietary — HYPERI PTY LIMITED
