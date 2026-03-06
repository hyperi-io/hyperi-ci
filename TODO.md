# TODO — HyperI CI

This is the **single source of truth** for all tasks and progress.

---

## Active Tasks

- [ ] Get Rust CI pipeline working end-to-end `[IN PROGRESS]`
  - Current state: All workflow and config changes done but uncommitted
  - Changes: runner-mode support (all 4 workflows), uv cache cleanup, defaults.yaml (ty, deny:warn, e2e config), DESIGN.md runner docs
  - Next: commit and push changes, re-trigger ci-test-rust-minimal CI, monitor and fix until quality+test+build+release+publish all pass
  - Then: test publish to JFrog, swap to OSS (crates.io), swap back
  - Blockers: none — previous cargo deny failure should resolve with deny:warn in new defaults.yaml

---

## Backlog

### High Priority

- [ ] Replicate CI pipeline testing for Python, TypeScript, Go (after Rust works)
- [ ] Validate with test projects — create GitHub repos, push, verify CI end-to-end
- [ ] Reliable migration automation and documentation for existing `ci/` submodule attached projects
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

---

## Notes for AI Assistants

This file is the **single source of truth** for tasks and progress.

**Rules:**

- All tasks go here, nowhere else
- Planning mode outputs go here (WBS section)
- Mark tasks `[IN PROGRESS]` when starting
- Mark tasks `[x]` when complete, move to Completed section
- Never add tasks to STATE.md or CLAUDE.md
