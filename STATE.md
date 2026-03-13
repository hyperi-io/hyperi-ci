# HyperI CI — Project State

Static context for AI assistants. For tasks and progress see `TODO.md`.

## Background

Ground-up rewrite of the HyperI CI system. The previous CI (`hyperi-io/ci`)
grew organically from GitLab origins, producing ~100 shell scripts, 50+
composite actions, a 1020-line `attach.sh`, and delivery via git submodule
across 14+ consumer projects. Six-layer dispatch hierarchy, config settable
in four places, significant dead code.

This repo rationalises everything into a single Python CLI tool (`hyperi-ci`),
distributed via `uv tool install`. Consumer projects get a five-line reusable
workflow and a Makefile. Same tool runs locally and in CI.

The old repo (`hyperi-io/ci`) will be archived once all consumer projects
have cut over. **Reference the old CI** at `/projects/ci` for proven patterns
before reinventing — it has working solutions for system deps, cross-compilation
sysroots, cargo registry config, and binary verification.

**MANDATORY: Read `docs/CI-LESSONS.md` before implementing or debugging any CI
handler.** It contains extracted patterns, gotchas, and solutions from the old
CI. Ignoring it wastes time rediscovering known problems (e.g. mold linker,
multi-arch package conflicts, sysroot approach, integration test threading).

## Hard Design Principles

1. **NO BASH** — all CI logic is Python. `subprocess.run()` with list args.
2. **Semantic release centric** — push to main, semantic-release creates tags.
3. **uv for everything** — venv, sync, lock, tool install, build.
4. **Cross-platform** — Linux (CI) and macOS (dev). Uses `pathlib`, `shutil.which()`.
5. **Self-hosting** — hyperi-ci uses itself for its own CI.
6. **Publish target routing** — `PUBLISH_TARGET` = internal/oss/both controls where artifacts go.

## Architecture

See `docs/DESIGN.md` for full architecture documentation.

```
src/hyperi_ci/
├── cli.py               # Typer CLI (run, check, init, detect, config, trigger, watch, logs)
├── config.py            # CIConfig, OrgConfig, config cascade loader
├── common.py            # Logging, subprocess helpers, GH Actions output
├── detect.py            # Language detection from file markers
├── dispatch.py          # Stage dispatcher → language handlers
├── publish_binaries.py  # Generic binary publish (GitHub Releases, JFrog, R2)
├── init.py              # Project scaffolding (replaces attach.sh)
├── gh.py                # GitHub CLI helpers
├── trigger.py           # Workflow trigger command
├── watch.py             # Run watch command
├── logs.py              # Log fetch command
└── languages/
    ├── python/          # quality, test, build, publish
    ├── rust/            # quality, test, build, publish
    ├── typescript/      # quality, test, build, publish
    └── golang/          # quality, test, build, publish
```

## Handler Interface

Every language handler module exports:

```python
def run(config: CIConfig, extra_env: dict[str, str] | None = None) -> int:
    """Run the stage. Returns exit code (0 = success)."""
```

Dispatch finds handlers via `hyperi_ci.languages.<lang>.<stage>` module path.

## Config Cascade

Priority (highest wins):
```
CLI flags → ENV vars (HYPERCI_*) → .hyperi-ci.yaml → config/defaults.yaml → hardcoded
```

## Key Files

| File | Purpose |
|------|---------|
| `VERSION` | Source of truth for version |
| `config/org.yaml` | Organisation-specific config (JFrog, GitHub, GHCR) |
| `config/defaults.yaml` | Default values for all CI settings |
| `config/versions.yaml` | SSOT for action/runtime/tool versions |
| `config/secrets-access.yaml` | Group-based org secret visibility management |
| `pyproject.toml` | Package config, deps, tool config |
| `uv.lock` | Locked dependencies (committed) |
| `.releaserc.yaml` | Semantic release config |
| `.github/workflows/ci.yml` | Self-hosting CI workflow |
| `.github/workflows/rust-ci.yml` | Reusable Rust CI workflow |
| `scripts/update-versions.py` | Version sync/update script |
| `scripts/sync-secrets-access.py` | Secret repo access sync script |
| `docs/DESIGN.md` | Full architecture documentation |
| `docs/CI-LESSONS.md` | Lessons from old CI — MUST READ before handler work |

## Commands

```bash
uv sync                              # Install deps
uv run pytest tests/ -v              # Run tests
uv run ruff check src/ tests/        # Lint
uv run ruff format src/ tests/       # Format
uv run hyperi-ci --version           # Verify CLI
uv run hyperi-ci detect              # Language detection
uv run hyperi-ci config              # Show merged config
uv run hyperi-ci init                # Scaffold a project
uv run hyperi-ci check               # Pre-push: quality + test
uv run hyperi-ci check --full        # Pre-push: quality + test + build (native only)
uv run hyperi-ci check --quick       # Pre-push: quality only
```

## Licensing

Proprietary — HYPERI PTY LIMITED.
