# HyperI CI — Project State

Static context for AI assistants. For tasks and progress see `TODO.md`.

## Flaky Test = Fix the Test

**CI runs are 20-30 minutes on Rust projects. Re-running a flaky test
in hope of getting lucky wastes half an hour and teaches nothing. Fix
the race.**

When an integration test fails with a race symptom (ConnectionRefused,
timeout during startup, port-in-use, "no message arrived"), fix the
root cause. Typical patterns:

- Replace fixed-duration `sleep` with a readiness poll. For a spawned
  TCP server, poll `TcpStream::connect` in a tight loop with a **hard
  budget** (e.g. 100 × 50ms then panic with a useful message). Never
  busy-loop without a ceiling — unbounded waits block the runner for
  the whole workflow timeout and poison the job queue.
- For spawned async tasks, have the handler signal readiness via a
  channel and `await` the signal.
- For subprocess / testcontainer infrastructure, use the container's
  `wait_for` hook — don't sleep.

`gh run rerun --failed` is reserved for genuine infra incidents
(GitHub outage, transient 5xx from Harbor). For anything the project
owns, fix it.

## Subprocess Output Is Not UTF-8 By Default

Anything that captures `subprocess.run(text=True)` MUST also pin
`encoding="utf-8", errors="replace"`. Python's default text decoder
follows the locale, which on minimal containers / non-en_US hosts is
ASCII or POSIX — a single 0xff in `gh run download` output then
crashes the whole `hyperi-ci logs` run with `UnicodeDecodeError`.

The same applies to `print()` of foreign bytes: at CLI entry,
reconfigure `sys.stdout`/`sys.stderr` with `errors="replace"` so a
missing terminal codepoint replaces with `�` instead of raising
`UnicodeEncodeError` mid-stream.

This is enforced in `common.run_cmd()` and `cli.main()`. Don't roll
your own `subprocess.run` — call `run_cmd` so the policy applies.

## Watch Timeout Must Match Real CI Durations

`hyperi-ci watch` defaults to 3600s (60 min) — sized to cover Tier 2
PGO + BOLT Rust builds for both archs in parallel (35-45 min observed
in v1.17.5/v1.18.0 dfe-loader publishes). Smaller defaults silently
time out mid-build and leave the developer staring at "still in
progress" with no clear next step.

When a timeout *does* fire, the error message includes:
- Last-known status (so the caller knows whether to re-watch or
  investigate stuck/silently-failing runs)
- A copy-pasteable resume command (`hyperi-ci watch <id> --timeout 0`)

`--timeout 0` disables the timeout entirely — use this for runs you
know will run indefinitely (semantic-release rollbacks, manual
publish reruns).

## Local CLI Must Track Latest PyPI

Before running any `hyperi-ci` command locally (this repo or any consumer
project), ensure the installed CLI matches the latest **published** PyPI
release. Stale CLIs silently drift from the runner image behaviour and
mask bugs.

```bash
uv tool upgrade hyperi-ci          # or: hyperi-ci upgrade
pip index versions hyperi-ci       # verify against PyPI
```

`HYPERCI_AUTO_UPDATE` defaults to on (4h check). If it's disabled for a
session, you must upgrade explicitly before the first `hyperi-ci`
invocation. This is mandatory for canary / release work — the runner
image bakes a specific version and the local CLI must match to get
consistent behaviour between dev and CI.

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

1. **Version-first** — predict version up front (semantic-release dry-run), stamp into Cargo.toml/VERSION/pyproject.toml/package.json before build. No catch-up rebuild.
2. **Tag-on-publish** — git tags exist iff the artefact is in the registry.
3. **No silent skips** — dispatcher hard-fails on broken handler; container hard-fails on missing artefacts; predict-version hard-fails on `Publish: true` with no release-worthy commits.
4. **NO BASH** — all CI logic is Python. `subprocess.run()` with list args.
5. **uv for everything** — venv, sync, lock, tool install, build.
6. **Cross-platform** — Linux (CI) and macOS (dev). Uses `pathlib`, `shutil.which()`.
7. **Self-hosting** — hyperi-ci uses itself for its own CI.
8. **FOSS-first** — default `publish.target` is `oss`. JFrog publishing was removed in v2.1.4.

## CI workflow architecture (post-2026-05-08)

The CI workflow shape is documented in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
The summary, for context:

- **Two-level indirection only:** consumer ci.yml → language workflow → `_release-tail.yml`. No `_ci.yml`. No `_setup.yml`.
- **Plan-job pattern** (per [astral-sh/uv ci.yml](https://github.com/astral-sh/uv/blob/main/.github/workflows/ci.yml)): each language reusable workflow's first job runs the `predict-version` composite action and emits `run-checks` + `run-build` outputs. Every downstream job gates on those.
- **Same-org refs use `@main`** — pinning every same-org workflow / composite action to a SHA adds maintenance burden without proportional security benefit. Third-party actions ARE SHA-pinned via Renovate.
- **Drift between language workflows** is caught by `tests/unit/test_workflow_consistency.py` — pytest test that loads all four `<lang>-ci.yml` files and asserts the gate `if:` strings are identical.
- **No `_ci.yml` orchestrator** — was proposed in conversation 2026-05-08, dropped because (a) composite-action path resolution from cross-repo reusable workflows is unsolved as of May 2026, (b) mature multi-language OSS repos (astral-sh/uv, tokio-rs/tokio, vercel/turborepo) all use flat single-workflow + plan-job pattern. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full reasoning.

## CI gate doctrine

A non-bumping commit (chore/docs/test/refactor/style/build/ci/cleanup/…)
**MUST NOT cause cargo/uv/npm to compile or docker buildx to push**.
Even quality + test are skipped on push events that won't ship — only
PR review and release-worthy pushes run them. The gate is computed
ONCE in the `plan` job and consumed by every downstream job; do not
re-implement the condition string elsewhere — `tests/unit/test_workflow_consistency.py` enforces this.

## CI bug-log convention (cross-repo)

CI bugs and fixes surfaced in consumer repos are logged under
`<consumer>/docs/superpowers/plans/<date>-ci-<topic>.md` (gitignored
local-only) plus a one-line entry in that consumer's `TODO.md`.
This is the SSoT location for CI fixes across the org — when canary
runs surface bugs, look there for the resolution status. The
hyperi-ci rollout doc references back to those plans so the loop
is closeable.

## Architecture

See `docs/DESIGN.md` for full architecture documentation.

```
.github/
├── workflows/
│   ├── rust-ci.yml          # Per-language: quality + test + setup + build → calls _release-tail
│   ├── python-ci.yml        # Per-language: same shape
│   ├── go-ci.yml            # Per-language: same shape
│   ├── ts-ci.yml            # Per-language: same shape
│   └── _release-tail.yml    # SHARED: container + tag-and-publish (called by all 4)
└── actions/
    └── predict-version/     # SHARED COMPOSITE: gate + semantic-release dry-run

src/hyperi_ci/
├── cli.py                # Typer CLI (run, check, push, init, detect, config, trigger, watch, logs, publish, release, check-commit)
├── config.py             # CIConfig, OrgConfig, config cascade loader
├── common.py             # Logging, subprocess helpers, GH Actions output
├── detect.py             # Language detection from file markers
├── dispatch.py           # Stage dispatcher → language handlers (StageRunFn protocol)
├── init.py               # Project scaffolding (config, Makefile, workflow, releaserc, githooks)
├── push.py               # Push wrapper (pre-checks, --publish trailer-amend, --no-ci)
├── publish/              # Publish package
│   ├── binaries.py       # GH Release creation + R2/JFrog binary upload
│   └── dispatch.py       # Retroactive workflow_dispatch on existing tag
├── release.py            # DEPRECATED back-compat shim (re-exports from publish/)
├── publish_binaries.py   # DEPRECATED back-compat shim
├── gh.py                 # GitHub CLI helpers
├── trigger.py            # Workflow trigger command
├── watch.py              # Run watch command (default 3600s timeout; --timeout 0 disables)
├── logs.py               # Log fetch command (force UTF-8 with errors=replace)
├── quality/
│   ├── gitleaks.py       # Secret scanning
│   └── commit_validation.py  # Conventional commit enforcement
└── languages/
    ├── _build_common.py  # Shared helpers: human_size, generate_checksums
    ├── quality_common.py # Shared helpers: get_test_paths, get_test_ignore
    ├── python/           # quality, test, build, publish
    ├── rust/             # quality, test, build, publish
    ├── typescript/       # quality, test, build, publish
    └── golang/           # quality, test, build, publish
```

## Handler Interface

Every language handler module exports a function matching the
:class:`StageRunFn` protocol in :mod:`hyperi_ci.dispatch`:

```python
def run(config: CIConfig, *, extra_env: dict[str, str] | None = None) -> int:
    """Run the stage. Returns exit code (0 = success)."""
```

Dispatch finds handlers via `hyperi_ci.languages.<lang>.<stage>` module
path. A missing or non-callable `run` is a packaging bug — the dispatcher
hard-fails with `TypeError` rather than silently skipping the stage.

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
uv run hyperi-ci push --publish      # Stamp `Publish: true` trailer, push, single-run publish
uv run hyperi-ci push --bump-patch   # Force +0.0.1 release even with no-bump commits
uv run hyperi-ci push --bump-minor   # Force +0.1.0 release even with no-bump commits
uv run hyperi-ci push --no-ci        # Push, skip CI
uv run hyperi-ci publish --list      # List unpublished version tags
uv run hyperi-ci publish v1.3.0      # Retroactive: dispatch publish on existing tag
uv run hyperi-ci check-commit --list # List accepted commit types
```

`--release` and `release` are kept as deprecated aliases of `--publish` /
`publish` for back-compat; will be removed in v4.0.

`--bump-patch` / `--bump-minor` are for the case where you want to ship
a release whose commits aren't release-worthy under conventional-commits
rules (e.g. a docs-only PR you want to release, or a force-rebuild).
The flag adds a non-empty `fix(release):`/`feat(release):` marker commit
that updates VERSION and carries the `Publish: true` trailer. The
VERSION write is essential — empty marker commits get filtered by
consumer-project `paths-ignore` in their `ci.yml`. Major bumps are
deliberately excluded — they require a human-written breaking-change
footer.

## Versioning and Publishing (v2 — version-first, tag-on-publish)

**Single versioning on main.** Semantic-release runs only on `main`, producing
real versions (`1.3.0`, not `1.3.0-dev.8`). No release branch.

**Tag-on-publish.** A git tag exists iff the artefact is in the registry —
the same convention as kubernetes / rust / python. No more orphan tags
from "tag every fix:, publish later" mode.

**Publish is explicit and single-run.** `hyperi-ci push --publish` amends
the head commit with the `Publish: true` git trailer and pushes. The CI
run sees the trailer in setup, predicts the next version, stamps it
into `Cargo.toml` / `VERSION` / `pyproject.toml` / `package.json`
**before** the build, then tags + publishes — all in one workflow. No
catch-up rebuild.

A push without `--publish` is validate-only: quality + test + build +
container build (no push). Default state of `main` = "validated, ready
to ship."

**Channels** control where artifacts go (`publish.channel` in `.hyperi-ci.yaml`):
- `spike` / `alpha` / `beta` — GH Release (prerelease), R2 channel path, no registries
- `release` — GH Release (GA), R2 versioned path, PyPI/crates.io/npm

**Commit validation** enforced by `.githooks/commit-msg` hook and CI quality stage.
Invalid messages get "Computer says no." with friendly guidance.

See `docs/MIGRATION-GUIDE.md` for migrating projects from v1 to v2.

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

## Future direction (aspirational)

Today: GitHub for git hosting, GitHub Actions for CI. Likely move when
budget and time allow:

- **Codeberg** for git hosting — reduce single-vendor lock-in to GitHub.
- **Buildkite** for CI — stronger pipeline ergonomics, self-hosted
  runners without ARC's K8s overhead.

Design implications today:

- CI logic stays in the `hyperi-ci` Python CLI, not embedded in
  workflow YAML. Buildkite (or any successor) calls the same CLI;
  only the runner glue changes.
- Workflows stay thin — plan job + gates + handler dispatch.
- Avoid hard dependencies on GitHub-only features in handler code
  (Actions-specific matrix syntax, GHCR-only auth flows).

Not on the near-term roadmap; recorded so we don't accidentally make
choices that paint us into the GitHub-Actions corner.

## Licensing

Proprietary — HYPERI PTY LIMITED.
