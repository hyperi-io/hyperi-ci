<!--
Project:   HyperI CI
File:      docs/dependency-licences.md
Purpose:   The dependency-licence gate: policy table, modes, overrides, output

License:   BUSL-1.1 -- HYPERI PTY LIMITED
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Dependency licences

A licence verdict comes from a table, never from anyone's memory. `src/hyperi_ci/config/license-policy.yaml` maps an SPDX id and a consumption mode to `allow`, `allow-with-obligations`, `deny` or `ask`, plus the obligations each carries.

| Mode | Means |
|---|---|
| `import` | installed unmodified and loaded at runtime (Python import, npm require) |
| `static` | linked into a binary (Rust crates, Go modules) |
| `vendored` | source copied into the tree |
| `modified` | patched, forked, or copied in and edited |

LGPL-3.0, for example, is allowed as an import with a notice and the LGPL and GPL texts, and denied when copied in. GPL, AGPL and SSPL are denied in every mode. Only what ships is judged, so a tool CI runs (reuse is GPL-3.0-or-later) is never in scope.

- `A OR B` takes the better term, `A AND B` the worse. A single pair of brackets round the whole expression is dropped. Nested brackets and mixed operators are ask.
- `A WITH LLVM-exception` is judged as `A`. Any other exception is ask.
- No licence declared, an id the table does not list, or a dependency missing from what the tool read is ask, never deny.

## What each language reads

| Language | Tool | Shipped set | Mode |
|---|---|---|---|
| Python | pip-licenses, pinned in `versions.yaml` | `uv export --no-dev --all-extras`, without the project. A pin whose marker excludes this platform is named and skipped | `import` |
| Rust | `cargo metadata --locked --all-features` | crates a workspace member reaches through normal dependencies, with every feature on. Not dev or build dependencies, proc-macros, or what only a proc-macro needs | `static` |
| TypeScript | license-checker-rseidelsohn, pinned in `versions.yaml` | the installed `node_modules`, `--production`, for npm, pnpm and yarn alike | `import` |

A licence license-checker guessed from file text (a trailing `*`) is ask. A README never stands in for a licence file. cargo deny still runs its own `deny.toml` checks beside this.

Every finding goes to `.hyperi-ci/licenses.json`, with the policy's sha256 and the tool versions. The directory carries its own `.gitignore`. A held finding prints one line a citation can quote verbatim, then its evidence: for an undeclared licence, the licence file's path and first lines.

```text
licence: pypi pysigma-backend-clickhouse 1.1.1 unknown import -> ask (rule undeclared, source license-file)
```

Every run also writes `.hyperi-ci/THIRD-PARTY-NOTICES`: each dependency whose obligations call for attribution, sorted, with its own licence file and, for Apache-2.0, its NOTICE file. A repo that commits `THIRD-PARTY-NOTICES` at its root has it compared byte for byte, and a stale copy fails the gate. Copy the generated file over it. An LGPL dependency's notices do not yet include the GPL text that LGPL incorporates.

## Answering an ask

`deny` and `ask` fail the gate, and `quality.dep_licenses` ships `warn`. A human answers an `ask`, or changes one cell of the table for this repo, in `.hyperi-ci.yaml`. Each entry needs a reason. Pin an answer to the version it was checked against, so a new version is asked again:

```yaml
quality:
  dep_licenses:
    mode: blocking
    overrides:
      - name: pysigma-backend-clickhouse
        version: "1.1.1"
        licence: LGPL-3.0-only
        reason: "wheel dist-info/licenses/LICENSE.md is the LGPL-3.0 text; PyPI metadata is null"
    policy:
      lgpl:
        static: {verdict: deny, reason: "no LGPL in our Rust binaries"}
```
