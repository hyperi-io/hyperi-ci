# HyperI CI — Project State

## Background

This is a ground-up rewrite of the HyperI CI system. The previous CI
(`hyperi-io/ci`) grew organically from its GitLab origins — running both
local and cloud GitLab concurrently — before migrating to GitHub Actions.
That evolution produced ~100 shell scripts, 26 Python scripts, 50+
composite actions, a 1020-line `attach.sh`, and delivery via git
submodule across 14+ consumer projects. The dispatch hierarchy reached
six layers deep (workflow → action → bash → Python → bash → tools),
with config settable in four different places and significant dead code.

We now have a defined, mature pattern for what CI actually needs to do
across our polyglot stack. This repo rationalises everything back into a
single Python CLI tool (`hyperi-ci`), distributed as a standard package
via `uv tool install`. Consumer projects get a five-line reusable
workflow and a Makefile — no submodules, no composite actions, no bash
dispatch chains. Same tool runs locally and in CI.

The old repo (`hyperi-io/ci`) will be archived read-only once all 14
consumer projects have cut over.

## Hard Design Principles

### 1. NO BASH — Python Only

**This is the #1 non-negotiable rule.**

70% of CI failures in the old system were caused by bash syntax peculiarities:
env expansion, variable substitution, quoting, and jq parsing. We cannot afford
the engineering time to debug these.

- **ALL CI logic is Python.** No `.sh` files, no `shell=True`, no bash fallbacks.
- **subprocess.run()** with list args only — never string commands.
- Language tool invocation (cargo, npm, go, pytest) uses subprocess with list args.
- The only shell interaction is the user's terminal running `hyperi-ci`.

### 2. Semantic Release Centric

All versioning and publishing flows through semantic-release:
- Push to main → semantic-release analyses conventional commits → creates tag → publishes
- Reusable workflows call `hyperi-ci run publish` only on release events
- No manual version bumps, no manual publishing
- Config: `.releaserc.yaml` with conventional commits preset

### 3. CI Skip — Simple and Obvious

To skip CI on a push, use the standard Git convention:
- **`[skip ci]`** or **`[ci skip]`** anywhere in the commit message
- GitHub Actions natively respects this — no custom logic needed

### 4. hyperi-pylib as Direct Dependency

- `hyperi-pylib` provides: structured logger (loguru), Typer CLI, config utilities
- Installed as a normal dependency — resolved at `uv tool install` time
- **Zero runtime package repo access** — the built wheel is self-contained
- For dev: local path source via `[tool.uv.sources]` in pyproject.toml

### 5. uv for Everything

- `uv venv --python 3.12 .venv` for virtual environment
- `uv sync` for dependency installation
- `uv tool install hyperi-ci` for end-user installation
- `uvx hyperi-ci` for one-shot execution without install
- Never use pip/venv directly

### 6. Self-Hosting CI

hyperi-ci uses itself for CI. The repo has its own CI workflow at
`.github/workflows/ci.yml` that runs quality, test (Linux + macOS matrix),
and semantic release on main.

### 7. Cross-Platform (Linux + macOS)

- All code runs identically on Linux (CI runners) and macOS (dev laptops)
- Use `pathlib` not string paths, `shutil.which()` not hardcoded paths
- Platform detection via `sys.platform`, skip cross-compile on macOS

### 8. Publish Target Routing

A single GitHub variable `PUBLISH_TARGET` (org-level, repo-level override)
controls where artifacts go:
- `internal` — JFrog Artifactory only (default)
- `oss` — Public registries (PyPI, npm, crates.io, GHCR)
- `both` — Publish to both

Maps to `HYPERCI_PUBLISH_TARGET` env var. Config API:
```python
config.publish_target           # "internal", "oss", or "both"
config.destination_for("python")  # ["jfrog-pypi"] or ["pypi"] or both
```

## Architecture

```
src/hyperi_ci/
├── __init__.py          # Package root, __version__
├── cli.py               # Typer CLI entry point
├── config.py            # Typed config schema + loader (CIConfig, OrgConfig)
├── common.py            # Shared utilities (logger, subprocess, GH Actions helpers)
├── detect.py            # Language detection from file markers
├── dispatch.py          # Stage dispatcher → language handlers
├── publish/             # Publish handlers (not yet wired)
└── languages/
    ├── python/          # quality.py, test.py, build.py
    ├── rust/            # quality.py, test.py, build.py
    ├── typescript/      # quality.py, test.py, build.py
    └── golang/          # quality.py, test.py, build.py

config/
├── org.yaml             # Organisation config (JFrog, GitHub, GHCR URLs)
└── defaults.yaml        # Default CI settings for all languages

test-projects/
├── ci-test-rust-minimal/      # Zero-dep Rust binary
├── ci-test-python-minimal/    # Zero-dep Python package
├── ci-test-ts-minimal/        # Zero-dep TypeScript package
└── ci-test-go-minimal/        # Zero-dep Go binary
```

## Config Cascade

Priority (highest wins):
```
CLI flags → ENV vars (HYPERCI_*) → .hyperi-ci.yaml → config/defaults.yaml → hardcoded
```

## Commands

```bash
uv sync                              # Install deps
uv run pytest tests/ -v              # Run tests (34 tests)
uv run ruff check src/ tests/        # Lint
uv run ruff format src/ tests/       # Format
uv run hyperi-ci --version           # Verify CLI
uv run hyperi-ci detect              # Test language detection
uv run hyperi-ci config              # Show merged config as JSON
```

## Handler Interface

Each language handler module exports a `run()` function:

```python
def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Run the stage. Returns exit code (0 = success)."""
```

Dispatch finds handlers via `hyperi_ci.languages.<lang>.<stage>` module path.

## Key Files

| File | Purpose |
|------|---------|
| `VERSION` | Source of truth for version (currently 0.0.0) |
| `config/org.yaml` | Organisation-specific config (JFrog, GitHub, GHCR) |
| `config/defaults.yaml` | Default values for all CI settings |
| `pyproject.toml` | Package config, deps, tool config |
| `uv.lock` | Locked dependencies (committed) |
| `.releaserc.yaml` | Semantic release config |
| `.github/workflows/ci.yml` | Self-hosting CI workflow |

## What's Done

- Core Python package: cli, config, detect, dispatch, common
- Language handlers: Python, Rust, TypeScript, Go (quality, test, build)
- Config system with defaults, org config, env override, publish target routing
- 34 unit tests — all passing
- 4 test projects (Rust, Python, TypeScript, Go)
- Self-hosting CI workflow (quality + test matrix + semantic release)
- Semantic release config
- README.md with background and usage
- Initial commit pushed to `hyperi-io/hyperi-ci`

## What's Next

| Priority | Task | Notes |
|----------|------|-------|
| 1 | Attach `ai` submodule | Standards, rules, CLAUDE.md context |
| 2 | Reusable workflow templates | `rust-ci.yml`, `python-ci.yml`, `ts-ci.yml`, `go-ci.yml` in `.github/workflows/` |
| 3 | `hyperi-ci init` command | Replaces 1020-line `attach.sh` — generates `.hyperi-ci.yaml`, Makefile, workflow |
| 4 | Publish handlers | Wire `src/hyperi_ci/publish/` — JFrog + OSS routing per `PUBLISH_TARGET` |
| 5 | Validate with test projects | Create GitHub repos, push, verify CI end-to-end |
| 6 | Consumer project cutover | Remove `ci/` submodule, run `hyperi-ci init`, verify |
| 7 | Archive old repo | `hyperi-io/ci` → read-only |

## Licensing

Proprietary — HYPERI PTY LIMITED.
