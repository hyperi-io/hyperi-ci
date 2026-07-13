# Project:   HyperI CI
# File:      docs/quality-gate.md
# Purpose:   Reference for the quality stage - tools, modes, --strict, skip hatch
#
# License:   BUSL-1.1 -- HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED

# Quality gate

The quality stage runs a fixed set of tools and decides, per tool, whether a
finding is fatal. Two cross-language scanners (gitleaks, semgrep) run once at
the dispatch level; the rest run in the per-language handler. Each tool's
**effective mode** is resolved from config, an optional strict upgrade, and a
force-skip escape hatch, in that precedence.

Same code runs locally (`hyperi-ci check`) and in CI (`hyperi-ci run quality`).
The only difference is the local-vs-CI handling of a missing tool (below).

## Effective mode - how a tool's fate is decided

Bottom line: **skip beats strict beats configured mode.** A force-skip disables
the tool; otherwise strict upgrades a `warn` tool to `blocking`; otherwise the
configured mode stands.

```mermaid
flowchart TB
    T["a quality tool"] --> SK{"tool in<br/>HYPERCI_QUALITY_SKIP?"}
    SK -->|yes| D["disabled<br/>(loud CI warning)"]:::skip
    SK -->|no| M["configured mode<br/>quality.&lt;tool&gt; or quality.&lt;lang&gt;.&lt;tool&gt;"]
    M --> ST{"--strict AND<br/>mode is warn?"}
    ST -->|yes| B["blocking"]:::block
    ST -->|no| K["keep configured mode<br/>(blocking / warn / disabled)"]
    classDef skip fill:#D55E00,color:#fff
    classDef block fill:#0072B2,color:#fff
```

Resolution lives in `src/hyperi_ci/languages/quality_common.py`
(`resolve_tool_mode`, `apply_strict`, `is_skipped`) and is shared by the
per-language handlers and the dispatch-level semgrep module, so the precedence
is identical everywhere.

## Modes

| Mode | Finding behaviour |
|---|---|
| `blocking` | A finding fails the stage (non-zero exit) |
| `warn` | A finding prints but does not fail |
| `disabled` | The tool does not run |

Set per project in `.hyperi-ci.yaml` under `quality.<lang>.<tool>` (or
`quality.<tool>` for the cross-language `gitleaks` / `semgrep`); defaults live in
`src/hyperi_ci/config/defaults.yaml`.

## Tools

| Tool | Scope | Where |
|---|---|---|
| gitleaks | cross-language secret scan | dispatch (`quality/gitleaks.py`) |
| semgrep | cross-language SAST (`--config auto`) | dispatch (`quality/semgrep.py`) |
| ruff (lint, format, docstrings) | Python | `languages/python/quality.py` |
| ty | Python types | Python handler |
| pip-audit, bandit, vulture | Python | Python handler |
| clippy, rustfmt, cargo-audit/deny, osv-scanner | Rust | `languages/rust/quality.py` |
| eslint, prettier, tsc, npm audit, osv-scanner | TypeScript | `languages/typescript/quality.py` |
| gofmt, govet, golangci-lint, gosec, govulncheck | Go | `languages/golang/quality.py` |

semgrep and gitleaks moved to the dispatch level because their rulesets are
language-agnostic - running them once avoids the drift where only one handler
passed shared excludes.

## --strict - a zero-warnings pre-push gate

`hyperi-ci check --strict` treats every `warn`-tier finding as `blocking`, so a
developer sees - and fixes or explicitly ignores - everything CI would surface
BEFORE the push, not after. It sets `HYPERCI_QUALITY_STRICT=1`, which
`apply_strict` reads.

`disabled` tools stay off (strict enforces warnings, it does not resurrect a
tool a project turned off). A tool that is not installed locally (and has no
`uv` fallback) is still warn-skipped even under `--strict` - strict enforces
what runs, not what your machine has; CI, where the tools are present, is the
backstop.

```bash
hyperi-ci check --strict --quick     # strict quality only, no tests
# -> non-zero if any tool has findings; fix or ignore each, then re-run
```

## HYPERCI_QUALITY_SKIP - the rare escape hatch

> **Note:** This is an EMERGENCY override, not the normal path. The reviewed,
> auditable way to silence a tool is the config (`quality.<tool>: disabled` or
> the `quality.ignore` list).

When a tool's false positive halts CI - a semgrep rule misfiring on a
dependency, an audit advisory with no fix yet - set `HYPERCI_QUALITY_SKIP` to
the tool name (comma-separated for several) to force it to `disabled` for the
blocked runs WITHOUT a config commit, then remove it once the real fix lands.

A force-skip is logged LOUDLY: a `warn()` line plus, in CI, a real GitHub
`::warning::` annotation that lands in the run summary (it does not hide inside
a collapsed log group) - so skipping a security scanner like gitleaks cannot
pass unnoticed.

In CI, set the `HYPERCI_QUALITY_SKIP` repo or org Actions variable; the four
reusable language workflows pass it through (empty variable = no-op). Only a
repo admin / org owner can set it.

```bash
# local one-off: skip semgrep for this run
HYPERCI_QUALITY_SKIP=semgrep hyperi-ci run quality
```

## Suppressing a specific rule (the reviewed path)

To silence one noisy rule permanently, use `quality.ignore` in `.hyperi-ci.yaml`
- it is committed, diffable, and carries a `reason`:

```yaml
quality:
  ignore:
    - tool: semgrep
      ids:
        - <full.rule.id>
      reason: "why this rule is noise here"
```

This is rule-scoped (not a path exclude), so the rest of the tool's coverage
stays active. `for_tool` in `src/hyperi_ci/quality/ignores.py` feeds these to
the tool's native ignore flag.

## Missing tool - local vs CI

A tool that is not installed and has no `uv`/`uvx` fallback:

- **In CI** (`CI` env set): a `blocking` tool FAILS - every tool must be
  present, and a silent skip would mask a coverage gap.
- **Locally**: it warn-skips and carries on, so `hyperi-ci check` still runs
  whatever IS installed and tells you what it skipped.

This matches the gitleaks stage's existing behaviour (`is_ci()` in
`src/hyperi_ci/common.py`).
