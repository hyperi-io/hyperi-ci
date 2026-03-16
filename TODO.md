# TODO — HyperI CI

This is the **single source of truth** for all tasks and progress.

---

## Active Tasks

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
