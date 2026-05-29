# Decision: reusable-workflow pinning — gate-only (issue #31)

**Status:** Accepted · 2026-05-29 · supersedes the "atomic frozen graph
(A-decouple)" spec.

## Context — the problem

The language reusable workflows (`rust-ci.yml`, `python-ci.yml`, …) reference
their sibling composites + `_release-tail.yml` at `@main`. A consumer SHA-pins
the *caller* (Renovate), but those internals resolve **live from `main`** at run
time. So a breaking interface change on `main` **retroactively breaks every
pinned consumer** at startup — 0 jobs, no logs. It bit hyperi-pylib. The pin is
skin-deep: the *cost* of pinning without the *benefit*.

Same root as the old tag-orphaning bug: **coupling correctness to a mutable git
ref** (there, "tags reachable from HEAD"; here, "`@main` siblings").

## Options considered

You can have any **two** of: keep semantic-release · frozen graph · seamless dev loop.

| Option | keep semantic-release | frozen graph | seamless dev loop | cost |
|---|---|---|---|---|
| **Gate-only** | ✅ | ✗ | ✅ | none — relies on a source-side gate + branch protection |
| `@v2` major tag | ✅ | ~ band | ✗ | composite edits **inert on `main` until `v2` advances** |
| Freeze-on-main | ✅ | ✅ | ✗ | re-adds committed-back `@semantic-release/git` (orphaning) + inert edits |
| A-decouple | ✗ **bespoke oracle** | ✅ | ✅ | ~280 lines replacing semantic-release's version+tag |

A-decouple is the only one giving frozen-graph *and* a seamless dev loop — but
only by replacing a battle-tested tool with custom code.

## Decision

**Gate-only.** Keep `@main` internals and semantic-release; prevent the breakage
**at source** with a static interface backward-compat gate
(`scripts/check-workflow-interfaces.py`) in hyperi-ci's own CI.

## Why (the rationale)

- **KISS. Over-engineered CI kills companies — repeatedly.** A-decouple meant
  replacing semantic-release (battle-tested) with bespoke release machinery — a
  custom version oracle + off-main `commit-tree` tagger + freezer — that we'd
  maintain and debug forever. A proven tool that's "good enough" wins.
- **The acute pain is already fixed at source.** The gate fails our CI when a
  sibling interface regresses → the break never ships. A frozen graph is not
  needed to *stop the breakage* — only to make the pin tamper-proof.
- **First-party context.** Consumers are hyperi-io's own repos; hyperi-ci is our
  own tool. The "frozen auditable graph" / tamper-resistance benefit matters far
  more for **third-party** deps (which `/deps` SHA-pins) than for our **own**
  internals — those are defended by org access control + branch protection, not
  by pinning.
- **Reversible.** If tamper-resistance ever becomes a hard requirement, `@v2`
  adds a compatible-band frozen graph with a one-line tag-move — still no custom
  oracle.

## The gate (design)

`scripts/check-workflow-interfaces.py`, run in hyperi-ci's Quality job, diffs
each reusable-workflow + composite interface in the working tree against the
**last release tag** and fails on a backward-incompatible delta:

- removed input / output / secret,
- a newly-**required** input (no default), or optional → required,
- a **removed** pipeline file (a pinned caller's `@main` ref would 404).

Additive changes (new optional input, relaxed required) pass. Tested in
`tests/unit/test_workflow_interfaces.py`.

## Precondition — the deal-maker

Gate-only is **source-side**. Because consumers consume `main` live, the gate
must *block* a regression from reaching `main`, so it requires:

- **branch protection ON** on hyperi-ci `main`,
- **Quality as a required status check**, and
- **PR-only merges**.

Without these the gate is an alarm, not a barrier (a direct push lands the bad
commit on `main` and the next consumer run pulls it). Branch protection is
currently disabled for active dev — **re-enable when stable** (org TODO).

## What we consciously accept

- Consumer caller-pins are **skin-deep by design** — internals float `@main`;
  breakage is prevented at source, not by a frozen graph.
- Consumers always run hyperi-ci's **latest** `main` internals (no pin-back); a
  bad `main` affects all at once — mitigated by the gate + the `ci-test-*`
  fixtures + fast fix-forward.
- The gate catches **structural/interface** breaks, not behavioral; there is no
  tamper-proof audit graph for the orchestration.

## Consequences

- **Removed:** the A-decouple oracle + freezer + their CLI commands + tests
  (`src/hyperi_ci/release/`, `next-version`, `freeze-internals`) — never reached
  PyPI, so no consumer impact.
- **Kept:** semantic-release as the oracle + tagger, `@main` internals, the
  seamless dev loop, and the Phase 1 gate (hardened with the removed-file check).
- **Unrelated, retained:** `@semantic-release/github` (hyperi-ci now creates GH
  Releases — that had been a migration-deletion bug, not part of this decision).
