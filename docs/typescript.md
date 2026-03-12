# TypeScript — Package Manager Support

## Summary of Implementation

Projects may use npm, yarn, or pnpm. The `packageManager` field in `package.json` is authoritative; Corepack enforces it. Hardcoding a single package manager in CI causes failures for projects that use a different one (e.g. `"packageManager": "yarn@1.22.0"`).

### Steps Taken

1. **Package manager detection**
   - Added `detect_package_manager()` in `src/hyperi_ci/languages/typescript/_common.py`
   - **Priority order:** `package.json` `packageManager` field → lock files → default `npm`
   - Lock file fallback: `pnpm-lock.yaml` > `yarn.lock` > `package-lock.json`

2. **Shared detection**
   - Updated `quality.py`, `test.py`, and `build.py` to use `_common.detect_package_manager()` instead of per-handler lockfile heuristics

3. **`install-deps` command**
   - Added `hyperi-ci install-deps typescript` in `src/hyperi_ci/install_deps.py`
   - Runs `corepack enable`, detects package manager, then runs:
     - **pnpm:** `pnpm install --frozen-lockfile`
     - **yarn:** `yarn install --frozen-lockfile`
     - **npm:** `npm ci` (or `npm install` if no lock file)

4. **Workflow changes**
   - Replaced "Install pnpm" with "Enable Corepack" in `ts-ci.yml`
   - Replaced `pnpm install --frozen-lockfile` with `hyperi-ci install-deps typescript`
   - Reordered steps so uv is installed before any `hyperi-ci` invocation

---

## Local Testing

### Run install-deps in a TypeScript project

```bash
cd /path/to/typescript-project
uvx hyperi-ci install-deps typescript
```

Or from the hyperi-ci repo:

```bash
uv run hyperi-ci install-deps typescript -C /path/to/typescript-project
```

Output shows the detected manager: `Using yarn (detected from package.json or lock file)`.

### Test detection with different setups

Create temporary projects to verify each path:

```bash
# Yarn (from packageManager field)
mkdir -p /tmp/test-yarn && cd /tmp/test-yarn
echo '{"name":"x","packageManager":"yarn@1.22.0"}' > package.json
uv run hyperi-ci install-deps typescript -C /tmp/test-yarn

# pnpm (from packageManager field)
mkdir -p /tmp/test-pnpm && cd /tmp/test-pnpm
echo '{"name":"x","packageManager":"pnpm@9.0.0"}' > package.json
touch pnpm-lock.yaml
uv run hyperi-ci install-deps typescript -C /tmp/test-pnpm

# Lock-file fallback (no packageManager)
mkdir -p /tmp/test-lock && cd /tmp/test-lock
echo '{"name":"x"}' > package.json
touch yarn.lock
uv run hyperi-ci install-deps typescript -C /tmp/test-lock
```

### Run quality, test, and build stages

In a real TypeScript project:

```bash
cd /path/to/typescript-project
uv run hyperi-ci run quality
uv run hyperi-ci run test
uv run hyperi-ci run build
```

These stages use the detected package manager for `{pm} run lint`, `{pm} run test`, and `{pm} run build`.

---

## Related

- `docs/CI-LESSONS.md` — TypeScript section for quality, publishing, and other patterns
- `docs/DESIGN.md` — Architecture and workflow structure
