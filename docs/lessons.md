# CI Lessons Learned

From Derek >>

Patterns, gotchas, and proven solutions extracted from the old HyperI CI
(`/projects/ci`). Reference this before implementing or debugging any CI
handler. The old CI grew organically but was comprehensive and production-tested
across 14+ consumer projects.

Source: `hyperi-io/ci` (to be archived once cutover is complete).

---

## Rust

### Cross-Compilation (Critical)

**The mold linker problem:**
- GitHub runners may have `mold` as default linker (`-fuse-ld=mold`)
- Cross-compilers (e.g. `aarch64-linux-gnu-gcc`) cannot find `ld.mold` for
  non-native targets, causing CMake test compilations to fail
- **Solution:** Force GNU BFD linker via `-fuse-ld=bfd` in the linker wrapper,
  and clear `LDFLAGS`/`CFLAGS`/`CXXFLAGS` to prevent host flags leaking into
  cross-compilation CMake builds

**Private sysroot approach (proven pattern):**
- Many `-dev` packages (e.g. `libsasl2-dev`) are NOT `Multi-Arch: same` -
  installing arm64 replaces amd64, breaking native builds
- **Solution:** Download cross-arch `.deb` files, extract to private sysroot
  (`.tmp/cross-sysroot/` in the workspace), point `PKG_CONFIG_PATH` and linker at it
- Only install cross-compilers system-wide (they ARE Multi-Arch safe):
  `gcc-aarch64-linux-gnu`, `g++-aarch64-linux-gnu`
- Also install `libc6-dev:arm64` (provides dynamic linker and standard libs)

**Linker wrapper script:**
- Creates wrapper around cross-compiler that injects sysroot library paths
- Uses `-fuse-ld=bfd` (forces GNU BFD linker, not mold)
- Includes `-L` and `-rpath-link` flags for transitive `.so` dependencies
- Example: `libsasl2.so` needs `libcrypto.so.3` - linker needs `-rpath-link`

**GNU LD script path patching:**
- Some `.so` files are ASCII linker scripts with absolute paths:
  `GROUP ( /lib/aarch64-linux-gnu/libm.so.6 ... )`
- These absolute paths don't exist on host - rewrite to point at sysroot

**Environment variables for cross-compilation:**
- `CC_<TARGET>`, `CXX_<TARGET>`, `AR_<TARGET>` for cross-compiler binaries
- `CARGO_TARGET_<TARGET>_LINKER` for Rust to use the linker wrapper
- `PKG_CONFIG_PATH`, `PKG_CONFIG_SYSROOT_DIR`, `PKG_CONFIG_ALLOW_CROSS=1`
- `CMAKE_PREFIX_PATH` for cmake-based `-sys` crates (e.g. `rdkafka-sys`)
- `CFLAGS_<TARGET>` with `-fuse-ld=bfd` and arch-specific include paths
- Clear `LDFLAGS`, `CFLAGS`, `CXXFLAGS` to prevent host flag leakage

**Build ordering:**
- Build native target FIRST, then cross targets
- Avoids multi-arch package conflicts

**Target installation:**
- Run `rustup target add <target>` for each non-native target before building

**Post-build verification:**
- Check binary exists and is not suspiciously small (<100KB)
- Verify ELF format with `file(1)` and machine type with `readelf -h`
- Native: smoke test with `--version` or `--help`
- Cross-compiled: skip smoke test (can't execute)

### Publishing

- `cargo publish --allow-dirty` (semantic-release modifies files pre-publish)
- Handle "already exists" gracefully (grep stderr, treat as success)
- Git dependency patching: replace `git = "https://..."` with
  `version = "1", registry = "hyperi"` before publish, restore after
- Intra-workspace path deps: add `registry = "hyperi"` (cargo publish strips
  `path=` but defaults to crates.io without explicit registry)

### Quality

- `cargo deny` requires `deny.toml` - skip if not present
- `cargo audit` may fail with "error loading advisory database" - skip gracefully
- Clippy: force `-D clippy::dbg_macro` to prevent debug macros in production
- Multi-feature testing: pipe-separated `RUST_FEATURES` runs clippy per set

### Testing

- Integration tests: default to 1 test thread (port conflicts with parallelism)
- `cargo nextest` and `cargo test` are NOT interchangeable: nextest gives each
  test its own process, cargo test shares one, so process-global state
  (a metrics recorder, a `OnceLock`) behaves differently and doctests run only
  under cargo test. Picking by what is installed silently changes semantics -
  the ARC image bakes nextest in, a hosted/free runner does not. Resolved via
  the `test.rust.nextest` tri-state, which announces the choice, annotates an
  `auto` degradation, and fails the stage on `true` with nextest absent
- Coverage: tarpaulin > llvm-cov, both optional - and both drive cargo's test
  harness, so coverage overrides a resolved nextest runner
- Feature combinations: builds use FIRST set only (`${FEATURES%%|*}`)

### Workspace Support

- Use `cargo metadata --no-deps --format-version 1` for workspace detection
- Cache metadata output (avoid repeated 1-2s invocations)
- Workspace-transparent helpers: work for both single-crate and multi-crate

---

## Go

### Cross-Compilation

- Always disable CGO: `CGO_ENABLED=0` (default for cross-compilation)
- Set per-target: `GOOS=<os> GOARCH=<arch> GOARM=<arm_version>`
- **Always unset** `GOOS`/`GOARCH`/`GOARM` after build loop (prevents
  contaminating subsequent commands)
- Target shortcuts: `all`, `linux`, `windows`, `darwin` expand to common matrices

### Build Patterns

- LDFLAGS: `-s -w` (strip symbols + DWARF debug info, smaller binaries)
- Version injection: `-X 'main.version=v1.0.0' -X 'main.commit=abc123'
  -X 'main.buildTime=2025-01-20T14:30:00Z'`
- Main package detection: `GO_MAIN_PKG` env > `cmd/{binary}/` > single `cmd/` subdir > `.`
- Optional: garble obfuscation (`GO_GARBLE=true`)
- Output naming: `{binary}-{version}-{os}-{arch}[.exe]`

### Testing

- Race detector: always enable (`-race`)
- Coverage mode: must be `atomic` when using `-race` (not `count` or `set`)
- Timeout: 10m default per test
- JUnit: `go-junit-report` for CI integration

### Quality

- golangci-lint: 5 minute timeout (long-running), auto-detect config file
- gosec: security static analysis
- govulncheck: dependency vulnerability scanning
- Tool modes: blocking/non-blocking/disabled (same pattern as all languages)

### Publishing

- Checksums uploaded as `SHA256SUMS`, not per-file `.sha256`
- Latest reference: copy to `/latest/` + `LATEST_VERSION.txt`
- Skip latest for snapshots (`^snapshot-` prefix)
- Container: `docker buildx build --platform linux/amd64,linux/arm64`

---

## TypeScript

### Package Manager Detection

- Lockfile-based: `pnpm-lock.yaml` > `yarn.lock` > default `npm`
- Auto-install pnpm/yarn via `npm install -g` if not found

### Registry Auth

- `.npmrc` with `_authToken`

### Quality

- ESLint config detection: flat (ESLint 9+) AND legacy formats
- TypeScript type checking: prefer `check-types`/`typecheck` package.json
  scripts over direct `tsc --noEmit` (allows monorepo tooling)
- Audit level: configurable (`low`/`moderate`/`high`/`critical`)

### Testing

- Framework auto-detection: Jest > Vitest > Mocha (from devDependencies)
- Turborepo awareness: skip framework-specific args when `turbo.json` exists
  (each workspace manages its own config)
- No test script: exit 0 (graceful skip, not failure)

### Publishing

- Auto-generate `.npmignore` if missing AND no `files` in package.json
- Package size pre-flight check (default max 1MB, configurable)
- pnpm: `--no-git-checks` flag required for workspace publishing

---

## Python

### uv Index Strategy (Critical - Do Not Change)

**NEVER add `UV_EXTRA_INDEX_URL` to dep install steps in reusable workflows.**

uv does NOT work like pip with mixed public + private indices. By default uv
uses first-match-wins per package name. If a private index returns an empty
200 for a package it doesn't have (e.g. `hatchling`, `setuptools`), uv stops
there and reports "no versions found" rather than falling back to PyPI.

A workaround exists (`UV_INDEX_STRATEGY=unsafe-best-match`) but is fragile
and changes resolver semantics across all packages.

**Correct approach:**
- Leave workflow install steps as `uv sync --frozen --all-extras` with NO
  extra index env vars
- Projects that need packages from a private index configure `[tool.uv.index]` with
  `explicit=true` in their own `pyproject.toml` - explicit indices are only
  consulted for packages that name them

This comment is in-code in all four reusable workflows. Do not remove it.

### uv Patterns

- CI builds on the version the PROJECT declares, not a fleet-wide one - see
  [python.md](languages/python.md). `versions.yaml` names the default for a
  project that declares nothing
- Build: `uv build` (replaces `python -m build`)
- Publish: `uv publish --publish-url` with explicit `--username` / `--password`

### sdist Exclusions (AI Agent Dirs and Org Submodules)

Hatchling's sdist includes all git-tracked files by default. This causes
problems when the repo has:
- AI agent config dirs (`.claude/`, `.cursor/`, `.gemini/`, `.windsurf/`)
- AI-related symlinks (`.claude/rules/user-standards.md` -> outside project)
- Org submodules (`hyperi-ai/`, `ci/`) with their own content

**Solution:** The `build.py` handler uses a `_inject_sdist_excludes()` context
manager that temporarily patches `pyproject.toml` before `uv build` to add
standard exclusions, then restores the original file after.

Standard exclusions applied automatically to every Python sdist build:
```
/.claude  /CLAUDE.md  /.cursor  /CURSOR.md  /.gemini  /GEMINI.md
/.github/copilot-instructions.md  /.windsurf  /STATE.md  /hyperi-ai  /ci
```

Projects can add project-specific excludes via `[tool.hatch.build.targets.sdist]
exclude` in their `pyproject.toml` - those are merged with the standard ones.

The build step in the reusable workflow MUST go through `hyperi-ci run build`
(not raw `uv build`) so the exclusion injection runs. The publish step re-runs
build to ensure fresh artifacts, also via `hyperi-ci run build`.

### Quality Tool Exclusions (Critical)

- `--extend-exclude` ADDS to defaults (safe)
- `--exclude` REPLACES defaults (dangerous - would scan `.venv`)
- Each tool has different exclusion syntax:
  - ruff: `--extend-exclude dir`
  - bandit: `--exclude dir1,dir2` (comma-separated, prefix with `./`)
  - pyright: config-file only (no CLI exclusions)
  - eslint: `--ignore-pattern dir`

### Bandit Configuration

- Skip `B104` (hardcoded_bind_all_interfaces) globally via `[tool.bandit] skips`
  in `pyproject.toml` when the service intentionally binds `0.0.0.0` (containers)
- Skip `B608` (hardcoded_sql) if queries use internal config templates, not user input
- Use config-level skips (`[tool.bandit]`) rather than inline `# nosec` where possible
- `bandit_exclude_tests: true` in `.hyperi-ci.yaml` skips `tests/` directory

### Testing

- Tiered: `tests/unit/`, `tests/integration/`, `tests/e2e/`
- Detection: directory-based > marker-based > conftest-based
- Coverage: separate `.coverage.<tier>` files, combine at end
- No test directory: exit 0 (graceful skip)
- Submodule-dependent tests or container builds:
  - Public submodule: declare `submodules: schemas` in `.hyperi-ci.yaml`;
    `hyperi-ci init` renders it as the reusable-workflow `submodules` input
    in `ci.yml`. The test job checks it out so the tests run, and the
    container build checks it out so a Dockerfile can bake submodule
    content into the image.
  - Private submodule (e.g. `dfe-schemas` while private): GITHUB_TOKEN can't
    clone it, so mark dependent tests with
    `@pytest.mark.skipif(not schemas_dir.exists(), reason="submodule not checked out")`
    rather than checking it out in CI (or wire a cross-repo token).

### Publishing

- Verification: query the index's simple API, check for version in HTML response
- Handle both naming conventions: `package-name-version` and `package_name-version`
- Index propagation takes minutes - retry loop (5 retries, 10s delay)
- "already exists" from PyPI is non-fatal (idempotent re-runs)

---

## Cross-Language Patterns

### A workflow change is not proven by a green test suite

Three real bugs in one workflow change got past ~3000 local tests and were
caught by `scripts/rehearse-branch.py` on a real `ci-test-*` fixture. Rehearse
every change to `.github/workflows/` before merging it.

The reason the suite cannot find them:

- **A wrong model is tested as confidently as a right one.** A gate job treated
  `build` as governed by `run-checks`; it is governed by `run-build`, which is
  publish-only, so the job failed every normal PR. Twelve unit tests passed it,
  because the tests were written to the same wrong model as the code. Unit
  tests confirm the author's understanding of the contract -- they cannot
  detect that the understanding is wrong. Only the real workflow can.
- **The runner is not your machine.** The same job ran `uvx` on a bare
  `ubuntu-latest` with no `setup-uv` step and exited 127 on a run where every
  other job was green. Nothing local exercises the runner's PATH.

And a trap in reading the result: a genuine flake can sit on top of a genuine
failure. A `pip-audit` timeout against pypi.org masked the first of those bugs,
the re-run looked like the reasonable response, and it cost two rehearsals.
When a rehearsal fails, read every failing job before deciding any of them is
transient.

And the rehearsal itself can be the thing that lies. A fixture takes its
WORKFLOW from `@main` the instant it merges, and its CLI from PyPI on a
release. So a rehearsal that does not pass `HYPERCI_INSTALL_OVERRIDE` runs the
PUBLISHED CLI against the branch's workflow -- it exercises the half that
arrived and silently skips the half that has not shipped.

That cost the fleet a broken Rust test leg. A composite action gained a verify
step; the rehearsal ran the INSTALL against the published CLI, never reached
the verify, and came back green. The step was wrong in a way a single real run
would have shown.

A rehearsal that can quietly run the old code is worse than none, because it
returns a green you believe. Pass the override, or say out loud which half you
tested.

Two coverage holes in the same family, both structural rather than missed:

- **A composite action's steps only run inside a job for that language.** The
  Rust verify steps cannot execute in this repo's CI at all, because this repo
  has no Rust. The green tick was honest about everything it could reach.
- **The fast channel is the workflow, the slow one is the wheel.** Anything
  that ships through both in one commit reaches consumers in halves. It turns
  fixtures RED and consumers falsely GREEN depending on direction.

### Turning on a check that was silently off is a behaviour change

Rust coverage never ran until `23c7086` made it run. That commit reads as a
fix. For consumers it was a behaviour change, and it broke a repo the same day.

`cargo llvm-cov` builds into `target/llvm-cov-target` and passes it as the
`--target-dir` FLAG. The flag does not set `CARGO_TARGET_DIR`, so a test that
builds a binary path from that variable reads it as unset, falls back to
`target/debug/`, and cannot find a binary the build definitely produced.

dfe-transform-vrl hand-rolled the path and broke. dfe-transform-vector asks
Cargo with `env!("CARGO_BIN_EXE_<name>")`, which resolves at compile time to
the binary just built, and was immune. That is the idiom for a test that
executes its own binary; a path assembled by hand is correct only until
something redirects the build.

The general shape: enabling a stage that was dormant runs consumer code that
has never run in CI, so its latent bugs all surface at once and look like a
regression in whatever merged that day. Say which stage started running, and
expect the first failures to be in the consumers rather than in the change.

### A check that reports success over what it never ran

Five of this repo's checks were green over work they had not done: a CI gate
that read `== skipped` and so passed a plan job that had FAILED; a
public-API check nothing installed, so every release took the missing-tool
branch and returned 0; that same check reading cargo's error code 101 as a
breaking change; `test.coverage` honoured up to the point the tool would run,
then running the tests plain; and a subcommand gate that asked an unpinned
`uvx` what was published and got an hour-old answer from a cached index.

The test that finds them, before writing any check:

> ask what the check prints when the thing it measures did not happen at all.
> If that is the same as success -- 0, silence, "ok" -- the check is decorative.

Make absence LOUD: a missing tool fails rather than skips, a skipped stage is
not a passed stage, a requested-but-unrun step is an error. Then test the
NEGATIVE path -- a test that only ever sees the tool present proves nothing
about the branch that ships.

That question catches four of the five. It does not catch the subcommand gate,
which printed a finding rather than a pass -- it did not miss a problem, it
invented one, because a cached index answered where PyPI should have. So the
underlying rule is wider than absence:

**A check has THREE outcomes, and collapsing the third is the defect.** Pass,
fail, and "could not determine". Four of these folded "could not run" into
pass; the fifth folded "could not resolve" into fail. Both directions destroy
the same information, and the second is worse in one respect -- a false pass
gets found eventually by the bug shipping, a false failure trains people to
ignore the check.

So ask it in both directions: what does this print when it could not run, and
what does it print when it could not get a trustworthy answer? If either
matches pass or fail rather than saying which, wire the third outcome.

Knowing the rule is not enough on its own. Two of those five were in code its
author had merged and self-reviewed the same day. Ask the question of the
artefact, not of yourself.

Full treatment: `standards/universal/testing.md`, "A green check that never
ran".

### Decoration by construction, and the weaker check that covers for it

Three doc checks -- lychee, markdownlint, the mermaid grammar layer -- warned
on every run of every repo for months. Each message was honest and said the
tool was missing. What made them decoration rather than a gap is that there
was no install path ANYWHERE: not in `versions.yaml`, not in the installer,
not baked into a runner image. A check that cannot run in any environment we
have is not warn-tier, and a permanent warning teaches people to stop reading
warnings.

The part that hid it for months is worth more than the fix. `doc-paths` is
DELIBERATELY disabled whenever lychee would run -- so the weaker check stood
in for the stronger one, permanently, and caught enough to look like coverage.
From outside, the system appeared to be working. A fallback that silently
becomes the only path produces a signal indistinguishable from the real one.

So when one check defers to another, ask which one is actually running. If the
answer is always the fallback, the primary is not a check.

The generic question this raises, which is bigger than three binaries: what
gate lets a check ship with no install path at all? Three did. The fix for
each is an afternoon of pinning; the fix for the class is asking, when a check
is added, where the tool comes from on a runner.

### A log records that a step ran. Only the artefact records what survived

No DFE binary in production carried BOLT. Four shipped files pulled from
downloads.hyperi.io and read by ELF section -- `dfe-receiver-linux-{amd64,arm64}`
and `dfe-loader-linux-{amd64,arm64}` -- and not one of them had
`.note.bolt_info`.

The release logs reported BOLT success and were not lying. cargo-pgo optimised
a real binary and named it `<binary>-bolt-optimized`. Packaging then copied the
unsuffixed PGO-only file sitting beside it. The log was accurate about a file
that no longer mattered.

That is the shape worth carrying: a step's log can only report what the step
did. Where one stage writes a file and a later stage decides which file ships,
nothing the first stage prints is evidence about the release.

The check that closes it reads the packaged file. `_verify_bolt_shipped`
(`languages/rust/build.py`) opens what packaging wrote and fails the build when
a target reported as BOLT-optimised carries no `.note.bolt_info`.
`tier2_shortfall` does the same for the stages a tier promised. Both landed in
`f812aec`, after the last DFE releases, so neither ran against a real
application until `ci-test-rust-simple` run 35812543453 printed `BOLT verified
in ci-test-rust-simple-linux-amd64 (.note.bolt_info)` and the arm64 equivalent.

So for any optimise, strip or sign step: the log line proves the tool ran. Read
the artefact to find out whether the tool's output is what shipped.

### Ask for the thing you expect, not the wide question you then filter

Two tools answered a wider question than the one put to them, and the wide
answer read as an answer.

**The unversioned PyPI endpoint is a cache. The versioned one is the fact.**
`https://pypi.org/pypi/<pkg>/json` served the PREVIOUS release for minutes
after a successful publish, while `https://pypi.org/pypi/<pkg>/<version>/json`
already carried the new one. It hit twice in one hour on different packages --
hyperi-ci 2.10.6, and scalo in a separate workstream, where it nearly got a
good publish reported as failed. Ask for the version you EXPECT and check it
exists. Do not ask what the latest is and compare, because that question has a
cached answer and no way to tell you it is cached.

**A branch filter is not a workflow filter.** `gh run list --branch main
--limit 2` returns rows from every workflow in the repo. It made a red CI run
look green on logreducer, and on dfe-schemas it made a chronological Gate
rollout look like runs with a missing gate. Three instances in one day across
two workstreams. Pass `--workflow CI --branch main`.

One mistake underneath both: a query whose result set is wider than the
question. Narrow it at the source, because filtering a wide answer by eye is
how both of these got read wrong.

### A frozen derived value is harmless until something reads it

`_get_native_target()` returned a hardcoded `x86_64-unknown-linux-gnu` for
every Linux host, for years, and nothing noticed. A cross-build guard added on
2026-09-23 began comparing the build target against it, and every arm64 Rust
release then failed: the runner read its own target as a cross build, skipped
PGO, and the strict Tier 2 check refused to ship a half-optimised binary.

The freeze was not a defect for the years it sat there, because nothing
consumed it. The new consumer is what turned it into one.

`platform.machine()` had the answer the whole time. So audit by consumer rather
than by value: ask what reads the constant now. A value the system can work out
for itself, written down anyway, is a defect waiting for its first reader.

### Configuration Cascade

Priority (highest wins):
1. CLI flags / function arguments
2. Environment variables (`HYPERCI_*`)
3. `.hyperi-ci.yaml` project config
4. `src/hyperi_ci/config/org.yaml` org defaults
5. `src/hyperi_ci/config/defaults.yaml`
6. Hardcoded in code

### Tool Mode System

Every quality tool supports three modes:
- `blocking` (default): fails CI
- `warn`/`non-blocking`: logs warning, continues
- `disabled`: skipped entirely

Resolution: `HYPERCI_QUALITY_<LANG>_<TOOL>` env > `.hyperi-ci.yaml` > default

### Exclusion Handling

Three-layer:
1. Auto-detect git submodules from `.gitmodules`
2. Fallback: always exclude `ci/`, `ai/`
3. Common artifacts: `.venv`, `node_modules`, `target`, `dist`, etc.
4. Custom: `quality.exclude_paths` in `.hyperi-ci.yaml`

### Secret Scanning (Gitleaks)

- Scan current branch only (`--log-opts=${branch}`), not full history
- Config: `.gitleaks.toml` with path exclusions, commit ignores, regex patterns
- CI: blocking; local dev: warn-only if not installed

### Container Building

- Dockerfile detection: `Dockerfile` > `docker/Dockerfile` > `build/Dockerfile`
- Semver tag expansion: `1.2.3` + `1.2` + `1` + `latest` (main branch only)
- Pre-release versions: NO major/minor tags
- Verification: `docker manifest inspect` with retry (registry propagation delay)

### Helm Charts

- Discovery: `charts/*/Chart.yaml` > `chart/Chart.yaml` > `./Chart.yaml`
- Sync both `version` and `appVersion` in Chart.yaml
- OCI registry: `helm package` then `helm push` (two-step)

### Binary Publishing

- Naming: `{binary}-{version}-{os}-{arch}`
- Checksums: `SHA256SUMS` file (not per-file `.sha256`)
- Latest: copy to `/latest/` + `LATEST_VERSION.txt` (skip snapshots)
- GitHub Actions strips executable permissions - restore before publish

### Publish Verification

- All registries: retry loop (5 retries, 10s delay default)
- Always handle "already exists" as success (idempotent re-runs)

### CI Detection & Output

- `is_ci()`: check `CI`, `GITHUB_ACTIONS`, `GITLAB_CI`, `JENKINS_URL`, `BUILDKITE`
- Interactive terminal: colours + emojis
- CI (GitHub Actions): `::error::`, `::warning::`, `::group::` workflow commands
- Piped/file: `[LEVEL] RFC3339 timestamp message`

### Resource Allocation

- CI: use all cores (`nproc`)
- Local dev: default 2 parallel jobs (conservative)
- Override: `LOCAL_PARALLEL_JOBS` env or `local.parallel_jobs` config

---

## Lessons from New CI Implementation (2026-03)

### Sysroot Location (ARC Runners)

- **Never use `/tmp` for sysroot** on ARC (Actions Runner Controller) runners
- ARC runners use pod ephemeral storage for `/tmp` - the same disk that
  causes pod evictions when it fills up
- **Solution:** Use `Path.cwd() / ".tmp" / "cross-sysroot"` (project-scoped,
  gitignored, survives across CI steps in the same job)
- Also aligns with project coding standards: "never hardcode `/tmp`"

### Cross-Compilation: Both gcc AND g++ Wrappers Required

- CMake-based `-sys` crates (e.g. `rdkafka-sys`) need BOTH a C compiler
  wrapper AND a C++ compiler wrapper in the sysroot `bin/` directory
- Only creating the gcc wrapper causes CMake to fail with:
  `CMAKE_CXX_COMPILER: .../aarch64-linux-gnu-g++ is not a full path`
- **Solution:** Generate both `{triple}-gcc` and `{triple}-g++` wrappers
  with identical flags (`-fuse-ld=bfd`, sysroot paths)
- Point `CC_<TARGET>` and `CXX_<TARGET>` env vars to the sysroot wrappers

### Go Publish: Binaries, Not Modules

- Go modules publish automatically to proxy.golang.org on tag push
- Publishing Go binaries means uploading **built binaries** to a release
  store, NOT running `go mod download`
- Binary naming convention: `{binary}-{version}-{goos}-{goarch}[.exe]`
- Checksums: `checksums.sha256` file alongside binaries (renamed to
  `SHA256SUMS` on upload)

### npm Config Pollution

- `npm config set registry=...` modifies **global** `~/.npmrc`
- On CI runners this persists across jobs sharing the same home directory
  (especially ARC runners with shared NFS-backed `RUNNER_HOME`)
- **Solution:** Write a project-local `.npmrc` file with registry + auth,
  then restore/remove it after publishing (try/finally pattern)
- Same principle: never modify global state when project-local config works

### Publish Verification Retry Pattern

- Registries have indexing lag: a just-uploaded artifact may 404 for 5-30 seconds
- All publish handlers should verify with HTTP HEAD + retry loop
- Default: 5 retries, 10 second delay (total ~50s worst case)
- Shared helper in `common.py:verify_publish()` - reusable across all languages
- The old CI had this per-language; the new CI centralises it

### ARC Persistent Cache + Rust Cross-Compilation (Milestone: 2026-03)

**The problem:** ARC runners keep `target/` between runs. With correct
cross-compilation env vars, the first run compiles aarch64 objects correctly
and everything works. But if an earlier run compiled with the wrong compiler
(e.g. host x86_64 gcc instead of `aarch64-linux-gnu-gcc`), those stale `.o`
files persist in the OUT_DIR. Subsequent runs see no source changes and skip
recompilation - the linker gets x86_64 objects and fails with `EM:62`.

This is the *cache pays off / cache bites you* duality. We want the cache
(warm builds are ~5x faster) but must detect and evict corrupt entries.

**Root causes (three layers, all must be fixed):**

1. **Plain `CC` not set for configure-based `-sys` crates.**
   `rdkafka-sys` default build uses `./configure && make` (mklove), NOT cmake.
   The `./configure` script reads `CC` from the environment directly - it does
   NOT use the cc-crate's `CC_aarch64_unknown_linux_gnu` convention.
   Without `CC` set, `./configure` picks up the host `gcc` (x86_64) even when
   building for aarch64, silently producing x86_64 objects.
   **Fix:** Set BOTH `env["CC"] = cc` AND `env[f"CC_{target_lower}"] = cc`
   in `_cross_env()`. Same for `CXX` and `AR`.

2. **Stale detection only scanned cmake-based crates.**
   The stale rlib detector checked only packages with a `cmake` dependency in
   `Cargo.lock`. `rdkafka-sys` without the `cmake-build` feature has no cmake
   dependency - it was invisible to the scanner.
   **Fix:** Scan ALL packages ending in `-sys` regardless of build system.
   Any `-sys` crate may compile C code. The regex `name = "...-sys"` in
   `Cargo.lock` catches them all. Renamed `_find_cmake_sys_crates()` ->
   `_find_c_sys_crates()`.

3. **Persistent OUT_DIR defeats `make libs` recompilation.**
   Even with `CC` now correct, `make libs` in an existing OUT_DIR sees
   unchanged source and reuses stale `.o` files. The Makefile has no concept
   of "cross-compiler changed" - it only checks source timestamps.
   **Fix:** The stale rlib detector reads each rlib with `ar p | file -`,
   checks the ELF machine type, and runs `cargo clean --package <pkg>
   --target <target>` when wrong-arch objects are found. This deletes the
   OUT_DIR entirely, forcing a fresh `./configure && make` with the correct CC.

**The cake-and-eating-it result:**

- First run after contamination: stale rlibs detected, OUT_DIR cleaned,
  full recompile (~19 min with rdkafka). Cache is now correct.
- All subsequent runs: stale detector finds correct arch, skips clean,
  warm cache used. Build time drops to ~3-5 min.
- No unnecessary cache invalidation for native x86_64 builds.

**Config gotcha:** `build.strategies` in `.hyperi-ci.yaml` only accepts
`native` for Rust. Cross-targets are handled via `build.rust.targets`.
The value `cross` is not a valid strategy and will error. Remove it from
any consumer config that inherited it from an old template.

**Publish gotcha:** `cargo publish` runs `cargo package --verify` which
does a clean rebuild. The publish runner (`ubuntu-latest`) lacks native
build tools (`protoc`, `librdkafka-dev` etc.) needed by build scripts.
Since the CI build step already verified everything, use `--no-verify`.
Exit code 101 from `cargo publish` = package verification failed (not auth).
