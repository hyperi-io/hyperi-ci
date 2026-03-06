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
- Many `-dev` packages (e.g. `libsasl2-dev`) are NOT `Multi-Arch: same` —
  installing arm64 replaces amd64, breaking native builds
- **Solution:** Download cross-arch `.deb` files, extract to private sysroot
  (`/tmp/cross-sysroot/<arch>/`), point `PKG_CONFIG_PATH` and linker at it
- Only install cross-compilers system-wide (they ARE Multi-Arch safe):
  `gcc-aarch64-linux-gnu`, `g++-aarch64-linux-gnu`
- Also install `libc6-dev:arm64` (provides dynamic linker and standard libs)

**Linker wrapper script:**
- Creates wrapper around cross-compiler that injects sysroot library paths
- Uses `-fuse-ld=bfd` (forces GNU BFD linker, not mold)
- Includes `-L` and `-rpath-link` flags for transitive `.so` dependencies
- Example: `libsasl2.so` needs `libcrypto.so.3` — linker needs `-rpath-link`

**GNU LD script path patching:**
- Some `.so` files are ASCII linker scripts with absolute paths:
  `GROUP ( /lib/aarch64-linux-gnu/libm.so.6 ... )`
- These absolute paths don't exist on host — rewrite to point at sysroot

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

### Cargo Registry Auth (JFrog)

- Credentials in `$CARGO_HOME/credentials.toml`:
  `[registries.hyperi]\ntoken = "Bearer <TOKEN>"`
- Config in `$CARGO_HOME/config.toml`:
  `[registries.hyperi]\nindex = "sparse+https://..."\ncredential-provider = "cargo:token"`
- `credential-provider = "cargo:token"` required for Cargo 1.74+
- Respect `CARGO_HOME` env var (ARC runners set it to NFS cache path)
- Permissions: `chmod 600` on credentials.toml

### Publishing

- `cargo publish --allow-dirty` (semantic-release modifies files pre-publish)
- Handle "already exists" gracefully (grep stderr, treat as success)
- Git dependency patching: replace `git = "https://..."` with
  `version = "1", registry = "hyperi"` before publish, restore after
- Intra-workspace path deps: add `registry = "hyperi"` (cargo publish strips
  `path=` but defaults to crates.io without explicit registry)

### Quality

- `cargo deny` requires `deny.toml` — skip if not present
- `cargo audit` may fail with "error loading advisory database" — skip gracefully
- Clippy: force `-D clippy::dbg_macro` to prevent debug macros in production
- Multi-feature testing: pipe-separated `RUST_FEATURES` runs clippy per set

### Testing

- Integration tests: default to 1 test thread (port conflicts with parallelism)
- Prefer `cargo nextest` over `cargo test` (faster, better output)
- Coverage: tarpaulin > llvm-cov, both optional
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

- Binaries to Artifactory via `curl -T` (PUT)
- Checksums uploaded as `SHA256SUMS` (not `.sha256`, avoids JFrog checksum API conflict)
- Latest reference: copy to `/latest/` + `LATEST_VERSION.txt`
- Skip latest for snapshots (`^snapshot-` prefix)
- Container: `docker buildx build --platform linux/amd64,linux/arm64`

---

## TypeScript

### Package Manager Detection

- Lockfile-based: `pnpm-lock.yaml` > `yarn.lock` > default `npm`
- Auto-install pnpm/yarn via `npm install -g` if not found

### Registry Auth

- `.npmrc` with `_authToken` (same pattern as Cargo Bearer token)
- Fallback: if JFrog fails, retry with public npm (remove `.npmrc`)

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
- Verification: use Artifactory **storage API** (more reliable than npm API,
  no indexing delay)

---

## Python

### uv Patterns

- CI uses Python 3.12 (has `tomllib` built-in)
- JFrog: URL-encode credentials, set `UV_INDEX_URL`, `UV_INDEX`, `UV_LINK_MODE=copy`
- Build: `uv build` (replaces `python -m build`)
- Publish: `uv publish --publish-url` with explicit credentials

### Quality Tool Exclusions (Critical)

- `--extend-exclude` ADDS to defaults (safe)
- `--exclude` REPLACES defaults (dangerous — would scan `.venv`)
- Each tool has different exclusion syntax:
  - ruff: `--extend-exclude dir`
  - bandit: `--exclude dir1,dir2` (comma-separated, prefix with `./`)
  - pyright: config-file only (no CLI exclusions)
  - eslint: `--ignore-pattern dir`

### Testing

- Tiered: `tests/unit/`, `tests/integration/`, `tests/e2e/`
- Detection: directory-based > marker-based > conftest-based
- Coverage: separate `.coverage.<tier>` files, combine at end
- No test directory: exit 0 (graceful skip)

### Publishing

- Verification: query JFrog simple API, check for version in HTML response
- Handle both naming conventions: `package-name-version` and `package_name-version`
- JFrog indexing takes minutes — retry loop (5 retries, 10s delay)

---

## Cross-Language Patterns

### Configuration Cascade

Priority (highest wins):
1. CLI flags / function arguments
2. Environment variables (`HYPERCI_*`)
3. `.hyperi-ci.yaml` project config
4. `config/org.yaml` org defaults
5. `config/defaults.yaml`
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
- GitHub Actions strips executable permissions — restore before publish

### Publish Verification

- All registries: retry loop (5 retries, 10s delay default)
- JFrog: use storage API (more reliable than package API)
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
