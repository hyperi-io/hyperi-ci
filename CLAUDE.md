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

### 3. CI Skip — Simple and Obvious

To skip CI on a push, use the standard Git convention:
- **`[skip ci]`** or **`[ci skip]`** anywhere in the commit message
- GitHub Actions natively respects this — no custom logic needed
- Document prominently in consumer project templates

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

hyperi-ci uses itself for CI. The repo has its own slimmed-down CI workflow
that runs `hyperi-ci run quality`, `hyperi-ci run test`, etc.

### 7. Cross-Platform (Linux + macOS)

- All code runs identically on Linux (CI runners) and macOS (dev laptops)
- Use `pathlib` not string paths, `shutil.which()` not hardcoded paths
- Platform detection via `sys.platform`, skip cross-compile on macOS

## Architecture

```
src/hyperi_ci/
├── __init__.py          # Package root, __version__
├── cli.py               # Typer CLI entry point
├── config.py            # Typed config schema + loader (CIConfig, OrgConfig)
├── common.py            # Shared utilities (logger, subprocess, GH Actions helpers)
├── detect.py            # Language detection from file markers
├── dispatch.py          # Stage dispatcher → language handlers
└── languages/
    ├── python/          # quality.py, test.py, build.py
    ├── rust/            # quality.py, test.py, build.py
    ├── typescript/      # quality.py, test.py, build.py
    └── golang/          # quality.py, test.py, build.py
```

## Config Cascade

Priority (highest wins):
```
CLI flags → ENV vars (HYPERCI_*) → .hyperi-ci.yaml → config/defaults.yaml → hardcoded
```

## Commands

```bash
uv sync                              # Install deps
uv run pytest tests/ -v              # Run tests
uv run ruff check src/ tests/        # Lint
uv run ruff format src/ tests/       # Format
uv run hyperi-ci --version           # Verify CLI
uv run hyperi-ci detect              # Test language detection
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
| `VERSION` | Source of truth for version |
| `config/org.yaml` | Organisation-specific config (JFrog, GitHub, GHCR) |
| `config/defaults.yaml` | Default values for all CI settings |
| `pyproject.toml` | Package config, deps, tool config |
| `uv.lock` | Locked dependencies (committed) |

## Licensing

Proprietary — HYPERI PTY LIMITED. Same licence as `/projects/ai`.
