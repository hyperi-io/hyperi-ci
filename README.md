# hyperi-ci

One CLI for all your CI. Python, Rust, TypeScript, Go — same tool locally
and in GitHub Actions. No bash scripts, no composite actions, no submodules.

## Why Use This

**You get:**
- One command before every push: `hyperi-ci check`
- Same quality/test/build runs locally as in CI — no "works on my machine"
- Automatic versioning via semantic-release (just use conventional commits)
- Publishing when you're ready: `hyperi-ci release v1.3.0`
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
hyperi-ci init              # Auto-detects language, generates everything
git config core.hooksPath .githooks   # Activate commit validation hook
```

This creates `.hyperi-ci.yaml`, `Makefile`, `.github/workflows/ci.yml`,
`.releaserc.yaml`, and `.githooks/commit-msg`. Commit and push.

## Daily Workflow

```bash
# 1. Write code
# 2. Check before pushing (mandatory)
hyperi-ci check              # Quality + test
hyperi-ci check --quick      # Quality only (fast)
hyperi-ci check --full       # Quality + test + build

# 3. Commit (hook validates your message format)
git commit -m "fix: resolve timeout in auth handler"

# 4. Push
git pull --rebase origin main
git push origin main

# 5. CI runs automatically — semantic-release tags a version if warranted
```

## Publishing a Release

Versions accumulate on main. You choose when to ship:

```bash
# See what's available
hyperi-ci release --list
  v1.5.0  (2026-03-27)
  v1.4.0  (2026-03-25)

# Ship it
hyperi-ci release v1.5.0
```

This triggers the full pipeline: quality, test, build (cross-compile), publish.
Creates a GitHub Release, uploads binaries to R2, publishes to registries
(PyPI, crates.io, npm — depending on language and config).

Not every version needs publishing. `v1.4.0` stays as a tag — ship it later
or skip it entirely.

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
`hotfix:`, `security:`/`sec:` (all patch).

**Types that don't:** `docs`, `test`, `refactor`, `chore`, `ci`, `build`,
`deps`, `style`, `revert`, `wip`, `cleanup`, `data`, `debt`, `design`,
`infra`, `meta`, `ops`, `review`, `spike`, `ui`.

Full list: `hyperi-ci check-commit --list`

## Publish Channels

Control where artifacts go with one line in `.hyperi-ci.yaml`:

```yaml
publish:
  channel: release    # spike | alpha | beta | release
```

| Channel | GitHub Release | R2 Path | Registries |
|---------|---------------|---------|------------|
| `spike` | Prerelease | `/{project}/spike/v1.3.0/` | Skipped |
| `alpha` | Prerelease | `/{project}/alpha/v1.3.0/` | Skipped |
| `beta` | Prerelease | `/{project}/beta/v1.3.0/` | Skipped |
| `release` | GA | `/{project}/v1.3.0/` | Published |

Graduate a project by changing one line. No code changes, no workflow changes.

## Commands

| Command | What it does |
|---------|-------------|
| `hyperi-ci check` | Pre-push validation (quality + test) |
| `hyperi-ci check --quick` | Quality only |
| `hyperi-ci check --full` | Quality + test + build |
| `hyperi-ci run quality` | Lint, format, type check, security audit |
| `hyperi-ci run test` | Tests with coverage |
| `hyperi-ci run build` | Build artifacts |
| `hyperi-ci release --list` | List unpublished version tags |
| `hyperi-ci release <tag>` | Trigger publish for a tag |
| `hyperi-ci check-commit --list` | Show all accepted commit types |
| `hyperi-ci detect` | Show detected language |
| `hyperi-ci config` | Show merged config |
| `hyperi-ci trigger [--watch]` | Trigger CI workflow |
| `hyperi-ci watch` | Watch latest CI run |
| `hyperi-ci logs [--failed]` | Show CI run logs |
| `hyperi-ci init` | Scaffold a new project |
| `hyperi-ci upgrade` | Upgrade to latest version |

## How It Works

GitHub Actions handles orchestration. The CLI handles execution. Workflow
files stay small, and the same code path runs locally and in CI.

```
Your Project                        hyperi-ci
├── .github/workflows/ci.yml       ├── .github/workflows/
│   (5 lines — calls reusable)     │   ├── rust-ci.yml    (reusable)
├── .hyperi-ci.yaml                │   ├── python-ci.yml  (reusable)
├── .releaserc.yaml                │   ├── ts-ci.yml      (reusable)
├── .githooks/commit-msg           │   └── go-ci.yml      (reusable)
└── Makefile                       └── src/hyperi_ci/
                                       ├── cli.py         (entry point)
                                       ├── dispatch.py    (stage router)
                                       └── languages/     (per-language handlers)
```

**On push to main:**
```
quality -> test -> build (validation) -> semantic-release (tags version)
```

**On publish dispatch:**
```
checkout tag -> quality -> test -> build (full cross-compile) -> publish
```

## Config

`.hyperi-ci.yaml` in the project root. Cascade (highest wins):

```
CLI flags -> ENV vars (HYPERCI_*) -> .hyperi-ci.yaml -> defaults.yaml -> hardcoded
```

```yaml
language: rust              # Auto-detected if omitted
publish:
  enabled: true
  target: both              # internal | oss | both
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

## Languages

| Language | Quality | Test | Build | Publish |
|----------|---------|------|-------|---------|
| Python | ruff, ty, bandit, pip-audit | pytest | uv build | uv publish (PyPI/JFrog) |
| Rust | cargo fmt, clippy, audit, deny | cargo test/nextest | cargo build (cross) | cargo publish (crates.io/JFrog) |
| TypeScript | eslint, prettier, tsc, npm audit | vitest/jest | npm/pnpm build | npm publish |
| Go | gofmt, go vet, golangci-lint, gosec | go test -race | go build (cross) | go proxy, gh release |

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

Main branch builds amd64 only (validation). Publish builds the full matrix.

## Design Principles

1. **No bash** — all CI logic is Python. `subprocess.run()` with list args.
2. **Semantic release** — push to main, versions happen automatically.
3. **uv for everything** — venv, sync, lock, tool install, build.
4. **Cross-platform** — Linux (CI) and macOS (dev).
5. **Self-hosting** — hyperi-ci uses itself for its own CI.

## Licence

Proprietary — HYPERI PTY LIMITED
