# TODO — HyperI CI

This is the **single source of truth** for all tasks and progress.

---

## Active Tasks

### Split-Runner Architecture + Release Gating

Multi-arch builds via native runners per architecture instead of cross-compilation sysroot.
`main` = dev pre-releases (x64 only), `release` = GA releases (x64 + arm64).

#### Phase 1: Documentation + SSOT

- [x] Update `config/versions.yaml` — add `upload-artifact: v7`, `download-artifact: v8`
- [x] Update `scripts/update-versions.py` — add upload/download-artifact to `_ACTION_OWNERS`
- [x] Apply version updates to all 4 workflow files (v4 → v7)
- [ ] Update `TODO.md` with full WBS (this section)
- [ ] Update `docs/DESIGN.md` — replace cross-compile section with split-runner architecture
- [ ] Create `config/runners.yaml` — runner SSOT per architecture

#### Phase 2: Workflow Templates

- [ ] `rust-ci.yml` — add setup job, build matrix (x64 ARC + arm64 native), remove cross-compile step
- [ ] `python-ci.yml` — update Release/Publish conditions for main + release branches
- [ ] `go-ci.yml` — update Release/Publish conditions for main + release branches
- [ ] `ts-ci.yml` — update Release/Publish conditions for main + release branches
- [ ] All workflows — Release on main (prerelease) + release (GA), Publish only on release

#### Phase 3: Release Configuration

- [ ] Update `init.py` `_render_releaserc()` — two-branch config (main=dev, release=GA)
- [ ] Update Release job conditions in all workflows (main || release)
- [ ] Publish job only on `release` branch

#### Phase 4: CLI — `init-release` Command

- [ ] Create `src/hyperi_ci/init_release.py` — init-release implementation
- [ ] Register `init-release` in `cli.py`
- [ ] Integrate with `migrate.py`

#### Phase 5: Commit + Push

- [ ] Commit GH Actions version fix (already staged)
- [ ] Commit architecture changes
- [ ] Push and verify hyperi-ci CI passes

#### Phase 6: Test with Consumer Projects

- [ ] hyperi-rustlib — `init-release`, verify dev + GA flow
- [ ] dfe-loader — `init-release`, verify x64 dev build, x64+arm64 GA build
- [ ] dfe-receiver — `init-release`, verify CI passes
- [ ] hyperi-pylib — `init-release`, verify CI passes
- [ ] dfe-engine — `init-release`, verify CI passes

### Other Active Tasks

- [ ] Address non-blocking quality warnings across all three consumer projects
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
