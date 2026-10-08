# CI Lessons Learned

From Derek >>

Gotchas and fixes from the old HyperI CI (`hyperi-io/ci`, checked out at `/projects/ci`) and from building hyperi-ci. Read the language section before you write or debug a handler. Read the debugging lessons before you trust a green run.

Language handlers:

- [Rust cross-compilation](#rust-cross-compilation)
- [ARC Persistent Cache + Rust Cross-Compilation](#arc-persistent-cache--rust-cross-compilation)
- [Rust publishing, quality and testing](#rust-publishing-quality-and-testing)
- [Go](#go)
- [TypeScript](#typescript)
- [Python uv index strategy](#python-uv-index-strategy)
- [Python packaging and quality](#python-packaging-and-quality)
- [Python testing and publishing](#python-testing-and-publishing)
- [Shared handler patterns](#shared-handler-patterns)

Debugging and verification:

- [A workflow change is not proven by a green test suite](#a-workflow-change-is-not-proven-by-a-green-test-suite)
- [The right idiom sitting in the same file does not propagate](#the-right-idiom-sitting-in-the-same-file-does-not-propagate)
- [A check can be correct, wired up, and unable to run where it runs](#a-check-can-be-correct-wired-up-and-unable-to-run-where-it-runs)
- [A cause that explains every symptom is still not the cause](#a-cause-that-explains-every-symptom-is-still-not-the-cause)
- [Improving a check inside a wrong frame feels exactly like progress](#improving-a-check-inside-a-wrong-frame-feels-exactly-like-progress)
- [A gate nobody reads costs the same as a gate that is off](#a-gate-nobody-reads-costs-the-same-as-a-gate-that-is-off)
- [A passing test answers a question only over the inputs it generates](#a-passing-test-answers-a-question-only-over-the-inputs-it-generates)
- [An empty result answers a question only if the query could have returned something](#an-empty-result-answers-a-question-only-if-the-query-could-have-returned-something)
- [A symptom is a class, a cause is an instance](#a-symptom-is-a-class-a-cause-is-an-instance)
- [A comment can be true about the design and false about the observable](#a-comment-can-be-true-about-the-design-and-false-about-the-observable)
- [A run that predates the fix cannot have tested it](#a-run-that-predates-the-fix-cannot-have-tested-it)
- [Turning on a check that was silently off is a behaviour change](#turning-on-a-check-that-was-silently-off-is-a-behaviour-change)
- [A check that reports success over what it never ran](#a-check-that-reports-success-over-what-it-never-ran)
- [Decoration by construction, and the weaker check that covers for it](#decoration-by-construction-and-the-weaker-check-that-covers-for-it)
- [A log records that a step ran. Only the artefact records what survived](#a-log-records-that-a-step-ran-only-the-artefact-records-what-survived)
- [Ask for the thing you expect, not the wide question you then filter](#ask-for-the-thing-you-expect-not-the-wide-question-you-then-filter)
- [A frozen derived value is harmless until something reads it](#a-frozen-derived-value-is-harmless-until-something-reads-it)

---

## Rust cross-compilation

Release builds run natively on each arch ([runtime/runners.md](runtime/runners.md)). This path runs only when `build.rust.targets` names a target the runner is not, and its code is in `languages/rust/build.py`.

**The mold linker problem.** A GitHub runner may default to `-fuse-ld=mold`. A cross-compiler such as `aarch64-linux-gnu-gcc` cannot find `ld.mold` for a foreign target, so CMake test compiles fail. Force GNU BFD with `-fuse-ld=bfd`, and clear `LDFLAGS` / `CFLAGS` / `CXXFLAGS` so host flags do not leak into the cross build.

**Private sysroot.** Many `-dev` packages (e.g. `libsasl2-dev`) are not `Multi-Arch: same`, so installing the arm64 one removes the amd64 one and breaks native builds.

- Download the cross-arch `.deb` files and extract them into a private sysroot. Point `PKG_CONFIG_PATH` and the linker at it.
- Install only the cross-compilers system-wide (`gcc-aarch64-linux-gnu`, `g++-aarch64-linux-gnu`), plus `libc6-dev:arm64` for the dynamic linker. Those are Multi-Arch safe.
- Put the sysroot at `.tmp/cross-sysroot/` in the workspace, never `/tmp`. On ARC, `/tmp` is pod ephemeral storage, the same disk whose filling evicts the pod.

**Linker wrappers.** Generate BOTH `{triple}-gcc` and `{triple}-g++` wrappers in the sysroot `bin/`, with identical flags. A CMake `-sys` crate such as `rdkafka-sys` fails with `CMAKE_CXX_COMPILER ... is not a full path` when only the C wrapper exists.

- Each wrapper adds `-fuse-ld=bfd`, `-L` and `-rpath-link` for the sysroot. `libsasl2.so` needs `libcrypto.so.3`, which only `-rpath-link` resolves.
- Some `.so` files are ASCII linker scripts with absolute paths (`GROUP ( /lib/aarch64-linux-gnu/libm.so.6 ... )`). Rewrite those paths to point into the sysroot.

**Environment.**

- `CC_<TARGET>`, `CXX_<TARGET>`, `AR_<TARGET>` point at the wrappers, and `CARGO_TARGET_<TARGET>_LINKER` at the linker wrapper.
- `PKG_CONFIG_PATH`, `PKG_CONFIG_SYSROOT_DIR`, `PKG_CONFIG_ALLOW_CROSS=1`, and `CMAKE_PREFIX_PATH` for CMake-based `-sys` crates.
- `CFLAGS_<TARGET>` carries `-fuse-ld=bfd` and the arch include paths.

**Order and checks.** Build the native target first, then cross targets, to dodge multi-arch package conflicts. Run `rustup target add <target>` for each foreign target. After the build, check the binary is over 100KB and its ELF machine type matches (`readelf -h`). Smoke-test native binaries with `--version` or `--help`; a cross-compiled one cannot run.

## ARC Persistent Cache + Rust Cross-Compilation

ARC runners keep `target/` between runs. If an earlier run compiled a `-sys` crate with the host `gcc`, the x86_64 `.o` files stay in its `OUT_DIR`. Later runs see no source change, skip the compile, and the link fails with `EM:62`. We want the warm cache (about 5x faster), so the fix detects and evicts bad entries rather than dropping the cache.

Three causes, all fixed in `languages/rust/build.py`:

1. **Plain `CC` was unset.** `rdkafka-sys` by default builds with `./configure && make` (mklove), not CMake. `./configure` reads plain `CC`, not the cc-crate's `CC_aarch64_unknown_linux_gnu`, so it picked the host `gcc`. `_cross_env()` sets both `CC` and `CC_<target>`, and the same for `CXX` and `AR`.
2. **Stale detection scanned only CMake crates.** `rdkafka-sys` without its `cmake-build` feature has no cmake dependency, so the scanner missed it. `_find_c_sys_crates()` scans every package in `Cargo.lock` whose name ends in `-sys`.
3. **A persistent `OUT_DIR` defeats `make`.** The Makefile checks source timestamps, not which compiler built the objects. The detector reads each rlib with `ar p | file -`, checks the ELF machine, and runs `cargo clean --package <pkg> --target <target>` on a mismatch.

The result: the first run after contamination recompiles in about 19 minutes with rdkafka, and later runs take 3-5 minutes on the warm cache. Native x86_64 builds never lose their cache.

`build.strategies` accepts only `native`. Cross targets go in `build.rust.targets`, and any other strategy, such as `cross` from an old template, fails with `Unknown build strategy` (`dispatch.py`).

## Rust publishing, quality and testing

**Publishing** (`languages/rust/release.py`):

- `cargo publish --allow-dirty --no-verify`. `--allow-dirty` because hyperi-ci stamps `Cargo.toml` before the build. `--no-verify` because verify rebuilds from clean, and the publish runner lacks build-script tools such as `protoc` and `librdkafka-dev`. The Build job already compiled it.
- Exit 101 from `cargo publish` means package verification failed, not auth.
- "already exists" on stderr is success, so a re-run does not fail.

**Quality** (`languages/rust/quality.py`):

- `cargo deny` needs `deny.toml` and skips without one.
- `cargo audit` can fail with `error loading advisory database`. It retries with backoff, and a `blocking` gate fails if the database never loads, as pip-audit does.
- Clippy always gets `-D clippy::dbg_macro`.
- Pipe-separated feature sets (`jemalloc|mimalloc`) run clippy once per set. Cargo `--features` is additive, so one invocation would union the sets.

**Testing** (`languages/rust/test.py`):

- Integration tests run with `--test-threads=1`, because parallel tests fight over ports.
- `cargo nextest` and `cargo test` are not interchangeable. nextest gives each test its own process and cargo test shares one, so process-global state (a metrics recorder, a `OnceLock`) behaves differently. Doctests run only under cargo test.
- Picking by what is installed changes semantics silently: the ARC image bakes nextest in, a hosted runner does not. The `test.rust.nextest` tri-state announces the choice, annotates an `auto` fallback, and fails the stage on `true` with nextest absent.
- Coverage uses tarpaulin, else llvm-cov, both optional. Both drive cargo's own harness, so coverage overrides a resolved nextest runner, and it runs on the first feature set only.

**Workspaces.** Detect them with `cargo metadata --no-deps --format-version 1` (`languages/rust/targets.py`). Write helpers that work for a single crate and a workspace alike.

## Go

Code: `languages/golang/`.

**Build.**

- Each target sets `GOOS` / `GOARCH` and `CGO_ENABLED` in that one call's environment, never the process's, so nothing leaks into later commands. CGO is off unless `build.golang.cgo` is true.
- Target shortcuts `all`, `linux` and `darwin` expand to common matrices. Windows targets are refused.
- `-ldflags` defaults to `-s -w` (strip symbols and DWARF), overridable with `GO_LDFLAGS`. With `GO_VERSION_PKG` set, `-X` injects `<pkg>.Version`, `<pkg>.Commit` and `<pkg>.BuildTime`.
- Main package: `GO_MAIN_PKG` env, else `cmd/{binary}/`, else a single `cmd/` subdirectory, else `.`.
- Output is `dist/{binary}-{os}-{arch}`, published like every other binary ([flow.md](flow.md) section 7).

**Test.** `-race` is on by default (`test.golang.race`). With `-race`, coverage mode must be `atomic`, not `count` or `set`.

**Quality.** golangci-lint runs with a 5 minute timeout, in a strict pass on source and a relaxed pass on tests. gosec and govulncheck run beside it, with the usual `blocking` / `warn` / `disabled` modes.

**Publish.** A Go module needs no upload: proxy.golang.org picks it up from the tag. Publishing Go means uploading built binaries, not running `go mod download`.

## TypeScript

Code: `languages/typescript/`.

- **Package manager.** The `packageManager` pin in `package.json` wins, then the lockfile (`pnpm-lock.yaml`, `yarn.lock`, `package-lock.json`), then npm. A pinned project resolves its manager through Corepack, because a global binary of another version refuses to run it.
- **Audit.** Yarn Berry dropped `yarn audit` for `yarn npm audit --severity`. Yarn Classic, npm and pnpm take `--audit-level`.
- **Lint and types.** ESLint runs the `lint` script, else `npx eslint .` when a flat (ESLint 9+) or legacy config exists. Type checking prefers a `typecheck` or `check-types` script over `tsc --noEmit`, so monorepo tooling stays in charge.
- **Test.** vitest or jest from `devDependencies` (default vitest), overridable with `test.typescript.runner`. A `test:<tier>` script runs when it exists, else `test`.
- **Publish.** `npm pack` runs in a job with no secrets. The tarball is then published with `--ignore-scripts` from an empty directory, so neither package scripts nor the repo's `.npmrc` see the token.

### npm Config Pollution

`npm config set registry=...` writes the GLOBAL `~/.npmrc`, so on a runner that shares its home directory between jobs, the token outlives the job. The publish step writes the token into a throwaway user config, created 0600 in a temporary directory and passed with `--userconfig`. It never touches the global file or the project's.

The rule is wider than npm: never change global state when a scoped config will do.

## Python uv index strategy

**NEVER add `UV_EXTRA_INDEX_URL` to a dependency install step in a reusable workflow.**

uv is not pip. By default it takes the first index that answers for a package name. A private index that returns an empty 200 for `hatchling` stops the search there, and uv reports "no versions found" rather than falling back to PyPI. `UV_INDEX_STRATEGY=unsafe-best-match` works around it, but changes resolver behaviour for every package.

- Workflow install steps stay `uv sync --frozen --all-extras` with no extra index variables (`python-ci.yml`).
- A project that needs a private index declares it under `[tool.uv.index]` with `explicit = true` in its own `pyproject.toml`. An explicit index is consulted only for packages that name it.

## Python packaging and quality

**Build.** CI builds on the Python version the project declares, not a fleet-wide one ([languages/python.md](languages/python.md)). `versions.yaml` names the default for a project that declares nothing. Build with `uv build`, not `python -m build`.

**sdist exclusions.** Hatchling's sdist includes every git-tracked file, which picks up AI agent directories (`.claude/`, `.cursor/`, `.gemini/`, `.windsurf/`), symlinks out of the project, and org submodules (`hyperi-ai/`, `ci/`). `_inject_sdist_excludes()` in `languages/python/build.py` patches `pyproject.toml` for the build and restores it after. It adds:

```text
/.claude  /CLAUDE.md  /.cursor  /CURSOR.md  /.gemini  /GEMINI.md
/.github/copilot-instructions.md  /.windsurf  /STATE.md  /hyperi-ai  /ci
```

A project's own `[tool.hatch.build.targets.sdist] exclude` merges with these. The workflow builds through `hyperi-ci run build`, never raw `uv build`, or the injection does not run.

**Tool exclusions.** `--extend-exclude` adds to a tool's defaults, and `--exclude` replaces them, which for ruff would scan `.venv`.

- ruff: `--extend-exclude dir`
- bandit: `--exclude dir1,dir2`, comma-separated
- pyright: config file only, no CLI exclusion
- eslint: `--ignore-pattern dir`

**Bandit.**

- Skip `B104` (bind all interfaces) in `[tool.bandit] skips` when a container service binds `0.0.0.0` on purpose.
- Skip `B608` (hardcoded SQL) where queries come from internal config templates, not user input.
- Prefer config-level skips to inline `# nosec`.
- `quality.python.bandit_exclude_tests` (default `true`) keeps bandit out of the test paths.

## Python testing and publishing

**Testing** (`languages/python/test.py`):

- `test.use_tiers: true` splits the run into `tests/unit/`, `tests/integration/` and `tests/e2e/`. e2e is off unless `test.tiers.e2e.enabled` is set.
- pytest's no-tests-collected exit counts as a pass unless `test.fail_on_missing` is true.
- A public submodule: declare `submodules: schemas` in `.hyperi-ci.yaml`, and `hyperi-ci init` renders it as the reusable-workflow `submodules` input. The test job and the container build both check it out.
- A private submodule: `GITHUB_TOKEN` cannot clone it. Mark dependent tests `@pytest.mark.skipif(not schemas_dir.exists(), reason="submodule not checked out")`, or wire a cross-repo token.

**Publishing** (`languages/python/release.py`):

- `uv publish --no-config` uploads the Build job's wheel and sdist. `--no-config` stops a project's `[tool.uv] publish-url` sending the token to another host.
- The token goes in `UV_PUBLISH_TOKEN`, not on the command line, where any process on the runner can read it.
- "already exists" from PyPI is success, so a re-run does not fail.

## Shared handler patterns

**Configuration.** The cascade and the three tool modes (`blocking`, `warn`, `disabled`) are in [architecture.md](architecture.md#configuration-cascade) and [quality-gate.md](quality-gate.md).

**Exclusions** (`get_exclude_dirs()` in `common.py`), four layers:

1. Submodule paths from `.gitmodules`.
2. `ci/` and `ai/`, always.
3. Common artefact directories: `.venv`, `node_modules`, `target`, `dist` and the rest.
4. `quality.exclude_paths` from `.hyperi-ci.yaml`.

**Secret scanning.** gitleaks scans the current branch only (`--log-opts <branch>`), not full history. Its config is `.gitleaks.toml` ([quality-gate-tools.md](quality-gate-tools.md#gitleaks-config)).

**Containers.** A container builds only from the repo's own Dockerfile at the configured path, and there is no generated image ([container-builds.md](container-builds.md)). A GA release tags `vX.Y.Z`, `latest` and `sha-<short>`. A prerelease never moves `latest` (`container/build.py`).

**Binaries.** GitHub Actions artefact upload strips the executable bit, so restore it before publish. Naming and per-binary checksums are in [flow.md](flow.md) section 7.

**Idempotent publish.** Every publish handler treats "already exists" as success, so a re-run fills gaps rather than failing.

**CI detection.** `is_ci()` checks `CI`, `GITHUB_ACTIONS`, `GITLAB_CI`, `JENKINS_URL` and `BUILDKITE`. Under GitHub Actions, output uses `::group::` and `::error::` / `::warning::` workflow commands.

---

## A workflow change is not proven by a green test suite

Three bugs in one workflow change got past ~3000 local tests. `scripts/rehearse-branch.py` caught them on a real `ci-test-*` fixture. Rehearse every change to `.github/workflows/` before merging it.

Why the suite cannot find them:

- **A wrong model is tested as confidently as a right one.** A gate job treated `build` as governed by `run-checks`. It is governed by `run-build`, which is publish-only, so the job failed every normal PR. Twelve unit tests passed it, because they encoded the same wrong model as the code.
- **The runner is not your machine.** The same job ran `uvx` on a bare `ubuntu-latest` with no `setup-uv` step and exited 127. Nothing local exercises the runner's PATH.

A real flake can sit on top of a real failure. A `pip-audit` timeout masked the first bug, the re-run looked reasonable, and it cost two rehearsals. When a rehearsal fails, read every failing job before calling any of them transient.

The rehearsal itself can lie. A fixture takes its WORKFLOW from `@main` the instant it merges, and its CLI from PyPI on a release. Without `HYPERCI_INSTALL_OVERRIDE`, a rehearsal runs the PUBLISHED CLI against the branch's workflow.

That cost the fleet a broken Rust test leg. A composite action gained a verify step, and the rehearsal ran the install against the published CLI, never reached the verify, and came back green. Pass the override, or say which half you tested.

Two coverage holes in the same family:

- **A composite action's steps only run inside a job for that language.** This repo has no Rust, so the Rust verify steps cannot run in its CI at all.
- **The workflow ships fast, the wheel ships slow.** A commit that changes both reaches consumers in halves. It turns fixtures RED and consumers falsely GREEN, depending on direction.

## The right idiom sitting in the same file does not propagate

`scan_code_paths` blanked fenced code blocks before scanning. `scan_links`, three functions above it in the same file, did not. A C++ lambda capture and a Python generic parameter were read as links to missing files.

The same week, four guard rules used a word-boundary anchor that matched after a hyphen. They denied `make az-delete-report` and `cat docs/find-delete.md`. A fifth rule in that file already anchored on command position, with a comment saying why.

**Nothing flags a helper that half the code forgot to call.** Both callers are exercised and both pass, because the one that skips the helper is not wrong in any way a test asserts.

So when adding a sibling to an existing function, read what the existing one does FIRST and copy it on purpose. When fixing one rule of a class, sweep the class.

## A check can be correct, wired up, and unable to run where it runs

Three in one day, all green or quiet, none of them working.

- `lychee`, `markdownlint-cli2` and the mermaid grammar check each printed an honest "not installed" line inside a passing job. None was in `versions.yaml`, no install path fetched them, and no runner image baked them.
- `osv-scanner` returned `True` on a missing lockfile under every mode. Three repos with no committed `Cargo.lock` reported zero findings, and the zero was silence, not coverage.
- The negative-case catalogue committed a planted patch before pushing it, and an ARC runner has no global git config. Every case failed at `git commit`, so no gate was ever exercised.

**The code was right in all three.** The gap is between the check and where it runs: a tool nothing installs, a missing file, an identity the runner lacks. A unit test supplies the environment the check assumes, so it never sees this.

So a new check is not finished when it passes locally. Name what it needs from the runner -- a binary, a file, a credential, an identity -- and confirm each one exists there. Make the tool say which outcome it reached: ran and passed, ran and failed, or could not run.

## A cause that explains every symptom is still not the cause

A ClickHouse container timed out at exactly its budget. The offered mechanism was an orphaned container from an earlier pod holding the port. The pod spec said `dind-sock: emptyDir{}`, so dockerd is per-pod and no other pod's container was ever visible.

A fixture rehearsal failed at `couldn't find remote ref refs/pull/19/merge`. The offered mechanism was cleanup closing the PR while the job queued. The PR was created at 13:01:35 and checkout failed at 13:01:43, long before cleanup. GitHub computes the merge ref asynchronously and fires the workflow at once.

**Both mechanisms were reasoned, not read.** A cause derived from the symptom always fits the symptom, so fitting is no evidence. A real cause is confirmed by something independent of the symptom: a config file, a timestamp, a second run. Before a cause goes anywhere durable, name the one lookup that would refute it, and make it.

**A lookup is not enough either, because it returns something.** `gh run list --branch main` returned runs four days stale while newer ones existed, and dropping the flag showed current ones. Twenty minutes later both forms returned identical current results. The listing had lagged and settled.

One reading cannot tell a real effect from a transient one. An eventually consistent API, a warm cache or a race hands you transients. So re-run it a DIFFERENT way before the cause is durable: a second query path, a second point in time, an inverted assertion.

## Improving a check inside a wrong frame feels exactly like progress

A search for override entries with no rule behind them returned 34 orphans. Narrowing the method returned 25. Both were wrong: the ids are passed positionally and built with suffixes, so no literal search could see them.

The method was refined twice and the number improved each time. Nobody asked whether the method could work at all. **Progress inside the frame reads as evidence the frame is right, and it is not evidence at all.**

The tell is a number that keeps moving toward what you expected. Before the third refinement, ask what result would mean the method cannot work, and whether you would recognise it.

## A gate nobody reads costs the same as a gate that is off

`doc_paths: warn` and a bats step masked with `continue-on-error` cost the same, because nothing acts on either output. The failure is that the STATED REASON for relaxing a gate stops being true. Nothing notices, because the check that would notice is the one turned down.

One mask claimed 68 references to a retired entry point across 7 files. There were 4 files and no references. The code it protected had been deleted, and the gate that would have said so was the one it silenced.

So state a relaxed gate's reason where a reader will meet it. Re-check that reason on a schedule, because it rots silently.

## A passing test answers a question only over the inputs it generates

Checking that a test DISCRIMINATES -- fails on the broken build, passes on the fix -- proves the ORACLE. It says nothing about the GENERATOR.

A property test for a shell-rewriting hook asked whether the resulting command was `env` holding only assignments. Right predicate, and it failed on the broken build. Its generator only varied SEPARATORS, so every input had a separator or command after the assignments.

A tail that was itself an assignment (`A=1 B=2`) was never generated. The suite passed on a build that still dumped the environment on 185 inputs out of 640.

**Enumeration failures move.** Catching one in hand-written cases pushes it into the generator, where it looks like coverage. The discriminating check tells you the test can fail, never that you asked about the case that matters.

## An empty result answers a question only if the query could have returned something

Absence is evidence of nothing until you know the query could hit. Three readings went wrong on this in one day:

- `gh api repos/O/R/branches/main/protection` returns 404 on a branch that IS protected, because ruleset protection lives at `repos/O/R/rules/branches/main`.
- Grepping a running job's log returns nothing because the log blob does not exist yet (`BlobNotFound`), not because the section has not run.
- Searching for a `.superseded` file returns nothing when no file ever collided. That is not proof the newer-wins rule ran and chose correctly.

Each has two states behind one empty output: the thing is absent, or the question never reached it. Before reading a negative, point the query at a case you know exists.

## A symptom is a class, a cause is an instance

Two jobs that both "stopped early" are one observation repeated. Cancelled-mid-run looks the same whether a merge, a concurrency group or a pod eviction did it.

That produced three wrong mechanisms in one day, each assuming a second instance shared the first one's CAUSE because it shared the OUTCOME. One grep separated them every time. `##[error]The runner has received a shutdown signal` appears in an evicted job and never in a concurrency cancel.

The fix is not vigilance. The second instance gets the SAME evidence standard as the first, not a lower one because it looks like the case you just proved.

## A comment can be true about the design and false about the observable

Each of these was an accurate statement of intent, written by someone who knew the code, and wrong about what happens:

- `publish-target: both` "resolves to release channel and unlocks Tier 2". `_resolve_build_channel` reads `HYPERCI_CHANNEL`, then the tag ref, then `RUST_VERSION` / `CI_COMMIT_TAG`, else `alpha`. The same workflow declares the input "legacy field, ignored".
- `reap_stale` promises a leftover container makes the start fail with `name is already in use`. What arrives is `WaitContainer(StartupTimeout)`, because testcontainers swallows the Docker error and reports a timeout.
- "Two concurrent runs of this suite on one machine share these names." True on a laptop. On ARC each runner pod has its own dockerd over an `emptyDir` socket, so nothing is shared.

All three were believed and reasoned from. Two cost a diagnosis each, and the third sent two sessions after a mechanism that cannot occur.

Nothing detects this class: a linter sees a comment, and a test exercises the code, not the sentence beside it. Check the claim against the layer that owns it -- the resolver, the library's real error, the pod spec -- BEFORE reasoning from it.

## A run that predates the fix cannot have tested it

Check timestamps before reading a verdict. A consumer CI run installs the CLI with an unpinned `uvx hyperi-ci`, so it gets whatever PyPI's latest was AT THAT MOMENT. The error is identical either way.

Caught once by two timestamps:

```text
run createdAt              2026-09-23T02:36:31Z
the wheel's upload_time    2026-09-23T04:18:14Z
```

102 minutes apart, so the fix was never in the binary under test. The unchanged error was read as the fix not working.

The same fault has produced four wrong readings:

- `gh run list --branch main` returning rows from every workflow
- a repo whose main skips Test having no baseline to compare against
- the unversioned PyPI endpoint serving a cached answer
- this one

**In each case the result set was wider or older than the question, and the filtering happened by eye.** Ask the narrow question: name the workflow, name the version, read the timestamp.

## Turning on a check that was silently off is a behaviour change

Rust coverage never ran until `23c7086` made it run. That commit reads as a fix, but for consumers it was a behaviour change, and it broke a repo the same day.

`cargo llvm-cov` builds into `target/llvm-cov-target` and passes it as the `--target-dir` FLAG. The flag does not set `CARGO_TARGET_DIR`. A test that builds a binary path from that variable falls back to `target/debug/` and cannot find the binary.

dfe-transform-vrl hand-rolled the path and broke. dfe-transform-vector asks Cargo with `env!("CARGO_BIN_EXE_<name>")`, which resolves at compile time to the binary just built, and was immune. That is the idiom for a test that runs its own binary.

Enabling a dormant stage runs consumer code that has never run in CI. Its latent bugs all surface at once and look like a regression in whatever merged that day. Say which stage started running, and expect the first failures in the consumers.

## A check that reports success over what it never ran

Five of this repo's checks were green over work they had not done:

- a CI gate that read `== skipped`, and so passed a plan job that had FAILED
- a public-API check nothing installed, so every release took the missing-tool branch and returned 0
- that same check reading cargo's exit 101 as a breaking change
- `test.coverage` honoured up to the point the tool would run, then running the tests plain
- a subcommand gate that asked an unpinned `uvx` what was published and got an hour-old cached answer

Before writing any check, ask what it prints when the thing it measures did not happen at all. If that matches success -- 0, silence, "ok" -- the check is decorative. Make absence LOUD: a missing tool fails, and a skipped stage is not a passed stage. Then test the NEGATIVE path.

That question catches four of the five. The subcommand gate printed a finding, not a pass: a cached index answered where PyPI should have, so it invented a problem.

**A check has THREE outcomes, and collapsing the third is the defect.** Pass, fail, and "could not determine". Four of these folded "could not run" into pass, and the fifth folded "could not resolve" into fail. A false pass is found when the bug ships, but a false failure trains people to ignore the check.

So ask both: what does this print when it could not run, and when it could not get a trustworthy answer? Two of the five were in code its author had merged and self-reviewed the same day, so ask the artefact, not yourself. Full treatment: `standards/universal/testing.md`, "A green check that never ran".

## Decoration by construction, and the weaker check that covers for it

Three doc checks -- lychee, markdownlint, the mermaid grammar layer -- warned on every run of every repo for months. Each said the tool was missing, and there was no install path ANYWHERE: not `versions.yaml`, not the installer, not a runner image. A check that cannot run in any environment we have is not warn-tier, and a permanent warning teaches people to stop reading warnings.

What hid it is the bigger lesson. `doc-paths` is disabled on purpose whenever lychee would run, so the weaker check stood in for the stronger one permanently. It caught enough to look like coverage.

So when one check defers to another, ask which one is actually running. If the answer is always the fallback, the primary is not a check. When a check is added, ask where its tool comes from on a runner.

## A log records that a step ran. Only the artefact records what survived

No DFE binary in production carried BOLT. Four shipped files from downloads.hyperi.io -- `dfe-receiver-linux-{amd64,arm64}` and `dfe-loader-linux-{amd64,arm64}` -- had no `.note.bolt_info` section.

The release logs reported BOLT success and were not lying. cargo-pgo optimised a real binary and named it `<binary>-bolt-optimized`. Packaging then copied the unsuffixed PGO-only file beside it.

Where one stage writes a file and a later stage decides which file ships, nothing the first stage prints is evidence about the release.

`_verify_bolt_shipped` (`languages/rust/build.py`) opens what packaging wrote. It fails the build when a target reported as BOLT-optimised carries no `.note.bolt_info`, and `tier2_shortfall` does the same for the stages a tier promised. `ci-test-rust-simple` run 35812543453 printed `BOLT verified in ci-test-rust-simple-linux-amd64 (.note.bolt_info)`, and the arm64 equivalent.

For any optimise, strip or sign step, the log proves the tool ran. Read the artefact to find out whether its output shipped.

## Ask for the thing you expect, not the wide question you then filter

**The unversioned PyPI endpoint is a cache. The versioned one is the fact.** `https://pypi.org/pypi/<pkg>/json` served the PREVIOUS release for minutes after a publish. `https://pypi.org/pypi/<pkg>/<version>/json` already carried the new one. It hit twice in one hour, on hyperi-ci 2.10.6 and on scalo, where it nearly got a good publish reported as failed.

Ask for the version you EXPECT and check it exists. Asking what the latest is has a cached answer, and nothing tells you it is cached.

**A branch filter is not a workflow filter.** `gh run list --branch main --limit 2` returns rows from every workflow in the repo. It made a red CI run look green on logreducer, and on dfe-schemas it made a chronological Gate rollout look like runs with a missing gate. Pass `--workflow CI --branch main`.

Both are a query whose result set is wider than the question. Narrow it at the source, because filtering a wide answer by eye is how both got read wrong.

## A frozen derived value is harmless until something reads it

`_get_native_target()` returned a hardcoded `x86_64-unknown-linux-gnu` on every Linux host for years, and nothing noticed. Then a cross-build guard began comparing the build target against it. Every arm64 Rust release failed: the runner read its own target as a cross build, skipped PGO, and the strict Tier 2 check refused the half-optimised binary.

The freeze was harmless while nothing consumed it. The new consumer turned it into a defect. `platform.machine()` had the answer the whole time.

Audit by consumer, not by value: ask what reads the constant now. A value the system can work out for itself, written down anyway, is a defect waiting for its first reader.
