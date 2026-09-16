### Upgrade notes

- LLVM 23 is now the BOLT and lld default for every Rust release build.
  - No repo in the fleet pins an LLVM major, so the whole fleet moves together on its next release.
  - A BOLT failure degrades to PGO-only and the build still passes.
  - Check the first dfe-receiver release on this version for `.bolt` and `.text.hot` sections -- `docs/languages/rust-release-verification.md`, under Verification, has the commands.
- Developer machines are unaffected.
  - hyperi-developer's Rust role installs no apt LLVM, only cargo-llvm-cov and rustup's llvm-tools-preview.
  - Running PGO or BOLT locally is the one case that needs `bolt-23` and `lld-23` from apt.llvm.org.
- `skip-optimize` drops the optimisation stage for a single run.
  - `hyperi-ci update` picks up the CLI half.
  - A repo scaffolded before this release has no dispatch input in its ci.yml: copy the block out of a fresh `hyperi-ci init`, or set the `HYPERCI_SKIP_OPTIMIZE` repo variable.
- Four tool digests that an automated bump had left stale are corrected.
  - A Go quality job or an osv-scanner step that failed at install today is fixed by this release.
- Markdown is out of the `ruff format` gate, and a project's own `[tool.ruff] exclude` list is honoured again.
- Node 24 and Helm v4 land in this release, for the TypeScript workflow and the gitops templates.
- Four Python quality tools are pinned in this release.
- The two cargo enrichment tests skip when rustup has no toolchain, rather than failing on a box that has only cargo on PATH.
