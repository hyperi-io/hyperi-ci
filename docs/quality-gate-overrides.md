<!--
Project:   HyperI CI
File:      docs/quality-gate-overrides.md
Purpose:   Turning a quality gate up (--strict), down for one run, or off for one rule

License:   BUSL-1.1 -- HYPERI PTY LIMITED
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Quality gate: overrides

Three mechanisms, in order
from strictest to most targeted: `--strict` upgrades every warning for a
pre-push check, `HYPERCI_QUALITY_SKIP` force-disables a tool for one run, and
`quality.ignore` silences one rule permanently and reviewably.

## `--strict` - a zero-warnings pre-push gate

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
\- it is committed, diffable, and carries a `reason`:

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

Semgrep's `python.lang.compatibility.*` rules are excluded automatically, with
no `quality.ignore` entry needed, for every rule whose target Python version
the project's own `requires-python` floor has already outgrown.

osv-scanner takes its ignores as a config file, and once it is handed one with `--config` it stops reading the repo's own `osv-scanner.toml` beside the lockfile. So hyperi-ci appends the `quality.ignore` and `deny.toml` ids to a copy of that file, outside the checkout, and the repo's file is never changed. Every setting in the repo's file is kept. Where both name the same id, the repo's entry wins and the generated one is dropped, because osv-scanner honours only the first. A repo file that cannot be extended (invalid TOML, or an inline `IgnoredVulns = [...]` array) stops the scan: it fails a `blocking` gate and warns under `warn`.

A lockfile that lists no packages makes osv-scanner exit 128. hyperi-ci reports that as NOT SCANNED, the same as a missing lockfile: a warning, neither a finding nor a clean result.

Only exit 1 is a finding. When osv.dev cannot be queried, v2.6.0 exits 127 and names the `vulnmatch/osvdev` matcher in its error, and it prints a zero-vulnerability summary that is not true. hyperi-ci reports that as NOT SCANNED too, and passes in both modes, the same policy as cargo-audit's unreachable advisory database. Exit 129 is mapped to the API failure in osv-scanner's source but not yet returned by it, and is handled the same way. Any other non-zero exit is a scanner error: it fails a `blocking` gate and warns under `warn`, and is never called a finding.

## See also

- [quality-gate.md](quality-gate.md) -- mode resolution, and relaxing a security gate with a `reason`
- [quality-gate-tools.md](quality-gate-tools.md) -- the per-tool reference these overrides apply to
