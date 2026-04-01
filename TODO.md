# TODO — HyperI CI

This is the **single source of truth** for all tasks and progress.

---

## Active Tasks

### Feature Branch: `feat/jfrog-migration-container-pipeline`

All work pushed to feature branch. Merge to main when ready.

- [ ] **JFrog Migration** — merge when CI is free
  - JFrog reduced to PyPI/Cargo staging only, everything else to GitHub
  - Docker Hub auth, GHCR login via container mgt app, channel-aware publish
  - GitHub secrets already updated live (JFROG narrowed, DOCKERHUB widened, CONTAINER_MGT visibility)
  - Blocker: user approval to merge (impacts all consumer projects)

- [ ] **Issue #14: Breaking change rule** — bundled with JFrog migration
  - `{"breaking": True, "release": "major"}` added to init.py + .releaserc.yaml

- [ ] **Container Build Pipeline** — merge after rustlib deployment contract + dfe-loader test
  - 6 modules in `src/hyperi_ci/container/` (labels, templates, manifest, compose, build, stage)
  - 33 new tests, 273 total passing
  - Container job added to all 3 CI workflows (rust, python, ts)
  - Spec: `docs/superpowers/specs/2026-04-01-container-build-pipeline-design.md`
  - Blocker: rustlib `deployment_contract()` not yet implemented

### Other Active Tasks

- [ ] Address non-blocking quality warnings across consumer projects
  - vulture: dead code in hyperi-pylib, dfe-engine (non-blocking)
  - semgrep: security patterns in dfe-engine (non-blocking)
  - ty: type errors in all three (non-blocking, replaces pyright)
  - cargo audit: advisory DB issues in hyperi-rustlib (non-blocking)

---

## Backlog

### High Priority

- [ ] Validate TypeScript pipeline end-to-end (after Rust)
  - ci-test-ts-minimal exists in test-projects — use for testing
  - Need a real consumer TypeScript project to cut over

- [ ] Validate Go pipeline end-to-end (after TypeScript)

- [ ] Reliable migration automation and documentation for existing `ci/` submodule projects
  - 14+ consumer projects need to cut over from old `ci/` submodule to `hyperi-ci init`
  - Need: migration script, step-by-step docs, rollback plan
  - Must handle: removing old `ci/` submodule, cleaning `.gitmodules`, generating new files
  - Test on 1-2 projects first, then automate the rest

### Medium Priority

- [ ] Consumer project cutover — remove `ci/` submodule, run `hyperi-ci init`, verify
- [ ] Archive old repo — `hyperi-io/ci` → read-only

### @kay Review — Tooling Modernisation

- [ ] **Biome support for TypeScript projects** `@kay`
  - Biome (v2.3, Jan 2026) replaces ESLint + Prettier in one tool — 25x faster
  - 423 lint rules, type-aware linting via Biotype (~75-85% typescript-eslint coverage)
  - Plugin system via GritQL (Biome 2.0+)
  - **Proposal:** Detect `biome.json` in project root → use `biome ci` instead of eslint+prettier
  - **Migration path for existing projects:** `npx biome migrate eslint --write`
  - **For new projects:** Default to Biome via `hyperi-ci init` for TypeScript
  - **Not suitable yet for:** Next.js projects (eslint-config-next not ported)
  - **References:** [Biome vs ESLint 2026](https://www.pkgpulse.com/blog/biome-vs-eslint-vs-oxlint-2026), [Migration Guide](https://dev.to/pockit_tools/biome-the-eslint-and-prettier-killer-complete-migration-guide-for-2026-27m)

- [ ] **Ruff auto-fix as pre-commit hook template** `@kay`
  - Safe auto-fixable rules: import sorting (I001), unused noqa (RUF100), modernise syntax (UP*), remove placeholders (PIE790)
  - **Proposal:** Add `.pre-commit-config.yaml` template to `hyperi-ci init` with `ruff check --fix` + `ruff format`
  - Recommended but not required — CI is the enforcement gate, pre-commit is developer convenience
  - Avoids the `--no-verify` anti-pattern (optional hooks don't get bypassed habitually)

- [ ] **Evaluate Oxlint as CI speed layer** `@kay`
  - 50-100x faster than ESLint, ~300 rules, lint-only (no format)
  - Vercel pattern: Oxlint fast pre-pass + ESLint for deep rules
  - Lower priority — Biome covers most of this if adopted

### Requires Design Discussion

- [ ] **OSS deployment model and access control**
  - **Decision:** hyperi-ci = public, hyperi-pylib = public PyPI, hyperi-ai = private
  - **Open questions:**
    - Expenditure controls: runner minute budgets per external contributor/org
    - Secrets governance: ensure no secrets baked into CI config that could leak
    - hyperi-ai access: private submodule means external forks won't have it — verify graceful skip works well in practice
    - Publishing gates: even with `publish-target: oss`, only HyperI maintainers should trigger publish to PyPI/npm/crates.io
    - Dependency supply chain: external PRs modifying `uv.lock`/`package-lock.json` need extra review
    - External contributor onboarding: how do non-HyperI devs discover and use hyperi-ci?
    - Approval workflow: currently using GitHub "Require approval for first-time contributors" + fork PRs get quality+test only (no secrets, no build/publish) — is this sufficient?
    - Cost allocation: if OSS projects get significant external contribution, who pays for CI minutes?

---

## Completed

- [x] **Self-upgrade command (`hyperi-ci upgrade`)**
  - Explicit `upgrade [VERSION] [--pre]` command
  - Auto-update on run (default on, 4h check interval, `HYPERCI_AUTO_UPDATE=false` to disable)
  - Detects install method: tries uv first, falls back to pip
  - Re-execs via `os.execvp()` after upgrade with notification
  - Spec: `docs/superpowers/specs/2026-03-18-self-upgrade-design.md`

- [x] **Fix: R2 latest/ directory not cleaned between releases**
  - Stale files lingered when binary filenames changed (e.g. `dfe-receiver-release-*` → `dfe-receiver-*`)
  - Now `aws s3 rm --recursive` cleans latest/ before uploading new artifacts
  - Versioned directories remain immutable

- [x] **Fix: Binary naming — drop version from filenames**
  - Convention: `{name}-{os}-{arch}` (e.g. `dfe-receiver-linux-amd64`)
  - Version is in the directory path only (`/v1.14.0/dfe-receiver-linux-amd64`)
  - Matches HashiCorp/Rust/Go convention
  - Fixed `_detect_version()` to prefer VERSION file over GITHUB_REF_NAME
  - Closes hyperi-io/dfe-receiver#6

- [x] **R2 content decision: binaries + checksums only**
  - No README/CHANGELOG in R2 — docs live in the source repo
  - GitHub Releases already links to the repo for context
  - Documented in DESIGN.md

- [x] **Re-attached hyperi-ai submodule (3.1.0 → 3.1.2)**
  - Ran `attach.sh --agent claude --force` to deploy commands, rules, skills, hooks

- [x] **Split-Runner Architecture + Release Gating**
  - Split-runner build matrix in rust-ci.yml (x64 ARC + arm64 native)
  - Two-branch release config in all workflows (main=dev, release=GA)
  - `init-release` CLI command (`init_release.py` + registered in `cli.py`)
  - `config/runners.yaml` SSOT, `docs/DESIGN.md` updated
  - Consumer projects tested: hyperi-rustlib, dfe-loader, dfe-receiver, hyperi-pylib, dfe-engine

- [x] **Fix: Branch names with `/` break build artifact paths**
  - `_detect_version()` in Rust and Go build handlers used `GITHUB_REF_NAME` raw
  - Branch names like `fix/reconcile-release` created subdirectories in `dist/`
  - Added `sanitize_ref_name()` in `common.py` — replaces `/` with `-`
  - Applied to both Rust and Go `_detect_version()` functions

- [x] Add hyperi-ai standards submodule
- [x] Create reusable workflow templates (python-ci, rust-ci, ts-ci, go-ci)
- [x] Add `init` command (generates .hyperi-ci.yaml, Makefile, workflow)
- [x] Add `trigger`, `watch`, `logs` commands (ported from old CI)
- [x] Make `init` existing-project-smart per language
- [x] Publish handlers — Python, Rust, TypeScript, Go (JFrog + OSS)
- [x] Validate language handlers against test projects (quality, test, build)
- [x] Add C/C++ deps (librdkafka) to Rust test project with cross-compile support
- [x] Update Rust build handler for C/C++ cross-compilation env vars (CC/CXX/AR/PKG_CONFIG)
- [x] Create docs/DESIGN.md (architecture: GH Actions side + CLI side)
- [x] Update README.md (init, trigger/watch/logs, cross-compilation, languages table)
- [x] 82 tests passing, lint clean
- [x] Add runner-mode support to all 4 reusable workflows (free vs self-hosted)
- [x] Remove uv cache from non-Python workflows (was causing spurious warnings)
- [x] Add ty (Astral type checker) to Python quality defaults, disable pyright
- [x] Add Playwright E2E config to TypeScript test defaults
- [x] Set cargo deny to warn mode in defaults.yaml
- [x] Document runner modes and cross-compilation in DESIGN.md
- [x] **Python pipeline validated end-to-end with dfe-engine**
  - Quality ✓, Test ✓, Build ✓, Release (semantic-release) ✓, Publish to JFrog ✓
  - dfe-engine confirmed in JFrog `hyperi-pypi-local`
  - Removed `ci/` submodule from dfe-engine (replaced by hyperi-ci)
  - Fixed: pip_audit blocking; bandit B104 via config skip; ty/semgrep/vulture warn
  - Key lessons captured in docs/CI-LESSONS.md (Python section)

- [x] **Rust pipeline validated end-to-end with hyperi-rustlib**
  - Quality ✓, Test ✓, Build ✓, Release ✓, Publish to crates.io ✓ (v1.13.2)
  - Fixed: 30+ Rust 2024 edition clippy errors (collapsible_if, implicit_hasher, semicolons)
  - Fixed: `std::env::set_var` unsafe in Rust 2024 — changed unsafe_code forbid→deny, added allow in test files
  - Fixed: hyperi-ci build handler incorrectly packaging library-only crates (no bin targets) — now skips packaging
  - Publish target: oss (crates.io)

- [x] **Single Versioning with Channel-Based Publishing (v1.4.3)**
  - Eliminated version mismatch: binary --version now always matches GH Release
  - Single versioning on main (no prerelease `-dev.N` suffixes)
  - Release branch eliminated — publish via `hyperi-ci release <tag>` (workflow_dispatch)
  - Channel system: spike/alpha/beta/release in `.hyperi-ci.yaml` `publish.channel`
  - Commit message enforcement: "Computer says no." validation (hook + CI)
  - `check-commit` CLI, `.githooks/commit-msg` in init, commit-types SSOT
  - All reusable workflows updated (rust-ci, python-ci, ci)
  - `release-merge` command removed, `release` command added
  - Verified: hyperi-ci v1.4.3 on PyPI, hyperi-rustlib v1.20.1 on crates.io
  - Consumer migration guide: docs/MIGRATION-GUIDE.md
  - Spec: docs/superpowers/specs/2026-03-27-single-versioning-design.md

- [x] **Rust Build Optimisation + Renovate Runner Fix**
  - Renovate branches routed to 2cpu runners (`GH_RUNNER_RENOVATE` org var, all 4 workflows)
  - Local dev: per-project target symlinks on `/cache`, sccache, mold, cargo-sweep, jobs=8
  - Per-project `.cargo/config.toml`: removed `jobs=2`, added mold linker flag to x86_64 rustflags
  - `install-native-deps rust`: mold + clang installed for all Rust CI builds
  - dfe-developer Ansible role: sccache, mold, cargo-sweep, global cargo config
  - `setup-rust-dev.py` script for workstation remediation
  - Verified: concurrent builds, 100% sccache hit rate, mold in binaries, 2x repeat speedup
  - Loose end: per-project config changes applied locally but not committed to individual repos

- [x] **hyperi-pylib v2.24.3 released and published to PyPI**
  - Fixed ruff import sort in test file; user restructured to optional extras (http, metrics, etc.)
  - All jobs: Quality ✓, Test ✓, Build ✓, Release ✓, Publish ✓

- [x] **dfe-engine updated for pylib optional extras**
  - Added `http` extra to hyperi-pylib dep (AsyncHttpClient/HttpClient now optional)
  - Added WASM transform compile/test endpoints (`api/v1/transforms.py`)
  - Updated pylib constraint to >=2.24.3
  - All jobs: Quality ✓, Test ✓, Build ✓, Release ✓, Publish ✓

---

## Notes for AI Assistants

This file is the **single source of truth** for tasks and progress.

**Rules:**

- All tasks go here, nowhere else
- Planning mode outputs go here (WBS section)
- Mark tasks `[IN PROGRESS]` when starting
- Mark tasks `[x]` when complete, move to Completed section
- Never add tasks to STATE.md or CLAUDE.md
