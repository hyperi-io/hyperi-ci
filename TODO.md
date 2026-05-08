# TODO — HyperI CI

This is the **single source of truth** for all tasks and progress.

---

## Active Tasks

### CI consolidation (KISS) — predict-first plan-job per language

**Plan:** [`docs/superpowers/plans/2026-05-08-ci-consolidation-kiss.md`](docs/superpowers/plans/2026-05-08-ci-consolidation-kiss.md)

**Architecture:** [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) (will be created in Task 1 of the plan)

**Why:** chore: commits + AI submodule bumps were burning ~25 minutes of CI compute per push. Two earlier consolidation attempts in this session (`_setup.yml` reusable workflow, sketched `_ci.yml` orchestrator) added indirection without solving the root cause. Web research (astral-sh/uv, tokio-rs/tokio, vercel/turborepo) showed the right shape: flat language workflow with a `plan` first job + conditional gates. No reusable-workflow chains.

**Status:** plan written 2026-05-08. Implementation tracked task-by-task in the plan file (17 tasks).

**Resume from this directory** (`/projects/hyperi-ci`) when picking this up — the plan, STATE.md, TODO.md, and code all live here. Recommended execution: inline for Tasks 1-12 (mechanical YAML + tests), subagent-driven for Tasks 13-15 (canaries that need fresh per-project context).

**Canaries (no SEP fields — fix bugs found, don't dismiss):**

- [ ] **Canary 0: hyperi-ci's own `test-projects/`** — `ci-test-python-app`, `ci-test-go-app`, `ci-test-ts-app` — drives the iteration loop in-repo via `tests/integration/`.
- [ ] **Canary 1: dfe-loader (Rust)** — most complex Rust example: clickhouse-driver, Arrow, columnar deps. Must produce a tagged R2 + GHCR + GH Release artefact under the new shape, AND a chore: bump must skip build entirely.
- [ ] **Canary 2: dfe-engine (Python)** — most complex Python example. Same exit criteria.

**Done means:**

- All 17 plan tasks checked off
- All four `<lang>-ci.yml` workflows match the same shape (verified by `tests/unit/test_workflow_consistency.py`)
- A chore: commit on each canary completes in <2 min, skips quality/test/build/container/publish
- A `Publish: true` commit on each canary still runs the full pipeline and ships to the right registries
- v2.2.0+ of hyperi-ci is published to PyPI

**Bugs discovered during rollout (no SEP fields):** populate as the rollout proceeds.

---

### Dep-Install SSOT — Canary Rollout

**Canary 1 (dfe-receiver): COMPLETE — released to R2.** Full BOLT+PGO
publish chain validated on rebuilt ARC runners. Remaining:

- [ ] **Canary 2: dfe-loader** — same shape, different deps (ClickHouse
  client lib, Arrow, columnar). Broader apt surface. Watch for the
  same criteria: cargo-pgo BOLT step, `ld.lld` unversioned shim at
  `~/.local/bin/ld.lld`, pre-baked toolchains (bolt-22, mold, LLVM
  19/20/21/22, GCC 13/14) picked up without refetch, R2 upload to
  `downloads.hyperi.io/dfe-loader/vX.Y.Z/`.
- [ ] **Broader rollout** — dfe-archiver, dfe-fetcher, hyperi-rustlib,
  hyperi-pylib, transform projects. Each should be a no-op if Canary 2
  is clean.
- [ ] **hyperi-infra side commit** — branch
  `fix/arc-runners-hyperi-ci-integration` (Dockerfile pin
  `'hyperi-ci>=1.12'` + Debian arm64 sources consolidation) still
  needs to be pushed + merged. Deferred this session because the
  hyperi-infra worktree has unrelated uncommitted changes (TODO.md,
  inventory.yml, k8s playbooks, r2-frontend, scripts/mail-migration/).

If the canary surfaces any issue: **no SEP fields** — the canary owner
has explicit authority to edit any of hyperi-ci, hyperi-infra,
dfe-receiver, dfe-loader to fix it. See `docs/ARC-RUNNERS.md` "Cross-
Project Rollout Flow" for how changes propagate.

**Source-of-truth checks (session-independent):**

1. Is a docker build still in-progress on infra?
   `ssh ubuntu@infra.devex.hyperi.io 'pgrep -af "docker build"'`
   Running line = build in-flight. Empty output = build finished
   (or playbook errored). Ubuntu runs first, then Debian.
2. Has Harbor received fresh pushes?
   `bao-admin kv get -field=admin_password kv/services/harbor` → curl
   `https://harbor.devex.hyperi.io:8443/api/v2.0/projects/library/repositories/arc-runner{,-debian}/artifacts?page_size=2`
   and compare `.push_time` against when the rebuild was kicked off.
3. If the build is not running and Harbor has no fresh push, it
   errored — re-run the ansible command.

**Ansible rebuild command** (run from `/projects/hyperi-infra`):

```
ansible-playbook -i ansible/inventories/prod/inventory.yml \
  ansible/playbooks/k8s-arc-runners.yml --tags image \
  -e harbor_admin_password=$(scripts/bao-admin kv get -field=admin_password kv/services/harbor)
```

Takes ~25-30 min. Both runner variants rebuild.

Known things to check if the build fails:
- If `install-toolchains --all` fails on a NEW conflict, look for
  `E: held broken packages` or `root is not in the sudoers file` lines.
  The per-version install is done in ONE batched `apt-get install` (see
  `install_native_deps()` in `src/hyperi_ci/native_deps.py`).
- Dockerfile `RUN` executes as root — `_sudo_prefix()` (v1.12.1+) drops
  the `sudo` prefix in that case. If a stale hyperi-ci is installed,
  the bake will fail with `root is not in the sudoers file`.
- If the pin in hyperi-infra's Dockerfile isn't picking up the latest
  version because of Docker layer caching, bump `'hyperi-ci>=X.Y'` to
  an exact version and rebuild with `--no-cache` on that layer.

### Dep-Install SSOT — Phases 2-5 (backlog)

The goal: runner Dockerfiles shrink from ~300 lines to ~30 by moving
ALL dep installation into hyperi-ci as SSOT.

- [ ] **Phase 2** — `config/ci-tools/default.yaml` + GH-release fetcher
  driver. Covers: `gh`, `hadolint`, `shellcheck`, `actionlint`,
  `cargo-nextest`. Currently installed via inline `curl | tar`
  invocations in the runner Dockerfiles.
- [ ] **Phase 3** — `config/base-apt/{noble,trixie,resolute}.yaml` for
  bootstrap apt packages (`build-essential`, `cmake`, `ninja-build`,
  `mold`, `zlib1g-dev`, etc.). Replaces the big inline `apt install`
  block in each runner Dockerfile.
- [ ] **Phase 4** — `config/runtimes/*.yaml` covering language version
  managers: `rustup`, `uv` (Python), `fnm` (Node), `mise` (Go),
  `sdkman` (Java). Per-manager driver backends in a new `runtimes.py`.
- [ ] **Phase 5** — `config/cross-sysroot/{arm64-noble,arm64-trixie}.yaml`
  for cross-compile sources + `qemu-user-static`. Driver to handle
  `dpkg --add-architecture` + source file writes in a dedupe-safe way.

End state: the runner Dockerfile body is

```dockerfile
RUN apt-get update && apt-get install -y python3 python3-pip curl ca-certificates gnupg
COPY internal-ca-chain.crt /usr/local/share/ca-certificates/
RUN update-ca-certificates
RUN pip install 'hyperi-ci==X.Y.Z'
RUN hyperi-ci prime-image --distro noble --all
```

### Legacy Active Tasks

### JFrog Migration (DO NOT PUSH — dozens of projects use CI live)

- [x] Fix CONTAINER_MGT secrets/vars visibility (PRIVATE -> all)
- [x] Update config/secrets-access.yaml (jfrog-staging group, Docker Hub widened, container mgt)
- [x] Widen DOCKERHUB_USERNAME + DOCKERHUB_TOKEN to ci-consumers group (applied live)
- [x] Narrow JFROG_TOKEN/JFROG_USERNAME to selected (dfe-engine, dfe-core only, applied live)
- [x] Delete JFROG_ACCESS_TOKEN (applied live)
- [x] Add Docker Hub login + GHCR login to all 4 reusable workflows (local)
- [x] Update defaults.yaml destinations_internal (npm/container/helm/go -> GitHub, local)
- [x] Update org.yaml (reduce JFrog repos, add dockerhub section, local)
- [x] Add _publish_ghcr_npm() to TypeScript publish handler (local)
- [x] Channel-aware publish: spike/alpha/beta force internal target (dispatch.py, local)
- [x] Update DESIGN.md, README.md, JFROG-MIGRATION.md with mermaid diagrams (local)
- [ ] **Push all local changes** `[BLOCKED — waiting for user approval]`
  - 13+ files modified locally, rebased onto v1.5.0
  - Next: user says "push" when CI is free

### Issue #14: Breaking Change Rule Missing

- [x] Add `{"breaking": True, "release": "major"}` to init.py scaffolded releaseRules
- [x] Fix hyperi-ci's own .releaserc.yaml
- [x] Document breaking rule in config/commit-types.yaml
- [ ] **Push fix** `[BLOCKED — bundled with JFrog migration push]`

### Container Build Pipeline (DO NOT PUSH — depends on rustlib deployment contract)

- [x] Spec approved: `docs/superpowers/specs/2026-04-01-container-build-pipeline-design.md`
- [x] Plan written: `docs/superpowers/plans/2026-04-01-container-build-pipeline.md`
- [x] Task 2: Extend container config defaults (defaults.yaml)
- [ ] Task 1: OCI Label Generation (labels.py + tests) `[IN PROGRESS — subagent]`
- [ ] Task 3: Python and Node Dockerfile Templates (templates.py + tests) `[IN PROGRESS — subagent]`
- [ ] Task 4: Container Manifest Parser (manifest.py + tests) `[IN PROGRESS — subagent]`
- [ ] Task 5: Dockerfile Composer contract mode (compose.py + tests)
- [ ] Task 6: Build and Push Module (build.py)
- [ ] Task 7: Container Stage Handler + Dispatch Integration (stage.py + dispatch.py)
- [ ] Task 8: Add Container Job to Rust CI Workflow
- [ ] Task 9: Add Container Job to Python and TS CI Workflows
- [ ] Task 10: Update DESIGN.md and README.md with mermaid diagrams
- [ ] Task 11: Run Full Test Suite + Lint
- [ ] **Push** `[BLOCKED — wait for rustlib deployment contract + dfe-loader test]`

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
  - Standalone fixture: github.com/hyperi-io/ci-test-ts-app (cloned at /projects/ci-test-ts-app)
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

- [x] **Canary 1 (dfe-receiver) — fully released through BOLT + R2 on the
  v1.12.1 runner image.** dfe-receiver v1.15.8 on GH Releases and
  `downloads.hyperi.io/dfe-receiver/v1.15.8/` (amd64 15.2 MB, arm64
  12.7 MB, checksums.sha256). BOLT+PGO both archs green (amd64 28m9s,
  arm64 29m3s). Pre-baked toolchains (bolt-22, mold, LLVM 19-22, GCC
  13/14) picked up without re-fetch at job time.

- [x] **hyperi-ci v1.12.1: `_sudo_prefix()` helper (runner image bake).**
  Dockerfile `RUN` runs as root where sudo isn't configured; old code
  prepended `sudo` unconditionally and failed with `root is not in the
  sudoers file`. New helper returns `[]` when root/non-Linux, `["sudo"]`
  otherwise. Three new regression tests (TestSudoPrefix) + two existing
  macOS test fixups (pin `_sudo_prefix` to `["sudo"]` so fake
  subprocess matchers still hit). 441 tests green.

- [x] **Rebuilt both ARC runner images against hyperi-ci v1.12.1.**
  Harbor push_time 2026-04-22T02:31Z for both `arc-runner` (noble) and
  `arc-runner-debian` (trixie). Ansible playbook
  `k8s-arc-runners.yml --tags image` from `/projects/hyperi-infra`.

- [x] **Fixed flaky dfe-receiver integration tests on ARC runners.**
  `protocol_kafka_roundtrip::test_splunk_hec_to_kafka_roundtrip` +
  `test_prometheus_rw_to_kafka_roundtrip` raced the handler bind with
  a fixed 500ms sleep → ConnectionRefused under parallel load.
  Replaced with `wait_for_port()` (polls `TcpStream::connect` with a
  5s hard budget). Same fix applied to
  `grpc_sink::start_server` (200ms sleep → port poll) and
  `grpc_sink::test_grpc_sink_large_payload` (bare `send` → `send_with_retry`).

- [x] **STATE.md rules added** (three static policies):
  - Local CLI must track latest PyPI before any `hyperi-ci` invocation
  - Flaky test = fix the test (no rerun-and-hope); readiness polls
    require a hard budget
  - Canary not done until R2 (branch CI bypasses release-gated PGO+BOLT)

- [x] **Dep-Install SSOT foundation (v1.10.8 → v1.12.0, 2026-04-21/22)**
  - v1.10.8: BOLT `strip=none` fix — rust-lld rejects `--strip-all` +
    `--emit-relocs`; `_bolt_linker_env` → `_bolt_build_env` (alias kept).
    Surfaced during dfe-receiver v1.15.7 canary.
  - v1.11.0: **`toolchains` category + `versions:` multi-version expansion
    + `${OS_CODENAME}` substitution + `--all` mode.** New `install-toolchains`
    CLI. Resolute (Ubuntu 26.04 LTS) added to fallback codename list.
  - v1.11.1: `_add_apt_repo` uses `tee -a` (append, not overwrite) —
    multi-version expansion was clobbering the sources file so only the
    last version survived.
  - v1.11.2: Drop `libc++-N-dev`, `libc++abi-N-dev`, `libomp-N-dev`,
    `libunwind-N-dev` from multi-version — apt.llvm.org declares
    `Conflicts: <pkg>-x.y`, only one version installable at a time.
  - v1.12.0: **`bake: false` schema flag** — first-class standard for
    non-coinstallable toolsets. Skipped in `--all` (runner image),
    installed on-demand at CI job time. Drops `lldb-N` from
    multi-version (transitive `python3-lldb-N` Conflicts).
  - Test suite: 438 passing.
  - Cross-project integration: hyperi-infra runner Dockerfile uses
    `pip install 'hyperi-ci>=1.12' && hyperi-ci install-toolchains --all`.

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
