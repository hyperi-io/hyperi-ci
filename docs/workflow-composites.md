<!--
Project:   HyperI CI
File:      docs/workflow-composites.md
Purpose:   Why some CI logic is a shared composite and some is duplicated per language

License:   BUSL-1.1 -- HYPERI PTY LIMITED
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Workflow composites and internal refs

The rule for what gets extracted into a shared composite versus duplicated per language, and why our own reusable workflows reference each other at `@main` rather than a pinned SHA.

## What's shared vs duplicated

The rule: **language-agnostic and identical across languages -> shared; anything
that needs a per-language carve-out -> stays in the language SME's domain in its
complete form.** Shared pieces must help the SME, never hobble them.

| Concern | Shared? | Where |
|---|---|---|
| Predict-and-gate (version oracle + gate outputs) | YES | `actions/predict-version` composite |
| Toolchain + dep install (uv, language runtime) | YES | `actions/setup-runtime` composite |
| OSV vulnerability scan | YES | `actions/setup-osv-scanner` composite |
| semantic-release toolchain + default config | YES | `actions/setup-semantic-release` composite |
| Release tail (container + tag + publish) | YES | `_release-tail.yml` reusable workflow |
| Version stamping (VERSION file) | YES | CLI `stamp-version` (central), see below |
| Build commands, cache keys, per-call `run_gate_tool` options | NO | Inline per language in `<lang>-ci.yml` + handlers |
| Plan-job structure, gate `if:` strings | DUPLICATED inline | small and identical across the four workflows; cheaper than the abstraction - drift caught by `tests/unit/test_workflow_consistency.py` |

**When we extract a composite vs inline:** when the shared steps are more than a
few lines *and* identical across languages (runtime setup, the OSV scan, the
semantic-release toolchain). A short repeated snippet stays inlined - composite
indirection would cost more than it saves, and the consistency lint catches
drift. This is a refinement of the earlier "inline everything" stance: the four
composites above earned extraction; nothing smaller has.

## Central vs language-specific (the VERSION example)

Version writing is identical regardless of language, so it is central:
`stamp-version` writes the `VERSION` file, then delegates only the
*manifest* edit (Cargo.toml `[package]`, pyproject `[project]`, package.json)
to a per-language `stamp_manifest`. The release version itself resolves once,
the same way everywhere - `HYPERCI_VERSION` env -> `VERSION` file
(`common.resolve_release_version`) - so build, container and publish never
disagree. See [flow.md](flow.md) section 3.

`VERSION` is a stamp TARGET, not a version the project maintains. The value is
derived from the git tags (semantic-release, in `predict-version`), written on the
runner before packaging, and never committed back -- the other half of
tag-on-publish: the released version IS the git tag. So the committed value is
whatever it was last stamped to by hand, in every repo on hyperi-ci, and it goes
stale immediately. It bites only where someone runs a package from its own
checkout, which is why `hyperi-ci --version` names the checkout path when the
install is editable.

## Same-org refs stay `@main` - made safe by a gate

Third-party actions are SHA-pinned by Renovate, with a 7-day cooldown. Our **own** reusable workflows and composites reference their
siblings at `@main`, deliberately - pinning them would freeze the dev loop. A
consumer SHA-pinning the *caller* still floats those `@main` internals, so a
breaking interface change on `main` could break pinned consumers retroactively.
We stop that **at source** with an interface backward-compat gate in our own
Quality job, not with a frozen graph. Full rationale, the trilemma, and the
branch-protection precondition: [dependencies/WORKFLOW-PINNING.md](dependencies/workflow-pinning.md).
Third-party pinning policy: [dependencies/DEPS-PINNING.md](dependencies/deps-pinning.md).

That interface comparison covers inputs, outputs and secrets, and cannot see the version split underneath it. A consumer resolves the YAML at `@main`, so a push is live instantly; the runner installs `uvx hyperi-ci` from PyPI, so CLI code is live only once a release finishes. A commit whose workflow needs new CLI behaviour is broken until that release lands, and hyperi-ci's own release run is the first caller of the workflow it is shipping.

**A workflow change on main must work against the CLI already released to PyPI.** Ship the capability as its own commit, release it, then switch the workflow on in a second commit. The reverse order is safe: CLI code needing a new workflow input finds it already there.

The gate enforces the names, not the behaviour: every subcommand and option a workflow or composite passes to the PyPI CLI (`${{ env.HYPERCI_INSTALL }}`, `$HYPERCI_INSTALL` or an unpinned `uvx hyperi-ci`) must exist in the latest release, read from that wheel's typer model. `uv run hyperi-ci` runs the checkout's own CLI and is not checked.

## See also

- [architecture.md](architecture.md) -- the two-sides overview
- [ci-job-contract.md](ci-job-contract.md) -- the jobs these composites run inside
- [dependencies/workflow-pinning.md](dependencies/workflow-pinning.md) -- the interface gate in full
- [dependencies/deps-pinning.md](dependencies/deps-pinning.md) -- third-party action pinning
